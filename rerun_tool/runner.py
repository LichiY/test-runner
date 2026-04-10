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
from typing import Dict, List, Optional, Tuple  # 导入字典、列表、可选值与元组类型注解。

from .data import RunnerBackend, TestEntry  # 导入统一执行后端枚举与兼容测试条目类型。
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


@dataclass  # 定义重跑阶段的执行摘要。 
class RerunExecutionSummary:  # 该结构专门承载纯 rerun 阶段的结果与耗时统计。 
    results: List[str] = field(default_factory=list)  # 保存每次重跑的原始 pass/fail/error 结果。 
    rerun_elapsed_seconds: float = 0.0  # 保存纯 rerun 阶段的总耗时。 
    checkpoint_rerun_elapsed_seconds: Dict[int, float] = field(default_factory=dict)  # 保存各阶段纯 rerun 的累计耗时。 


@dataclass
class TestRunResult:
    entry: TestEntry
    status: str  # "completed", "clone_failed", "file_not_found", "patch_failed", "build_failed"
    results: List[str] = field(default_factory=list)
    error_message: str = ""
    build_output: str = ""
    total_elapsed_seconds: float = 0.0  # 保存包含克隆、构建与重跑在内的总耗时。
    rerun_elapsed_seconds: float = 0.0  # 保存纯重跑阶段的累计耗时。
    checkpoint_total_elapsed_seconds: Dict[int, float] = field(default_factory=dict)  # 保存各阶段包含构建等在内的累计耗时。
    checkpoint_rerun_elapsed_seconds: Dict[int, float] = field(default_factory=dict)  # 保存各阶段纯重跑累计耗时。

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
        cmd_variants = _get_docker_maven_cmd_variants(repo_dir, cmd_parts)  # Docker 中优先尝试 wrapper，必要时退回镜像自带 mvn。
        return _run_in_docker_variants(docker_image, repo_dir, cmd_variants, timeout)  # 按顺序执行 Docker 命令候选并在需要时回退。
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
        cmd_variants = _get_docker_gradle_cmd_variants(repo_dir, cmd_parts)  # Docker 中优先尝试 wrapper，必要时退回镜像自带 gradle。
        return _run_in_docker_variants(docker_image, repo_dir, cmd_variants, timeout)  # 按顺序执行 Docker 命令候选并在需要时回退。
    else:
        cmd = [gradle] + cmd_parts
        return _run_local(cmd, repo_dir, timeout)


def _maven_build_cmd(entry: TestEntry) -> list:
    """Get Maven build command arguments (without the mvn binary)."""
    cmd = ['test-compile', '-DskipTests', '--batch-mode']  # 使用 test-compile 验证补丁是否可编译。
    cmd.extend(_maven_stability_flags())  # 跳过与目标测试编译无关的质量检查插件，减少外部噪声导致的误失败。
    cmd.extend(_maven_network_flags())  # 为 Maven 依赖下载增加内置重试参数以降低瞬时网络波动影响。
    if entry.module and entry.module != '.':
        cmd.extend(['-pl', entry.module, '-am'])
    return cmd


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def run_test(repo_dir: str, entry: TestEntry, rerun_count: int,  # 根据指定后端多次执行目标测试。 
             mode: RerunMode = RerunMode.ISOLATED,  # 保留现有 JVM 复用模式开关。 
             use_docker: bool = True,  # 控制是否在 Docker 中执行测试。 
             timeout: int = 300,  # 控制单次执行超时时间。 
             runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> List[str]:  # 新增执行后端参数以支持 standard 与 nondex。 
    return run_test_with_summary(repo_dir=repo_dir, entry=entry, rerun_count=rerun_count, mode=mode, use_docker=use_docker, timeout=timeout, runner_backend=runner_backend).results  # 保留旧接口，仅返回原始结果数组。 


def run_test_with_summary(repo_dir: str, entry: TestEntry, rerun_count: int,  # 根据指定后端多次执行目标测试并返回耗时摘要。 
                          mode: RerunMode = RerunMode.ISOLATED,  # 保留现有 JVM 复用模式开关。 
                          use_docker: bool = True,  # 控制是否在 Docker 中执行测试。 
                          timeout: int = 300,  # 控制单次执行超时时间。 
                          runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> RerunExecutionSummary:  # 返回包含结果与耗时的执行摘要。 
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

    results = []  # 保存每次重跑的原始结果。 
    checkpoint_rerun_elapsed_seconds: Dict[int, float] = {}  # 保存阶段性纯 rerun 累计耗时。 
    checkpoint_targets = _checkpoint_targets(rerun_count)  # 预先计算本次执行需要记录的关键阶段。 
    rerun_started_at = time.perf_counter()  # 记录纯 rerun 阶段的起始时间。 
    for i in range(rerun_count):
        logger.info(f"  Run {i + 1}/{rerun_count}")

        if build_tool == 'maven':  # Maven 项目根据执行后端选择 standard 或 NonDex。 
            if runner_backend == RunnerBackend.NONDEX:  # 当用户显式选择 NonDex 时走插件执行路径。 
                result = _run_maven_nondex_test(repo_dir, entry, timeout, use_docker, docker_image)  # 调用 Maven NonDex 执行逻辑。 
            else:  # 其余情况仍然走标准 surefire 重跑逻辑。 
                result = _run_maven_test(repo_dir, entry, mode, timeout, use_docker, docker_image)  # 调用现有 Maven 测试执行逻辑。 
        else:  # Gradle 项目当前仅支持标准重跑后端。 
            if runner_backend == RunnerBackend.NONDEX:  # Gradle 下显式请求 NonDex 时返回错误结果。 
                logger.error("NonDex backend is currently only supported for Maven projects")  # 明确记录当前能力边界。 
                result = "error"  # 返回 error 供上层统一统计。 
            else:  # Gradle 标准模式继续沿用现有逻辑。 
                result = _run_gradle_test(repo_dir, entry, mode, timeout, use_docker, docker_image)  # 调用现有 Gradle 测试执行逻辑。 

        results.append(result)  # 记录当前轮次的原始执行结果。 
        if (i + 1) in checkpoint_targets:  # 命中关键阶段时记录到该轮为止的纯 rerun 累计耗时。 
            checkpoint_rerun_elapsed_seconds[i + 1] = time.perf_counter() - rerun_started_at  # 保存当前阶段的纯 rerun 耗时。 
        logger.info(f"  Run {i + 1} result: {result}")

    rerun_elapsed_seconds = time.perf_counter() - rerun_started_at  # 计算整个纯 rerun 阶段的总耗时。 
    return RerunExecutionSummary(results=results, rerun_elapsed_seconds=rerun_elapsed_seconds, checkpoint_rerun_elapsed_seconds=checkpoint_rerun_elapsed_seconds)  # 返回包含结果与耗时统计的摘要对象。 


def _checkpoint_targets(rerun_count: int) -> List[int]:  # 根据总 rerun 次数生成需要记录的关键阶段。 
    if rerun_count <= 0:  # 非正次数时不生成任何关键阶段。 
        return []  # 直接返回空列表。 
    if rerun_count <= 10:  # 小样本执行只保留最终阶段即可避免列膨胀。 
        return [rerun_count]  # 返回最终阶段。 
    checkpoints = list(range(10, rerun_count + 1, 10))  # 默认每 10 次记录一个阶段。 
    if checkpoints[-1] != rerun_count:  # 若最终次数不是 10 的倍数则补充最终阶段。 
        checkpoints.append(rerun_count)  # 追加最终阶段确保总结果可见。 
    return checkpoints  # 返回按顺序排列的关键阶段列表。 


def _run_maven_test(repo_dir: str, entry: TestEntry, mode: RerunMode,
                    timeout: int, use_docker: bool,
                    docker_image: Optional[str]) -> str:
    """Run a single Maven test."""
    test_spec = f"{entry.test_class}#{entry.test_method}"

    cmd_parts = ['test', '--batch-mode', '-fn',
                 f'-Dtest={test_spec}',
                 '-Dsurefire.useFile=false',
                 '-DfailIfNoTests=false',
                 '-Dsurefire.failIfNoSpecifiedTests=false']
    cmd_parts.extend(_maven_stability_flags(include_test_failure_ignore=True))  # 关闭无关质量插件并让 Maven 保留测试输出供我们自行判定。
    cmd_parts.extend(_maven_network_flags())  # 测试阶段同样启用 Maven 网络重试参数。
    mvn = _get_local_maven_cmd(repo_dir)  # 测试阶段同样优先使用项目 Maven wrapper。

    if entry.module and entry.module != '.':
        cmd_parts.extend(['-pl', entry.module, '-am'])  # 连带构建上游依赖模块，减少多模块测试运行失败。

    if mode == RerunMode.SAME_JVM:
        cmd_parts.extend(['-DforkCount=0', '-DreuseForks=true'])

    try:
        if use_docker and docker_image:
            returncode, output = _run_in_docker_variants(  # Docker 中优先尝试 wrapper，必要时回退到镜像自带 mvn。
                docker_image, repo_dir,
                _get_docker_maven_cmd_variants(repo_dir, cmd_parts),
                timeout
            )
        else:
            result = subprocess.run(
                [mvn] + cmd_parts, cwd=repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=_get_build_env()
            )
            returncode = result.returncode  # 记录本地命令的退出码。
            output = result.stdout + '\n' + result.stderr  # 记录本地命令的组合输出。
        return _parse_test_result(returncode, output)
    except subprocess.TimeoutExpired:
        logger.warning(f"Test timed out after {timeout}s")
        return "error"
    except Exception as e:
        logger.error(f"Test execution failed: {e}")
        return "error"


def _run_maven_nondex_test(repo_dir: str, entry: TestEntry, timeout: int, use_docker: bool, docker_image: Optional[str]) -> str:  # 使用 NonDex 插件执行单次 Maven 测试。 
    test_spec = f"{entry.test_class}#{entry.test_method}"  # 先构造 Maven 可识别的测试选择器。 
    cmd_parts = [  # 按一次外层重跑对应一次 nondexRuns=1 的语义拼接命令。 
        'edu.illinois:nondex-maven-plugin:2.1.7:nondex',  # 指定 FlakyDoctor 使用过的稳定 NonDex 插件版本。 
        '--batch-mode',  # 关闭交互输出便于日志解析。 
        '-fn',  # 允许 Maven 在失败时尽量输出完整上下文。 
        f'-Dtest={test_spec}',  # 只执行目标测试方法。 
        '-DnondexRuns=1',  # 每次外层重跑只触发一次 NonDex 扰动。 
        '-Dsurefire.useFile=false',  # 将 surefire 结果直接写到标准输出。 
        '-DfailIfNoTests=false',  # 关闭未匹配测试导致的 Maven 失败。 
        '-Dsurefire.failIfNoSpecifiedTests=false',  # 关闭多模块上游无匹配测试导致的失败。 
    ]  # 完成 NonDex 核心命令参数构造。 
    cmd_parts.extend(_maven_stability_flags(include_test_failure_ignore=True))  # 复用 Maven 稳定性降噪参数。 
    cmd_parts.extend(_maven_network_flags())  # 复用 Maven 网络重试参数。 
    mvn = _get_local_maven_cmd(repo_dir)  # 继续优先使用项目自带的 Maven wrapper。 
    if entry.module and entry.module != '.':  # 多模块仓库仍然需要限定目标模块并联动上游依赖。 
        cmd_parts.extend(['-pl', entry.module, '-am'])  # 追加 Maven 多模块参数。 
    try:  # 统一捕获超时与基础设施异常。 
        if use_docker and docker_image:  # Docker 模式下继续沿用 wrapper 回退链。 
            returncode, output = _run_in_docker_variants(docker_image, repo_dir, _get_docker_maven_cmd_variants(repo_dir, cmd_parts), timeout)  # 在 Docker 中执行 NonDex 命令。 
        else:  # 本地模式下直接运行 Maven NonDex 命令。 
            result = subprocess.run([mvn] + cmd_parts, cwd=repo_dir, capture_output=True, text=True, timeout=timeout, env=_get_build_env())  # 执行本地 NonDex 测试命令。 
            returncode = result.returncode  # 记录本地命令退出码。 
            output = result.stdout + '\n' + result.stderr  # 合并标准输出与错误输出。 
        return _parse_test_result(returncode, output)  # 复用统一测试结果解析逻辑。 
    except subprocess.TimeoutExpired:  # 将超时统一视为 error。 
        logger.warning(f"NonDex test timed out after {timeout}s")  # 记录 NonDex 执行超时。 
        return "error"  # 返回 error 供上层统计。 
    except Exception as e:  # 捕获其余执行期异常。 
        logger.error(f"NonDex test execution failed: {e}")  # 记录执行失败原因。 
        return "error"  # 返回 error 供上层统计。 


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
            returncode, output = _run_in_docker_variants(  # Docker 中优先尝试 wrapper，必要时回退到镜像自带 gradle。
                docker_image, repo_dir,
                _get_docker_gradle_cmd_variants(repo_dir, cmd_parts),
                timeout
            )
        else:
            result = subprocess.run(
                [gradle] + cmd_parts, cwd=repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=_get_build_env()
            )
            returncode = result.returncode  # 记录本地命令的退出码。
            output = result.stdout + '\n' + result.stderr  # 记录本地命令的组合输出。
        return _parse_test_result(returncode, output)
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
    compilation_patterns = [  # 使用正则兼容中英文编译错误提示以及不同 Maven 输出变体。
        r'compilation failure',  # Maven 英文编译失败。
        r'compilation error',  # Maven 英文编译错误。
        r'编译失败',  # Maven 中文编译失败。
        r'编译错误',  # Maven 中文编译错误。
        r'cannot find symbol',  # Java 英文缺失符号。
        r'找不到符号',  # Java 中文缺失符号。
        r'failed to execute goal org\.apache\.maven\.plugins:maven-compiler-plugin',  # Compiler plugin 失败。
        r'error:\s+cannot access',  # Java 访问错误。
        r'error:\s+package\s+.+\s+does not exist',  # 英文缺失包错误。
        r'程序包.+不存在',  # 中文缺失包错误。
    ]  # 这些模式命中时通常说明测试尚未真正开始执行。
    if any(re.search(pattern, output_lower) for pattern in compilation_patterns):
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

    test_failure_indicators = [  # 没有汇总行时用这些典型文本兜底识别真实测试失败。
        'there are test failures',  # Maven Surefire 常见失败提示。
        '<<< failure!',  # Surefire 明细中的失败标记。
        'comparisonfailure',  # JUnit 断言失败类型。
        'assertionerror',  # 常见断言失败异常类型。
    ]  # 这些标记通常表示测试已经实际执行。
    if any(ind in output_lower for ind in test_failure_indicators):
        return "fail"

    build_failure_patterns = [  # 当没有测试汇总时，这些模式更像构建基础设施错误而非测试断言失败。
        r'build failure',  # Maven/Gradle 构建失败总括。
        r'processing the poms',  # POM 解析失败常见提示。
        r'non-resolvable parent pom',  # 父 POM 无法解析。
        r'could not resolve dependencies',  # 依赖解析失败。
        r'failed to collect dependencies',  # 依赖收集失败。
        r'failed to read artifact descriptor',  # 制品描述符读取失败。
        r'pluginresolutionexception',  # Maven 插件解析失败。
        r'mojofailureexception',  # Mojo 执行失败但未进入测试摘要。
    ]  # 与 FlakyDoctor 类似先识别构建失败，再把剩余返回码交给测试结果兜底。
    if any(re.search(pattern, output_lower) for pattern in build_failure_patterns) and 'tests run:' not in output_lower:
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


def _run_in_docker_variants(image: str, repo_dir: str, cmd_variants: List[list],
                            timeout: int) -> Tuple[bool, str]:  # 按顺序执行多个 Docker 命令候选并在 wrapper 引导失败时回退。
    last_output = "No Docker command variants executed"  # 初始化最后一次输出，便于极端情况下返回错误信息。
    for idx, cmd in enumerate(cmd_variants):  # 依次尝试每个 Docker 命令候选。
        success, output = _run_in_docker(image, repo_dir, cmd, timeout)  # 执行当前候选命令。
        if success:  # 当前候选执行成功时立即返回。
            return True, output  # 返回成功状态与命令输出。
        last_output = output  # 记录当前失败输出，供后续可能的兜底返回使用。
        has_next_variant = idx < len(cmd_variants) - 1  # 判断是否还存在后续候选命令。
        if has_next_variant and _is_wrapper_bootstrap_error(output):  # 只有 wrapper 引导失败时才值得尝试回退命令。
            logger.warning("Wrapper bootstrap failed in Docker, falling back to container-provided build tool")  # 记录发生了 wrapper 引导回退。
            continue  # 继续尝试下一条候选命令。
        return False, output  # 非可回退错误时直接返回当前失败结果。
    return False, last_output  # 所有候选都失败时返回最后一次输出。


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


def _get_docker_maven_cmd_variants(repo_dir: str, cmd_parts: list) -> List[list]:  # 生成 Docker 中 Maven 的执行候选序列。
    preferred_cmd = _get_local_maven_cmd(repo_dir)  # 先沿用本地逻辑判断仓库是否自带 mvnw。
    variants = [[preferred_cmd] + cmd_parts]  # 将首选命令作为第一候选。
    if preferred_cmd != 'mvn':  # 仓库存在 wrapper 时额外追加镜像自带 mvn 作为回退选项。
        variants.append(['mvn'] + cmd_parts)  # 回退到容器镜像内置 Maven，避免 wrapper 分发包下载失败卡死。
    return variants  # 返回按优先级排序的命令候选列表。


def _get_local_gradle_cmd(repo_dir: str) -> str:
    gradlew = os.path.join(repo_dir, 'gradlew')
    if os.path.isfile(gradlew):
        os.chmod(gradlew, 0o755)
        return './gradlew'
    return 'gradle'


def _get_docker_gradle_cmd_variants(repo_dir: str, cmd_parts: list) -> List[list]:  # 生成 Docker 中 Gradle 的执行候选序列。
    preferred_cmd = _get_local_gradle_cmd(repo_dir)  # 先沿用本地逻辑判断仓库是否自带 gradlew。
    variants = [[preferred_cmd] + cmd_parts]  # 将首选命令作为第一候选。
    if preferred_cmd != 'gradle':  # 仓库存在 wrapper 时额外追加镜像自带 gradle 作为回退选项。
        variants.append(['gradle'] + cmd_parts)  # 回退到容器镜像内置 Gradle，避免 wrapper 分发包下载失败卡死。
    return variants  # 返回按优先级排序的命令候选列表。


def _is_recoverable_build_error(output: str) -> bool:
    output_lower = output.lower()
    recoverable_indicators = [
        'connection timed out', 'connection refused',
        'could not resolve dependencies', 'failed to read artifact descriptor',
        'network is unreachable', 'repository not accessible',
        'transfer failed', 'read timed out',
        'concurrentmodificationexception',
        'lock held by', 'could not acquire lock',
        'ssl peer shut down incorrectly', 'remote host terminated the handshake',
        'java.io.eofexception', 'received fatal alert',
        'premature end of content-length delimited message body',
    ]
    return any(ind in output_lower for ind in recoverable_indicators)


def _is_wrapper_bootstrap_error(output: str) -> bool:  # 判断失败是否来自 wrapper 自身的分发包引导阶段。
    output_lower = output.lower()  # 统一转小写以便做大小写无关匹配。
    wrapper_markers = [  # 这些标记通常只会出现在 Maven/Gradle wrapper 引导栈里。
        'org.apache.maven.wrapper', 'defaultdownloader', 'installer.createdist',
        'wrapperexecutor.execute', 'mavenwrappermain.main',
        'gradle wrapper', 'gradle-wrapper', 'could not install gradle distribution',
    ]  # 通过栈信息识别 wrapper 自身失败而非项目编译失败。
    network_markers = [  # 只有同时伴随网络错误时才做回退，以免误吞真实构建问题。
        'ssl peer shut down incorrectly', 'java.io.eofexception',
        'remote host terminated the handshake', 'connection reset',
        'read timed out', 'connection refused',
    ]  # 网络标记用于判断 wrapper 失败是瞬时下载问题。
    return any(marker in output_lower for marker in wrapper_markers) and any(marker in output_lower for marker in network_markers)  # 仅当 wrapper 栈和网络错误同时出现时才判为可回退。


def _maven_network_flags() -> List[str]:  # 为 Maven 下载依赖时增加更稳的网络重试参数。
    return [  # 这些参数由 Maven Wagon 处理，可降低瞬时网络波动对依赖下载的影响。
        '-Dmaven.wagon.http.retryHandler.count=3',  # 开启 Maven 自身的 HTTP 重试机制。
        '-Dmaven.wagon.http.retryHandler.requestSentEnabled=true',  # 允许对请求已发送的场景继续重试。
    ]  # 返回需要追加到 Maven 命令尾部的网络相关参数。


def _maven_stability_flags(include_test_failure_ignore: bool = False) -> List[str]:  # 为 Maven 构建与测试添加保守的稳定性降噪参数。
    flags = [  # 只保留对“编译并运行目标测试”通常无副作用的质量检查跳过项。
        '-Dstyle.color=never',  # 关闭 ANSI 彩色输出，方便后续日志解析。
        '-Drat.skip=true',  # 跳过 Apache RAT 许可证扫描。
        '-Dcheckstyle.skip=true',  # 跳过 Checkstyle 校验。
        '-Denforcer.skip=true',  # 跳过 Enforcer 版本与环境校验。
        '-Dspotbugs.skip=true',  # 跳过 SpotBugs 分析。
        '-Dfindbugs.skip=true',  # 跳过 FindBugs 分析。
        '-Djacoco.skip=true',  # 跳过 JaCoCo 覆盖率任务。
        '-Danimal.sniffer.skip=true',  # 跳过 API 兼容性扫描。
        '-Dspotless.check.skip=true',  # 跳过 Spotless 格式检查。
        '-Ddependency-check.skip=true',  # 跳过依赖漏洞扫描。
        '-Dlicense.skip=true',  # 跳过 license 检查。
        '-Dgpg.skip=true',  # 跳过签名相关任务。
    ]  # 这些参数主要减少与测试本身无关的失败来源。
    if include_test_failure_ignore:  # 仅测试阶段需要让 Maven 即使遇到失败用例也尽量保留完整输出。
        flags.append('-Dmaven.test.failure.ignore=true')  # 让我们可以基于日志自行区分 fail 与 error。
    return flags  # 返回追加到 Maven 命令尾部的稳定性参数。


def _get_build_env() -> dict:
    env = os.environ.copy()
    env['CI'] = 'true'
    if 'MAVEN_OPTS' not in env:
        env['MAVEN_OPTS'] = '-Xmx2g -Xms512m'
    if 'GRADLE_OPTS' not in env:
        env['GRADLE_OPTS'] = '-Xmx2g -Xms512m'
    return env
