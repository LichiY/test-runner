"""Test runner module - handles build and test execution.

Supports:
- Docker-based execution (default): auto-detects JDK version, runs in container
- Local execution: direct Maven/Gradle on the host machine
- Two rerun modes:
  - isolated: separate JVM per run (default, for Implementation Dependency type)
  - same_jvm: same JVM with forkCount=0 (for NIO type)
"""

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .data import TestEntry
from .docker import (detect_java_version, docker_run, get_docker_image,
                     is_docker_available, pull_image)

logger = logging.getLogger(__name__)


class RunResult(Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class RerunMode(Enum):
    ISOLATED = "isolated"
    SAME_JVM = "same_jvm"


@dataclass
class TestRunResult:
    entry: TestEntry
    status: str  # "completed", "clone_failed", "file_not_found", "patch_failed", "build_failed"
    results: List[str] = field(default_factory=list)
    error_message: str = ""
    build_output: str = ""

    @property
    def pass_count(self) -> int:
        return self.results.count("pass")

    @property
    def fail_count(self) -> int:
        return self.results.count("fail")

    @property
    def error_count(self) -> int:
        return self.results.count("error")

    @property
    def is_flaky(self) -> bool:
        return self.pass_count > 0 and self.fail_count > 0

    @property
    def all_pass(self) -> bool:
        return len(self.results) > 0 and all(r == "pass" for r in self.results)

    @property
    def all_fail(self) -> bool:
        return len(self.results) > 0 and all(r == "fail" for r in self.results)


def detect_build_tool(repo_dir: str, module: str) -> str:
    """Detect Maven or Gradle."""
    if os.path.isfile(os.path.join(repo_dir, 'pom.xml')):
        return 'maven'
    if (os.path.isfile(os.path.join(repo_dir, 'build.gradle')) or
            os.path.isfile(os.path.join(repo_dir, 'build.gradle.kts'))):
        return 'gradle'
    return 'maven'


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_project(repo_dir: str, entry: TestEntry,
                  use_docker: bool = True,
                  timeout: int = 600, max_retries: int = 2) -> Tuple[bool, str]:
    """Build the project (compile test classes).

    Args:
        repo_dir: Repository root.
        entry: Test entry with module info.
        use_docker: If True, run build inside Docker container.
        timeout: Build timeout in seconds.
        max_retries: Max retry attempts on recoverable errors.

    Returns:
        (success, output).
    """
    build_tool = detect_build_tool(repo_dir, entry.module)

    # Prepare Docker image if needed
    docker_image = None
    if use_docker:
        if not is_docker_available():
            logger.warning("Docker daemon not available, falling back to local execution")
            use_docker = False
        else:
            docker_image = get_docker_image(repo_dir, build_tool, entry.module)  # 按模块选择更准确的 Docker 镜像。
            if not pull_image(docker_image):
                logger.warning(f"Failed to pull Docker image {docker_image}, falling back to local")
                use_docker = False

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"Build retry attempt {attempt}/{max_retries}")
            time.sleep(2)

        if build_tool == 'maven':
            success, output = _build_maven(repo_dir, entry, timeout,
                                           use_docker, docker_image)
        else:
            success, output = _build_gradle(repo_dir, entry, timeout,
                                            use_docker, docker_image)

        if success:
            return True, output

        if attempt < max_retries and _is_recoverable_build_error(output):
            logger.info("Recoverable build error detected, retrying...")
            continue

        return False, output

    return False, "Build failed after all retries"


def _build_maven(repo_dir: str, entry: TestEntry, timeout: int,
                 use_docker: bool, docker_image: Optional[str]) -> Tuple[bool, str]:
    """Build using Maven."""
    cmd_parts = _maven_build_cmd(entry)
    mvn = _get_local_maven_cmd(repo_dir)  # 无论本地还是 Docker 都优先复用项目自带的 Maven wrapper。

    if use_docker and docker_image:
        return _run_in_docker(docker_image, repo_dir, [mvn] + cmd_parts, timeout)  # 容器内也使用 wrapper 以保持构建一致性。
    else:
        cmd = [mvn] + cmd_parts
        return _run_local(cmd, repo_dir, timeout)


def _build_gradle(repo_dir: str, entry: TestEntry, timeout: int,
                  use_docker: bool, docker_image: Optional[str]) -> Tuple[bool, str]:
    """Build using Gradle."""
    if entry.module and entry.module != '.':
        task = f':{entry.module}:testClasses'
    else:
        task = 'testClasses'
    cmd_parts = [task, '-q', '--no-daemon']
    gradle = _get_local_gradle_cmd(repo_dir)  # 无论本地还是 Docker 都优先使用项目 wrapper。

    if use_docker and docker_image:
        return _run_in_docker(docker_image, repo_dir, [gradle] + cmd_parts, timeout)  # 容器内也沿用 wrapper，减少 Gradle 版本漂移。
    else:
        cmd = [gradle] + cmd_parts
        return _run_local(cmd, repo_dir, timeout)


def _maven_build_cmd(entry: TestEntry) -> list:
    """Get Maven build command arguments (without the mvn binary)."""
    cmd = ['test-compile', '-DskipTests', '--batch-mode']
    if entry.module and entry.module != '.':
        cmd.extend(['-pl', entry.module, '-am'])
    return cmd


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def run_test(repo_dir: str, entry: TestEntry, rerun_count: int,
             mode: RerunMode = RerunMode.ISOLATED,
             use_docker: bool = True,
             timeout: int = 300) -> List[str]:
    """Run the test multiple times and collect results.

    Args:
        repo_dir: Repository root.
        entry: Test entry.
        rerun_count: Number of reruns.
        mode: Rerun mode.
        use_docker: If True, run tests inside Docker.
        timeout: Per-run timeout in seconds.

    Returns:
        List of results: ["pass", "fail", "error", ...]
    """
    build_tool = detect_build_tool(repo_dir, entry.module)

    docker_image = None
    if use_docker:
        if not is_docker_available():
            logger.warning("Docker not available for test run, falling back to local")
            use_docker = False
        else:
            docker_image = get_docker_image(repo_dir, build_tool, entry.module)  # 测试阶段继续沿用模块级镜像选择。

    results = []
    for i in range(rerun_count):
        logger.info(f"  Run {i + 1}/{rerun_count}")

        if build_tool == 'maven':
            result = _run_maven_test(repo_dir, entry, mode, timeout,
                                     use_docker, docker_image)
        else:
            result = _run_gradle_test(repo_dir, entry, mode, timeout,
                                      use_docker, docker_image)

        results.append(result)
        logger.info(f"  Run {i + 1} result: {result}")

    return results


def _run_maven_test(repo_dir: str, entry: TestEntry, mode: RerunMode,
                    timeout: int, use_docker: bool,
                    docker_image: Optional[str]) -> str:
    """Run a single Maven test."""
    test_spec = f"{entry.test_class}#{entry.test_method}"

    cmd_parts = ['test', '--batch-mode', '-fn',
                 f'-Dtest={test_spec}',
                 '-Dsurefire.useFile=false']
    mvn = _get_local_maven_cmd(repo_dir)  # 测试阶段同样优先使用项目 Maven wrapper。

    if entry.module and entry.module != '.':
        cmd_parts.extend(['-pl', entry.module, '-am'])  # 连带构建上游依赖模块，减少多模块测试运行失败。

    if mode == RerunMode.SAME_JVM:
        cmd_parts.extend(['-DforkCount=0', '-DreuseForks=true'])

    try:
        if use_docker and docker_image:
            result = docker_run(docker_image, repo_dir,
                                [mvn] + cmd_parts, timeout=timeout)  # 容器中直接执行 wrapper 命令。
        else:
            result = subprocess.run(
                [mvn] + cmd_parts, cwd=repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=_get_build_env()
            )
        output = result.stdout + '\n' + result.stderr
        return _parse_test_result(result.returncode, output)
    except subprocess.TimeoutExpired:
        logger.warning(f"Test timed out after {timeout}s")
        return "error"
    except Exception as e:
        logger.error(f"Test execution failed: {e}")
        return "error"


def _run_gradle_test(repo_dir: str, entry: TestEntry, mode: RerunMode,
                     timeout: int, use_docker: bool,
                     docker_image: Optional[str]) -> str:
    """Run a single Gradle test."""
    test_filter = f"{entry.test_class}.{entry.test_method}"

    if entry.module and entry.module != '.':
        task = f':{entry.module}:test'
    else:
        task = 'test'

    cmd_parts = [task, '--tests', test_filter, '--no-daemon', '--rerun-tasks']
    gradle = _get_local_gradle_cmd(repo_dir)  # 测试阶段同样优先使用项目 Gradle wrapper。
    if mode == RerunMode.SAME_JVM:
        cmd_parts.extend(['-Dtest.forkEvery=0'])

    try:
        if use_docker and docker_image:
            result = docker_run(docker_image, repo_dir,
                                [gradle] + cmd_parts, timeout=timeout)  # 容器中也执行 wrapper 以保持 Gradle 版本一致。
        else:
            result = subprocess.run(
                [gradle] + cmd_parts, cwd=repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=_get_build_env()
            )
        output = result.stdout + '\n' + result.stderr
        return _parse_test_result(result.returncode, output)
    except subprocess.TimeoutExpired:
        logger.warning(f"Test timed out after {timeout}s")
        return "error"
    except Exception as e:
        logger.error(f"Test execution failed: {e}")
        return "error"


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_test_result(returncode: int, output: str) -> str:
    """Parse test output to determine pass/fail/error.

    - pass: test executed and passed
    - fail: test executed but failed (assertion failure, exception, etc.)
    - error: compilation error, infrastructure issue, test not found
    """
    output_lower = output.lower()

    # Compilation errors → always "error"
    compilation_indicators = [
        'compilation failure', 'compilation error',
        'cannot find symbol',
        'failed to execute goal org.apache.maven.plugins:maven-compiler-plugin',
        'error: cannot access',
        'error: package .* does not exist',
    ]
    if any(ind in output_lower for ind in compilation_indicators):
        if 'tests run:' not in output_lower:
            return "error"

    # Test not found → error
    not_found_indicators = [
        'no tests were executed',
        'no tests found',
        'no tests to run',
        'no tests matched',
    ]
    if any(ind in output_lower for ind in not_found_indicators):
        return "error"

    # Maven Surefire output: Tests run: X, Failures: Y, Errors: Z
    surefire_pattern = re.compile(
        r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)',
        re.IGNORECASE
    )
    matches = surefire_pattern.findall(output)
    if matches:
        total, failures, errors = int(matches[-1][0]), int(matches[-1][1]), int(matches[-1][2])
        if total == 0:
            return "error"
        if failures > 0 or errors > 0:
            return "fail"
        return "pass"

    # Gradle
    if 'build successful' in output_lower:
        return "pass"
    if 'build failed' in output_lower:
        # Distinguish test failure from build failure
        if any(kw in output_lower for kw in ['test fail', 'assertion', 'expected']):
            return "fail"
        return "error"

    # Fallback: return code
    if returncode == 0:
        return "pass"
    return "fail"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_in_docker(image: str, repo_dir: str, cmd: list,
                   timeout: int) -> Tuple[bool, str]:
    """Run a command in Docker and return (success, output)."""
    try:
        result = docker_run(image, repo_dir, cmd, timeout=timeout)
        output = result.stdout + '\n' + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Docker command timed out after {timeout}s"
    except Exception as e:
        return False, f"Docker execution failed: {e}"


def _run_local(cmd: list, repo_dir: str, timeout: int) -> Tuple[bool, str]:
    """Run a command locally and return (success, output)."""
    try:
        result = subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, text=True,
            timeout=timeout, env=_get_build_env()
        )
        output = result.stdout + '\n' + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Build timed out"
    except Exception as e:
        return False, f"Build exception: {e}"


def _get_local_maven_cmd(repo_dir: str) -> str:
    mvnw = os.path.join(repo_dir, 'mvnw')
    if os.path.isfile(mvnw):
        os.chmod(mvnw, 0o755)
        return './mvnw'
    return 'mvn'


def _get_local_gradle_cmd(repo_dir: str) -> str:
    gradlew = os.path.join(repo_dir, 'gradlew')
    if os.path.isfile(gradlew):
        os.chmod(gradlew, 0o755)
        return './gradlew'
    return 'gradle'


def _is_recoverable_build_error(output: str) -> bool:
    output_lower = output.lower()
    recoverable_indicators = [
        'connection timed out', 'connection refused',
        'could not resolve dependencies', 'failed to read artifact descriptor',
        'network is unreachable', 'repository not accessible',
        'transfer failed', 'read timed out',
        'concurrentmodificationexception',
        'lock held by', 'could not acquire lock',
    ]
    return any(ind in output_lower for ind in recoverable_indicators)


def _get_build_env() -> dict:
    env = os.environ.copy()
    env['CI'] = 'true'
    if 'MAVEN_OPTS' not in env:
        env['MAVEN_OPTS'] = '-Xmx2g -Xms512m'
    if 'GRADLE_OPTS' not in env:
        env['GRADLE_OPTS'] = '-Xmx2g -Xms512m'
    return env
