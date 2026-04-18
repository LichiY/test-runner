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
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple  # 导入字典、列表、可选值与元组类型注解。

from .data import RunnerBackend, TestEntry  # 导入统一执行后端枚举与兼容测试条目类型。
from .docker import (check_local_jdk, detect_java_version, docker_run, get_docker_image,  # 导入本地 JDK 兼容性检查与 Docker 执行相关能力。
                     is_docker_available, pull_image)  # 继续导入 Docker 可用性检测与镜像拉取能力。

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
    error_outputs: List[str] = field(default_factory=list)  # 保存各次 error 结果对应的关键输出尾部，便于结果 CSV 直接解释 RUN_ERROR。 


@dataclass  # 定义构建与测试阶段统一使用的执行环境决策结果。 
class ExecutionEnvironment:  # 该结构用于在工作流、构建与测试阶段之间传递同一份环境决策。 
    build_tool: str  # 保存当前仓库最终识别出的构建工具。 
    use_docker: bool  # 保存当前是否真正使用 Docker 执行。 
    docker_image: Optional[str] = None  # 保存最终选中的 Docker 镜像。 
    decision_reason: str = ""  # 保存当前环境决策的说明文本。 
    fallback_note: str = ""  # 保存从 Docker 回退到本地时的说明文本。 
    error_message: str = ""  # 保存无法安全确定执行环境时的阻断错误。 


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


def detect_build_tool(repo_dir: str, module: str) -> str:  # 按模块目录向上探测 Maven 或 Gradle，减少多模块仓库误判。
    """Detect Maven or Gradle."""  # 保留原有函数职责说明。 
    for search_dir in _candidate_build_dirs(repo_dir, module):  # 先检查最靠近模块的目录，再逐级回退到仓库根目录。 
        if os.path.isfile(os.path.join(search_dir, 'pom.xml')):  # 命中 pom.xml 时优先判定为 Maven 项目。 
            return 'maven'  # 返回 Maven 构建工具类型。 
        if (os.path.isfile(os.path.join(search_dir, 'build.gradle')) or os.path.isfile(os.path.join(search_dir, 'build.gradle.kts'))):  # 命中 Gradle 构建脚本时判定为 Gradle 项目。 
            return 'gradle'  # 返回 Gradle 构建工具类型。 
    return 'maven'  # 两者都未命中时继续保留 Maven 作为兼容兜底。 


def _candidate_build_dirs(repo_dir: str, module: str) -> List[str]:  # 为构建工具探测生成由近到远的候选目录列表。 
    repo_root = os.path.abspath(repo_dir)  # 先将仓库根目录规范化为绝对路径。 
    candidate_dirs: List[str] = []  # 初始化候选目录列表。 
    if module and module != '.':  # 只有声明了具体模块时才从模块目录开始向上回溯。 
        current_dir = os.path.abspath(os.path.join(repo_root, module))  # 先定位到模块目录本身。 
        while current_dir.startswith(repo_root):  # 仅在仓库根目录内部做向上回溯。 
            if current_dir not in candidate_dirs:  # 避免路径规范化后出现重复目录。 
                candidate_dirs.append(current_dir)  # 记录当前候选目录。 
            if current_dir == repo_root:  # 回溯到仓库根目录后即可停止。 
                break  # 结束目录回溯。 
            parent_dir = os.path.dirname(current_dir)  # 获取当前目录的父目录。 
            if parent_dir == current_dir:  # 理论上的安全保护，防止路径异常造成死循环。 
                break  # 无法继续向上时直接退出。 
            current_dir = parent_dir  # 继续沿着父目录链向上探测。 
    if repo_root not in candidate_dirs:  # 保证仓库根目录至少会被检查一次。 
        candidate_dirs.append(repo_root)  # 将仓库根目录作为最后兜底候选。 
    return candidate_dirs  # 返回按优先级排序的候选目录列表。 


def resolve_execution_environment(repo_dir: str, entry: TestEntry, requested_use_docker: bool, docker_fallback_allowed: bool = True) -> ExecutionEnvironment:  # 统一决定构建与测试阶段到底使用 Docker 还是本地环境。 
    build_tool = detect_build_tool(repo_dir, entry.module)  # 先基于模块路径探测实际构建工具。 
    if not requested_use_docker:  # 工作流已经决定走本地执行时无需再做 Docker 探测。 
        return ExecutionEnvironment(build_tool=build_tool, use_docker=False, decision_reason="Local execution selected before build/test")  # 返回显式本地执行的环境决策。 
    if not is_docker_available():  # Docker 守护进程不可用时根据模式决定是否允许回退。 
        return _resolve_local_fallback_environment(repo_dir=repo_dir, entry=entry, build_tool=build_tool, failure_reason="Docker daemon is unavailable", docker_fallback_allowed=docker_fallback_allowed, docker_image=None)  # 返回严格失败或有条件回退后的环境决策。 
    docker_image = get_docker_image(repo_dir, build_tool, entry.module)  # 在 Docker 可用时先解析当前仓库所需镜像。 
    if not pull_image(docker_image):  # 镜像拉取失败时同样根据模式与本地兼容性决定是否回退。 
        return _resolve_local_fallback_environment(repo_dir=repo_dir, entry=entry, build_tool=build_tool, failure_reason=f"Failed to pull Docker image {docker_image}", docker_fallback_allowed=docker_fallback_allowed, docker_image=docker_image)  # 返回严格失败或有条件回退后的环境决策。 
    return ExecutionEnvironment(build_tool=build_tool, use_docker=True, docker_image=docker_image, decision_reason=f"Using Docker image {docker_image} for {build_tool} execution")  # 返回最终确认使用 Docker 的环境决策。 


def _resolve_local_fallback_environment(repo_dir: str, entry: TestEntry, build_tool: str, failure_reason: str, docker_fallback_allowed: bool, docker_image: Optional[str]) -> ExecutionEnvironment:  # 在 Docker 不可用时决定是否允许安全回退到本地环境。 
    if not docker_fallback_allowed:  # 显式 `--docker always` 时绝不允许静默回退。 
        return ExecutionEnvironment(build_tool=build_tool, use_docker=False, docker_image=docker_image, decision_reason=failure_reason, error_message=f"{failure_reason}; Docker execution was explicitly required")  # 返回明确的环境阻断错误。 
    fallback_allowed, fallback_note = _local_fallback_note(repo_dir=repo_dir, module=entry.module, failure_reason=failure_reason)  # 基于 Java 版本兼容性判断当前是否可以安全本地回退。 
    if not fallback_allowed:  # 无法确认本地环境兼容时不再冒险回退。 
        return ExecutionEnvironment(build_tool=build_tool, use_docker=False, docker_image=docker_image, decision_reason=failure_reason, error_message=fallback_note)  # 返回阻断当前执行的详细环境错误。 
    logger.warning(fallback_note)  # 记录本次从 Docker 回退到本地的原因。 
    return ExecutionEnvironment(build_tool=build_tool, use_docker=False, docker_image=None, decision_reason=fallback_note, fallback_note=fallback_note)  # 返回经过兼容性校验后允许本地回退的环境决策。 


def _local_fallback_note(repo_dir: str, module: str, failure_reason: str) -> Tuple[bool, str]:  # 生成 Docker 回退到本地时的说明或阻断原因。 
    java_version = detect_java_version(repo_dir, module)  # 先按模块路径探测当前仓库声明的 Java 版本。 
    if not java_version:  # 无法识别 Java 版本时不再盲目回退到本地。 
        return False, f"{failure_reason}; local fallback blocked because Java version could not be detected"  # 返回无法安全回退的阻断消息。 
    if not check_local_jdk(java_version):  # 本地 JDK 与项目要求不兼容时也不允许回退。 
        return False, f"{failure_reason}; local fallback blocked because local JDK is incompatible with Java {java_version}"  # 返回本地 JDK 不兼容的阻断消息。 
    return True, f"{failure_reason}; falling back to local execution because local JDK is compatible with Java {java_version}"  # 返回允许安全回退到本地的说明文本。 


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_project(repo_dir: str, entry: TestEntry, use_docker: bool = True, timeout: int = 600, max_retries: int = 2, execution_env: Optional[ExecutionEnvironment] = None, docker_fallback_allowed: bool = True) -> Tuple[bool, str]:  # 构建项目并支持复用统一的环境决策结果。
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
    resolved_env = execution_env or resolve_execution_environment(repo_dir=repo_dir, entry=entry, requested_use_docker=use_docker, docker_fallback_allowed=docker_fallback_allowed)  # 优先复用工作流已算好的环境决策，否则在本地补算。 
    if resolved_env.error_message:  # 环境无法安全确定时直接阻断当前构建。 
        logger.error(resolved_env.error_message)  # 记录当前被阻断的环境错误。 
        return False, resolved_env.error_message  # 将环境错误作为构建失败原因返回。 
    build_tool = resolved_env.build_tool  # 复用统一环境决策里的构建工具结论。 
    use_docker = resolved_env.use_docker  # 复用统一环境决策里的执行环境开关。 
    docker_image = resolved_env.docker_image  # 复用统一环境决策里的 Docker 镜像。 
    if resolved_env.decision_reason:  # 仅在存在环境说明时打印日志。 
        logger.info(f"Build environment: {resolved_env.decision_reason}")  # 记录当前构建阶段最终采用的环境。 

    for attempt in range(max_retries + 1):
        attempt_timeout = timeout * max(1, attempt + 1)  # 在重试时逐步放宽超时，给首次依赖下载或大模块增量编译留出更多时间。
        if attempt > 0:
            logger.info(f"Build retry attempt {attempt}/{max_retries} with timeout {attempt_timeout}s")
            time.sleep(2)

        if build_tool == 'maven':
            success, output = _build_maven(repo_dir, entry, attempt_timeout,
                                           use_docker, docker_image)
        else:
            success, output = _build_gradle(repo_dir, entry, attempt_timeout,
                                            use_docker, docker_image)

        if success:
            return True, output

        if build_tool == 'maven':  # 少数 Maven 项目在 test-compile 阶段还需要一次项目特定的预构建恢复。
            recovery_triggered, recovery_success, recovery_output = _attempt_special_maven_recovery(repo_dir=repo_dir, entry=entry, use_docker=use_docker, docker_image=docker_image, timeout=attempt_timeout, build_output=output)  # 仅在命中已知构建链缺口时执行一次额外恢复。
            if recovery_triggered:  # 命中恢复分支时以恢复后的结果为准，不再继续普通重试。
                if recovery_success:  # 预构建恢复后如果已经成功则直接返回。
                    return True, recovery_output  # 返回恢复后的成功输出。
                output = recovery_output  # 恢复仍失败时用增强后的诊断输出覆盖当前失败日志。

        if attempt < max_retries and _is_recoverable_build_error(output):
            logger.info("Recoverable build error detected, retrying...")
            continue

        if resolved_env.fallback_note:  # 当构建是在 Docker 失败后回退到本地时补充环境说明。 
            return False, f"{resolved_env.fallback_note}\n{output}"  # 将回退说明拼接到最终失败输出里。 
        return False, output  # 其余场景直接返回原始构建输出。 

    return False, "Build failed after all retries"


def _build_maven(repo_dir: str, entry: TestEntry, timeout: int,
                 use_docker: bool, docker_image: Optional[str]) -> Tuple[bool, str]:
    """Build using Maven."""
    cmd_parts = _maven_build_cmd(repo_dir, entry)
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


def _maven_build_cmd(repo_dir: str, entry: TestEntry) -> list:
    """Get Maven build command arguments (without the mvn binary)."""
    cmd = _maven_cli_args(repo_dir) + ['test-compile', '-DskipTests', '--batch-mode']  # 使用隔离后的 Maven 设置和 test-compile 验证补丁是否可编译。
    cmd.extend(_maven_stability_flags())  # 跳过与目标测试编译无关的质量检查插件，减少外部噪声导致的误失败。
    cmd.extend(_maven_network_flags())  # 为 Maven 依赖下载增加内置重试参数以降低瞬时网络波动影响。
    cmd.extend(_maven_project_flags(repo_dir, entry))  # 为少数依赖 os-classifier 或特殊本机参数的项目补入保守且可复现的系统属性。
    if entry.module and entry.module != '.':
        cmd.extend(['-pl', entry.module, '-am'])
    return cmd


def _maven_preinstall_cmd(repo_dir: str, module: str, entry: Optional[TestEntry] = None) -> list:  # 为需要先落地上游产物的项目生成一次性的 Maven install 命令。
    cmd = _maven_cli_args(repo_dir) + ['install', '-DskipTests', '--batch-mode']  # 通过 install 将上游模块产物放入隔离本地仓库供目标模块复用。
    cmd.extend(_maven_stability_flags())  # 继续跳过与当前问题无关的质量检查插件。
    cmd.extend(_maven_network_flags())  # 继续复用 Maven 网络重试参数。
    if entry is not None:  # 仅在拿到项目条目时才尝试补入项目特定系统属性。
        cmd.extend(_maven_project_flags(repo_dir, entry))  # 例如 os.detected.classifier 这类项目级参数。
    if module and module != '.':  # 有明确预构建模块时同步附加多模块参数。
        cmd.extend(['-pl', module, '-am'])  # 让 Maven 一并构建该模块所需的上游依赖。
    return cmd


def _maven_project_flags(repo_dir: str, entry: TestEntry) -> List[str]:  # 为少数需要额外系统属性的 Maven 项目追加保守且可复现的命令参数。
    flags: List[str] = []  # 保存当前项目需要额外追加的 Maven 参数。
    classifier_override = _preferred_os_classifier(repo_dir)  # 仅在仓库显式依赖 `${os.detected.classifier}` 时才生成覆盖值。
    if classifier_override:  # 命中 classifier 覆盖场景时再追加参数。
        flags.append(f'-Dos.detected.classifier={classifier_override}')  # 避免 os-maven-plugin 在容器中误判成 linux-x86_32。
    return flags  # 返回当前项目需要额外追加的 Maven 参数列表。


def _preferred_os_classifier(repo_dir: str) -> str:  # 为依赖 os-maven-plugin 的仓库生成稳定的 Linux classifier。
    if not _repo_uses_os_classifier(repo_dir):  # 仓库没有显式依赖 `${os.detected.classifier}` 时不追加任何覆盖。
        return ''  # 返回空串表示无需系统属性覆盖。
    architecture = platform.machine().lower()  # 读取当前宿主或 Docker 所在平台的机器架构。
    classifier_by_arch = {  # 当前先覆盖当前失败集中实际出现的主流 Linux 架构。
        'x86_64': 'linux-x86_64',
        'amd64': 'linux-x86_64',
        'aarch64': 'linux-aarch_64',
        'arm64': 'linux-aarch_64',
    }  # 对未知架构保持保守，不做额外覆盖。
    return classifier_by_arch.get(architecture, '')  # 仅在已知架构下返回稳定的 Linux classifier。


def _repo_uses_os_classifier(repo_dir: str) -> bool:  # 判断仓库是否显式依赖 `${os.detected.classifier}` 一类平台相关属性。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和无关目录。
    required_markers = ('${os.detected.classifier}', 'os-maven-plugin')  # 只有同时命中 classifier 属性或扩展时才值得覆盖。
    optional_markers = ('netty-tcnative',)  # 当前失败集中 `timely` 使用该依赖最容易被错误 classifier 卡住。
    for root, dirs, files in os.walk(repo_dir):  # 遍历当前仓库目录树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 裁剪无关目录减少扫描开销。
        if 'pom.xml' not in files:  # 当前目录没有 pom.xml 时无需读取。
            continue  # 跳过当前目录。
        pom_path = os.path.join(root, 'pom.xml')  # 拼出当前 pom 文件路径。
        try:  # 个别 pom 读取失败时直接跳过即可。
            with open(pom_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取 pom 文本。
                pom_text = f.read()  # 读取完整 pom 内容供后续文本判断。
        except Exception:  # 读取失败时不影响其他 pom 的扫描。
            continue  # 继续处理后续目录。
        if all(marker in pom_text for marker in required_markers) or ('${os.detected.classifier}' in pom_text and any(marker in pom_text for marker in optional_markers)):  # 命中显式 classifier 属性时即可认定需要稳定覆盖。
            return True  # 当前仓库显式依赖平台 classifier。
    return False  # 扫描完仍未命中时说明无需覆盖。


def _attempt_special_maven_recovery(repo_dir: str, entry: TestEntry, use_docker: bool, docker_image: Optional[str], timeout: int, build_output: str) -> Tuple[bool, bool, str]:  # 针对少数已确认的 Maven 构建链问题执行一次额外恢复。
    if _needs_seatunnel_shade_preinstall(repo_dir, entry, build_output):  # 当前失败模式说明 target reactor 还没有把 shaded 产物完整 install 到隔离本地仓库。
        recovery_module = entry.module if entry.module and entry.module != '.' else 'seatunnel-api'  # 优先沿用当前目标模块，让 Maven 自己把所需上游链路一起 install。
        logger.warning(f"Detected shaded-module build gap, installing the full target reactor for {recovery_module} before retrying test-compile")  # 记录当前进入 seatunnel 特定恢复分支。
        preinstall_success, preinstall_output = _run_maven_auxiliary_goal(repo_dir=repo_dir, entry=entry, cmd_parts=_maven_preinstall_cmd(repo_dir, recovery_module, entry), timeout=timeout, use_docker=use_docker, docker_image=docker_image)  # 先把目标模块及其上游依赖完整 install 到隔离仓库。
        if not preinstall_success:  # 预构建本身失败时直接返回增强后的诊断输出。
            combined_output = f"{build_output}\n\nTarget-reactor install failed for {recovery_module}:\n{preinstall_output}"  # 将恢复失败信息追加到原始构建日志之后。
            return True, False, combined_output  # 告诉上层已命中恢复分支但恢复仍然失败。
        rebuilt_success, rebuilt_output = _build_maven(repo_dir, entry, timeout, use_docker, docker_image)  # 预构建成功后重新执行原目标模块构建。
        if rebuilt_success:  # 重试构建成功时直接返回新的成功输出。
            return True, True, rebuilt_output  # 返回恢复成功结果。
        combined_output = f"{build_output}\n\nTarget-reactor install succeeded for {recovery_module} but target build still failed:\n{rebuilt_output}"  # 保留原始失败上下文和恢复后的新日志。
        return True, False, combined_output  # 告诉上层恢复已尝试但仍然失败。
    return False, False, build_output  # 未命中任何特殊恢复场景时保持原始结果不变。


def _needs_seatunnel_shade_preinstall(repo_dir: str, entry: TestEntry, build_output: str) -> bool:  # 判断当前失败是否属于 seatunnel 上游 shaded 模块未 install 的经典场景。
    shade_pom = os.path.join(repo_dir, 'seatunnel-config', 'seatunnel-config-shade', 'pom.xml')  # seatunnel 的 shaded 配置模块路径相对稳定。
    if not os.path.isfile(shade_pom):  # 当前仓库不存在该模块时直接返回假。
        return False  # 说明不是 seatunnel 这类场景。
    output = build_output or ''  # 统一处理空日志场景。
    required_markers = ('seatunnel-config-shade', 'ConfigParser.java')  # 失败日志同时命中 shaded 模块和 ConfigParser 才是当前已确认的根因。
    missing_symbol_markers = ('AbstractConfigValue', 'ConfigNodeRoot', 'FullIncluder', 'ConfigIncludeContext', 'ConfigSyntax')  # 这些缺失符号都指向 shade 产物没有先生成。
    if all(marker in output for marker in required_markers) and any(marker in output for marker in missing_symbol_markers):  # 历史 seatunnel shade 模块未 install 的老问题仍然继续保留。
        return True
    shaded_test_markers = ('org.apache.seatunnel.shade.', f'{os.sep}src{os.sep}test{os.sep}')  # v8 暴露出来的新症状是测试源码直接缺少 shaded 包。
    return all(marker in output for marker in shaded_test_markers)  # 一旦命中 shaded 包缺失的测试编译错误，也说明需要先把目标 reactor install 到隔离仓库。


def _run_maven_auxiliary_goal(repo_dir: str, entry: TestEntry, cmd_parts: list, timeout: int, use_docker: bool, docker_image: Optional[str]) -> Tuple[bool, str]:  # 执行一次不直接面向目标测试的辅助 Maven 命令。
    mvn = _get_local_maven_cmd(repo_dir)  # 与主构建路径保持一致，优先使用项目自带 mvnw。
    if use_docker and docker_image:  # Docker 模式下继续沿用 wrapper 回退链和参数改写逻辑。
        cmd_variants = _get_docker_maven_cmd_variants(repo_dir, cmd_parts)  # 为容器内命令生成候选序列。
        return _run_in_docker_variants(docker_image, repo_dir, cmd_variants, timeout)  # 在 Docker 中执行辅助 Maven 命令。
    return _run_local([mvn] + cmd_parts, repo_dir, timeout)  # 本地模式下直接执行辅助 Maven 命令。


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def run_test(repo_dir: str, entry: TestEntry, rerun_count: int,  # 根据指定后端多次执行目标测试。 
             mode: RerunMode = RerunMode.ISOLATED,  # 保留现有 JVM 复用模式开关。 
             use_docker: bool = True,  # 控制是否在 Docker 中执行测试。 
             timeout: int = 300,  # 控制单次执行超时时间。 
             runner_backend: RunnerBackend = RunnerBackend.STANDARD) -> List[str]:  # 新增执行后端参数以支持 standard 与 nondex。 
    return run_test_with_summary(repo_dir=repo_dir, entry=entry, rerun_count=rerun_count, mode=mode, use_docker=use_docker, timeout=timeout, runner_backend=runner_backend).results  # 保留旧接口，仅返回原始结果数组。 


def run_test_with_summary(repo_dir: str, entry: TestEntry, rerun_count: int, mode: RerunMode = RerunMode.ISOLATED, use_docker: bool = True, timeout: int = 300, runner_backend: RunnerBackend = RunnerBackend.STANDARD, execution_env: Optional[ExecutionEnvironment] = None, docker_fallback_allowed: bool = True) -> RerunExecutionSummary:  # 根据统一环境决策多次执行目标测试并返回耗时摘要。 
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
    resolved_env = execution_env or resolve_execution_environment(repo_dir=repo_dir, entry=entry, requested_use_docker=use_docker, docker_fallback_allowed=docker_fallback_allowed)  # 优先复用工作流已算好的环境决策，否则在本地补算。 
    build_tool = resolved_env.build_tool  # 复用统一环境决策里的构建工具结论。 
    use_docker = resolved_env.use_docker  # 复用统一环境决策里的执行环境开关。 
    docker_image = resolved_env.docker_image  # 复用统一环境决策里的 Docker 镜像。 
    if resolved_env.decision_reason:  # 仅在存在环境说明时输出日志。 
        logger.info(f"Test environment: {resolved_env.decision_reason}")  # 记录当前测试阶段最终采用的环境。 

    results = []  # 保存每次重跑的原始结果。 
    error_outputs: List[str] = []  # 保存每次 error 结果对应的关键输出尾部。 
    checkpoint_rerun_elapsed_seconds: Dict[int, float] = {}  # 保存阶段性纯 rerun 累计耗时。 
    checkpoint_targets = _checkpoint_targets(rerun_count)  # 预先计算本次执行需要记录的关键阶段。 
    if resolved_env.error_message:  # 测试阶段如果环境无法安全确定则直接返回 error 结果数组。 
        logger.error(resolved_env.error_message)  # 记录测试阶段的环境阻断错误。 
        for checkpoint in checkpoint_targets:  # 为所有关键阶段补上 0 秒耗时以保持结果结构完整。 
            checkpoint_rerun_elapsed_seconds[checkpoint] = 0.0  # 当前没有真正进入 rerun 阶段，因此耗时恒为 0。 
        return RerunExecutionSummary(results=['error'] * rerun_count, rerun_elapsed_seconds=0.0, checkpoint_rerun_elapsed_seconds=checkpoint_rerun_elapsed_seconds, error_outputs=[resolved_env.error_message])  # 返回全 error 的执行摘要并保留环境错误。 
    rerun_started_at = time.perf_counter()  # 记录纯 rerun 阶段的起始时间。 
    for i in range(rerun_count):
        logger.info(f"  Run {i + 1}/{rerun_count}")

        run_output = ''  # 保存当前轮次的测试执行输出，供 error 诊断落盘。 
        if build_tool == 'maven':  # Maven 项目根据执行后端选择 standard 或 NonDex。 
            if runner_backend == RunnerBackend.NONDEX:  # 当用户显式选择 NonDex 时走插件执行路径。 
                result, run_output = _run_maven_nondex_test_with_output(repo_dir, entry, timeout, use_docker, docker_image)  # 调用 Maven NonDex 执行逻辑并保留关键输出。 
            else:  # 其余情况仍然走标准 surefire 重跑逻辑。 
                result, run_output = _run_maven_test_with_output(repo_dir, entry, mode, timeout, use_docker, docker_image)  # 调用现有 Maven 测试执行逻辑并保留关键输出。 
        else:  # Gradle 项目当前仅支持标准重跑后端。 
            if runner_backend == RunnerBackend.NONDEX:  # Gradle 下显式请求 NonDex 时返回错误结果。 
                logger.error("NonDex backend is currently only supported for Maven projects")  # 明确记录当前能力边界。 
                result = "error"  # 返回 error 供上层统一统计。 
                run_output = "NonDex backend is currently only supported for Maven projects"  # 保留当前能力边界错误便于结果 CSV 解释 RUN_ERROR。 
            else:  # Gradle 标准模式继续沿用现有逻辑。 
                result, run_output = _run_gradle_test_with_output(repo_dir, entry, mode, timeout, use_docker, docker_image)  # 调用现有 Gradle 测试执行逻辑并保留关键输出。 

        results.append(result)  # 记录当前轮次的原始执行结果。 
        if result == "error" and run_output.strip():  # 只有真正的 error 结果才需要额外保存诊断输出。 
            error_outputs.append(_tail_command_output(run_output))  # 仅保留输出尾部关键片段，避免结果对象过大。 
        if (i + 1) in checkpoint_targets:  # 命中关键阶段时记录到该轮为止的纯 rerun 累计耗时。 
            checkpoint_rerun_elapsed_seconds[i + 1] = time.perf_counter() - rerun_started_at  # 保存当前阶段的纯 rerun 耗时。 
        logger.info(f"  Run {i + 1} result: {result}")

    rerun_elapsed_seconds = time.perf_counter() - rerun_started_at  # 计算整个纯 rerun 阶段的总耗时。 
    return RerunExecutionSummary(results=results, rerun_elapsed_seconds=rerun_elapsed_seconds, checkpoint_rerun_elapsed_seconds=checkpoint_rerun_elapsed_seconds, error_outputs=error_outputs)  # 返回包含结果、耗时统计与 error 诊断的摘要对象。 


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
    result, _ = _run_maven_test_with_output(repo_dir, entry, mode, timeout, use_docker, docker_image)  # 复用带输出版本并保持旧接口返回纯结果字符串。
    return result  # 返回当前轮次的 pass/fail/error 结果。


def _run_maven_test_with_output(repo_dir: str, entry: TestEntry, mode: RerunMode, timeout: int, use_docker: bool, docker_image: Optional[str]) -> Tuple[str, str]:  # 执行单次 Maven 测试并同时返回结果与关键输出。
    """Run a single Maven test and keep output for diagnostics."""
    test_spec = f"{entry.test_class}#{entry.test_method}"

    cmd_parts = _maven_cli_args(repo_dir) + ['test', '--batch-mode', '-fn',
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
            docker_success, output = _run_in_docker_variants(  # Docker 中优先尝试 wrapper，必要时回退到镜像自带 mvn。
                docker_image, repo_dir,
                _get_docker_maven_cmd_variants(repo_dir, cmd_parts),
                timeout
            )
            returncode = 0 if docker_success else 1  # 将 Docker 执行结果显式转换为整数退出码，避免布尔值被误当成 0/1 造成回退判定错误。
        else:
            result = subprocess.run(
                [mvn] + cmd_parts, cwd=repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=_get_build_env(repo_dir)
            )
            returncode = result.returncode  # 记录本地命令的退出码。
            output = result.stdout + '\n' + result.stderr  # 记录本地命令的组合输出。
        return _parse_test_result(returncode, output), output  # 同时返回解析结果与组合输出。
    except subprocess.TimeoutExpired:  # 超时时将其记为 error 并保留说明文本。
        logger.warning(f"Test timed out after {timeout}s")  # 记录 Maven 测试执行超时。
        return "error", f"Test timed out after {timeout}s"  # 返回 error 与超时说明。
    except Exception as e:  # 捕获其余测试执行异常。
        logger.error(f"Test execution failed: {e}")  # 记录测试执行失败原因。
        return "error", f"Test execution failed: {e}"  # 返回 error 与异常说明。


def _run_maven_nondex_test(repo_dir: str, entry: TestEntry, timeout: int, use_docker: bool, docker_image: Optional[str]) -> str:  # 使用 NonDex 插件执行单次 Maven 测试。 
    result, _ = _run_maven_nondex_test_with_output(repo_dir, entry, timeout, use_docker, docker_image)  # 复用带输出版本并保持旧接口返回纯结果字符串。 
    return result  # 返回当前轮次的 pass/fail/error 结果。 


def _run_maven_nondex_test_with_output(repo_dir: str, entry: TestEntry, timeout: int, use_docker: bool, docker_image: Optional[str]) -> Tuple[str, str]:  # 使用 NonDex 插件执行单次 Maven 测试并返回结果与关键输出。 
    test_spec = f"{entry.test_class}#{entry.test_method}"  # 先构造 Maven 可识别的测试选择器。 
    cmd_parts = _maven_cli_args(repo_dir) + [  # 按一次外层重跑对应一次 nondexRuns=1 的语义拼接命令。 
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
            docker_success, output = _run_in_docker_variants(docker_image, repo_dir, _get_docker_maven_cmd_variants(repo_dir, cmd_parts), timeout)  # 在 Docker 中执行 NonDex 命令。 
            returncode = 0 if docker_success else 1  # 将 Docker 执行结果显式转换为整数退出码，避免布尔值影响结果解析。 
        else:  # 本地模式下直接运行 Maven NonDex 命令。 
            result = subprocess.run([mvn] + cmd_parts, cwd=repo_dir, capture_output=True, text=True, timeout=timeout, env=_get_build_env(repo_dir))  # 执行本地 NonDex 测试命令。 
            returncode = result.returncode  # 记录本地命令退出码。 
            output = result.stdout + '\n' + result.stderr  # 合并标准输出与错误输出。 
        return _parse_test_result(returncode, output), output  # 复用统一测试结果解析逻辑并保留输出。 
    except subprocess.TimeoutExpired:  # 将超时统一视为 error。 
        logger.warning(f"NonDex test timed out after {timeout}s")  # 记录 NonDex 执行超时。 
        return "error", f"NonDex test timed out after {timeout}s"  # 返回 error 与超时说明。 
    except Exception as e:  # 捕获其余执行期异常。 
        logger.error(f"NonDex test execution failed: {e}")  # 记录执行失败原因。 
        return "error", f"NonDex test execution failed: {e}"  # 返回 error 与异常说明。 


def _run_gradle_test(repo_dir: str, entry: TestEntry, mode: RerunMode,
                     timeout: int, use_docker: bool,
                     docker_image: Optional[str]) -> str:
    """Run a single Gradle test."""
    result, _ = _run_gradle_test_with_output(repo_dir, entry, mode, timeout, use_docker, docker_image)  # 复用带输出版本并保持旧接口返回纯结果字符串。
    return result  # 返回当前轮次的 pass/fail/error 结果。


def _run_gradle_test_with_output(repo_dir: str, entry: TestEntry, mode: RerunMode, timeout: int, use_docker: bool, docker_image: Optional[str]) -> Tuple[str, str]:  # 执行单次 Gradle 测试并同时返回结果与关键输出。
    """Run a single Gradle test and keep output for diagnostics."""
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
            docker_success, output = _run_in_docker_variants(  # Docker 中优先尝试 wrapper，必要时回退到镜像自带 gradle。
                docker_image, repo_dir,
                _get_docker_gradle_cmd_variants(repo_dir, cmd_parts),
                timeout
            )
            returncode = 0 if docker_success else 1  # 将 Docker 执行结果显式转换为整数退出码，避免布尔值干扰后续解析逻辑。
        else:
            result = subprocess.run(
                [gradle] + cmd_parts, cwd=repo_dir,
                capture_output=True, text=True, timeout=timeout,
                env=_get_build_env(repo_dir)
            )
            returncode = result.returncode  # 记录本地命令的退出码。
            output = result.stdout + '\n' + result.stderr  # 记录本地命令的组合输出。
        return _parse_test_result(returncode, output), output  # 同时返回解析结果与组合输出。
    except subprocess.TimeoutExpired:  # 超时时将其记为 error 并保留说明文本。
        logger.warning(f"Test timed out after {timeout}s")  # 记录 Gradle 测试执行超时。
        return "error", f"Test timed out after {timeout}s"  # 返回 error 与超时说明。
    except Exception as e:  # 捕获其余测试执行异常。
        logger.error(f"Test execution failed: {e}")  # 记录测试执行失败原因。
        return "error", f"Test execution failed: {e}"  # 返回 error 与异常说明。


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

    failure_summary_pattern = re.compile(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)', re.IGNORECASE)  # 统一提取 surefire 风格的测试汇总行，后续多处分支都会复用。
    failure_summaries = [(int(total), int(failures), int(errors)) for total, failures, errors in failure_summary_pattern.findall(output)]  # 先把所有测试汇总行解析出来，避免多模块场景被后续 BUILD FAILURE 噪声误导。
    if any((failures > 0 or errors > 0) and total > 0 for total, failures, errors in failure_summaries):  # 只要真正执行过的测试汇总行里出现失败或错误，就应当明确归类为测试失败。
        return "fail"  # 这里是断言失败或测试逻辑错误，不是运行基础设施异常。
    if any(total > 0 for total, _, _ in failure_summaries):  # 已经真正执行过至少一个目标测试且没有失败时，应当优先视为通过。
        return "pass"  # 即使后面因为 reactor 汇总或其他模块非关键噪声返回非零退出码，也不应误判成 RUN_ERROR。

    explicit_test_failure_indicators = [  # 当汇总行被裁掉时，再用这些典型文本兜底识别真实测试失败。
        'there are test failures',  # Maven Surefire 常见的统一失败提示。
        'failed to execute goal org.apache.maven.plugins:maven-surefire-plugin',  # Surefire 插件自身抛出的失败头。
        'failed to execute goal org.gradle.api.tasks.testing',  # Gradle 测试任务失败头。
        '<<< failure!',  # Surefire 明细中的失败标记。
        'comparisonfailure',  # JUnit 断言失败类型。
        'assertionerror',  # 常见断言失败异常类型。
        'failures:',  # 有些日志尾部只有 `[ERROR] Failures:` 而没有完整汇总行。
    ]  # 这些标记通常说明测试已经进入执行阶段，而不是依赖解析或编译阶段。
    if any(ind in output_lower for ind in explicit_test_failure_indicators):  # 命中这些文本时优先认定为 fail。
        return "fail"  # 将真实测试失败和 RUN_ERROR 明确区分。

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
    if failure_summaries:  # 走到这里说明汇总行存在，但全部都是 `Tests run: 0` 之类未真正执行目标测试的情况。
        return "error"  # 明确标记为 RUN_ERROR，避免把“没跑到测试”误算成通过。

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
            timeout=timeout, env=_get_build_env(repo_dir)
        )
        output = result.stdout + '\n' + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Build timed out"
    except Exception as e:
        return False, f"Build exception: {e}"


def _tail_command_output(output: str, limit: int = 4000) -> str:  # 仅保留命令输出尾部以避免结果对象过大。
    normalized_output = (output or '').strip()  # 先统一去掉空值与首尾空白。
    if len(normalized_output) <= limit:  # 短输出无需再裁剪。
        return normalized_output  # 直接返回完整输出。
    focused_output = _extract_interesting_output_window(normalized_output)  # 优先尝试围绕真正的错误标记提取更有信息量的输出窗口。
    if focused_output:  # 如果成功定位到更相关的输出片段则优先返回它。
        return focused_output if len(focused_output) <= limit else focused_output[:limit]  # 聚焦窗口已经围绕关键标记裁好时，优先保留窗口前部避免丢失解释性摘要。
    return normalized_output[-limit:]  # 无法识别关键标记时退回到简单尾部截断。


def _extract_interesting_output_window(output: str, context_before: int = 8, context_after: int = 12) -> str:  # 围绕关键测试错误标记提取更有诊断价值的输出片段。
    marker_candidates = [  # 这些标记通常比单纯的日志尾部更能解释 RUN_ERROR 的真实原因。
        'tests run:',  # Surefire 汇总行通常直接包含 0 tests 或 failure/error 数量。
        'no tests were executed',  # Maven/Surefire 未执行任何测试。
        'no tests found',  # 测试筛选未命中。
        'no tests to run',  # Gradle/Maven 没有要执行的测试。
        'no tests matched',  # 测试过滤条件未匹配到目标。
        'there are test failures',  # 测试失败但未必被归类为 RUN_ERROR，也仍然值得优先展示。
        'compilation failure',  # 测试执行阶段再次触发编译错误时应优先展示该区域。
        'cannot find symbol',  # Java 缺失符号是最常见的执行期编译错误。
        '找不到符号',  # 中文 Maven 输出中的缺失符号提示。
        'failed to execute goal',  # Maven 目标执行失败时的统一错误头。
    ]  # 完成关键输出标记列表定义。
    lines = output.splitlines()  # 按行拆分输出便于围绕关键标记提取窗口。
    last_match_index = -1  # 记录最后一次命中的关键标记行位置。
    for idx, line in enumerate(lines):  # 逐行扫描整个命令输出。
        lowered_line = line.lower()  # 统一转小写以便做大小写无关匹配。
        if any(marker in lowered_line for marker in marker_candidates):  # 命中任一关键标记时记录当前位置。
            last_match_index = idx  # 持续覆盖以保留最后一个更接近最终失败原因的标记。
    if last_match_index == -1:  # 没有命中任何关键标记时返回空串。
        return ''  # 交由上层退回到简单尾部截断。
    start_idx = max(0, last_match_index - context_before)  # 在关键标记前保留少量上下文帮助理解阶段位置。
    end_idx = min(len(lines), last_match_index + context_after + 1)  # 在关键标记后保留一小段错误尾部和后续说明。
    return '\n'.join(lines[start_idx:end_idx]).strip()  # 返回围绕关键标记截取出的聚焦输出窗口。


def _get_local_maven_cmd(repo_dir: str) -> str:
    mvnw = os.path.join(repo_dir, 'mvnw')
    if os.path.isfile(mvnw):
        os.chmod(mvnw, 0o755)
        return './mvnw'
    return 'mvn'


def _get_docker_maven_cmd_variants(repo_dir: str, cmd_parts: list) -> List[list]:  # 生成 Docker 中 Maven 的执行候选序列。
    preferred_cmd = _get_local_maven_cmd(repo_dir)  # 先沿用本地逻辑判断仓库是否自带 mvnw。
    docker_cmd_parts = _dockerize_maven_cmd_parts(repo_dir, cmd_parts)  # 将宿主机绝对路径参数改写成容器内可见路径。
    variants = [[preferred_cmd] + docker_cmd_parts]  # 将首选命令作为第一候选。
    if preferred_cmd != 'mvn':  # 仓库存在 wrapper 时额外追加镜像自带 mvn 作为回退选项。
        variants.append(['mvn'] + docker_cmd_parts)  # 回退到容器镜像内置 Maven，避免 wrapper 分发包下载失败卡死。
    return variants  # 返回按优先级排序的命令候选列表。


def _dockerize_maven_cmd_parts(repo_dir: str, cmd_parts: list) -> List[str]:  # 将 Maven 参数中的宿主机路径改写为容器内路径。
    settings_path = _ensure_maven_settings_file(repo_dir)  # 先确保隔离 settings 文件已经生成，避免容器内引用不存在的文件。
    docker_settings_path = f"/workspace/{os.path.basename(settings_path)}"  # Docker 中仓库根目录统一挂载到 /workspace。
    docker_cmd_parts = []  # 收集容器内可执行的命令参数。
    for arg in cmd_parts:  # 顺序遍历现有 Maven 参数列表。
        if arg == settings_path:  # 命中宿主机绝对路径的 settings 文件时改写为容器路径。
            docker_cmd_parts.append(docker_settings_path)  # 使用容器工作目录下可见的 settings 文件路径。
            continue  # 继续处理后续参数。
        docker_cmd_parts.append(arg)  # 其余参数原样透传。
    return docker_cmd_parts  # 返回适用于 Docker 的 Maven 参数列表。


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
        'build timed out',
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


def _maven_cli_args(repo_dir: str) -> List[str]:  # 为 Maven 命令统一追加隔离后的设置文件与更新策略。
    return ['-U', '-s', _ensure_maven_settings_file(repo_dir)]  # 强制重新检查此前缓存的缺失依赖，并绕开宿主机用户 settings 里的镜像污染。


def _ensure_maven_settings_file(repo_dir: str) -> str:  # 为当前工作区生成隔离后的 Maven settings 文件。
    settings_path = os.path.join(repo_dir, '.rerun_tool.maven-settings.xml')  # 将 settings 放在仓库根目录，便于本地与 Docker 共用同一路径。
    if os.path.isfile(settings_path):  # 已存在时直接复用，避免重复写盘。
        return settings_path  # 返回已有的隔离 settings 文件路径。
    settings_content = (
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"\n'
        '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">\n'
        '  <mirrors>\n'
        '    <mirror>\n'
        '      <id>rerun-tool-central</id>\n'
        '      <name>rerun-tool-central</name>\n'
        '      <url>https://repo1.maven.org/maven2</url>\n'
        '      <mirrorOf>central</mirrorOf>\n'
        '    </mirror>\n'
        '  </mirrors>\n'
        '</settings>\n'
    )  # 仅覆盖 central，避免宿主机 ~/.m2/settings.xml 里的私有镜像把公共依赖解析带偏。
    with open(settings_path, 'w', encoding='utf-8') as f:  # 以 UTF-8 写入隔离后的 settings 文件。
        f.write(settings_content)  # 落盘最小化 settings 内容。
    return settings_path  # 返回生成好的 settings 文件路径。


def _maven_stability_flags(include_test_failure_ignore: bool = False) -> List[str]:  # 为 Maven 构建与测试添加保守的稳定性降噪参数。
    flags = [  # 只保留对“编译并运行目标测试”通常无副作用的质量检查跳过项。
        '-Dstyle.color=never',  # 关闭 ANSI 彩色输出，方便后续日志解析。
        '-Drat.skip=true',  # 跳过 Apache RAT 许可证扫描。
        '-Dcheckstyle.skip=true',  # 跳过 Checkstyle 校验。
        '-Dpmd.skip=true',  # 跳过 PMD 静态检查，避免 botbuilder-java 一类项目在 test-compile 前就被质量门拦下。
        '-DskipPmd=true',  # 兼容部分项目使用 skipPmd 作为统一开关的写法。
        '-Dbasepom.check.skip-pmd=true',  # 兼容 HubSpot basepom 风格的 PMD 跳过开关。
        '-Denforcer.skip=true',  # 跳过 Enforcer 版本与环境校验。
        '-Dspotbugs.skip=true',  # 跳过 SpotBugs 分析。
        '-Dfindbugs.skip=true',  # 跳过 FindBugs 分析。
        '-Djacoco.skip=true',  # 跳过 JaCoCo 覆盖率任务。
        '-Danimal.sniffer.skip=true',  # 跳过 API 兼容性扫描。
        '-Dspotless.check.skip=true',  # 跳过 Spotless 格式检查。
        '-Dformatter.skip=true',  # 跳过 formatter-maven-plugin 之类的源码格式化检查。
        '-Dimpsort.skip=true',  # 跳过 impsort-maven-plugin 的 import 排序检查。
        '-Dsort.skip=true',  # 兼容部分插件使用 sort.skip 控制 import 排序检查。
        '-Dfmt.skip=true',  # 跳过 fmt-maven-plugin 的格式检查。
        '-DskipFormat=true',  # 兼容部分项目约定的统一格式检查开关。
        '-Dprettier.skip=true',  # 跳过 prettier 相关检查。
        '-Dprettier-java.skip=true',  # 跳过 prettier-java 检查。
        '-Dbasepom.check.skip-prettier=true',  # 兼容 HubSpot basepom 风格的 prettier 开关。
        '-Dxml-format.skip=true',  # 兼容通过 xml-format.skip 控制的格式化插件。
        '-Ddependency-check.skip=true',  # 跳过依赖漏洞扫描。
        '-Dlicense.skip=true',  # 跳过 license 检查。
        '-Dgpg.skip=true',  # 跳过签名相关任务。
        '-Dskip.web.build=true',  # 跳过 Graylog 一类与目标 Java 测试无关的前端构建 profile。
        '-Dfrontend.skip=true',  # 尝试关闭 frontend-maven-plugin 这类 Node/Yarn 下载与构建步骤。
        '-Dskip.installnodenpm=true',  # 兼容部分项目约定的前端安装跳过属性名。
        '-Dskip.installnodeandyarn=true',  # 兼容 frontend-maven-plugin 的 Node/Yarn 安装跳过属性名。
        '-Dskip.npm=true',  # 尝试关闭 NPM 相关步骤，减少与目标测试无关的前端噪声。
        '-Dskip.yarn=true',  # 尝试关闭 Yarn 相关步骤，减少与目标测试无关的前端噪声。
    ]  # 这些参数主要减少与测试本身无关的失败来源。
    if include_test_failure_ignore:  # 仅测试阶段需要让 Maven 即使遇到失败用例也尽量保留完整输出。
        flags.append('-Dmaven.test.failure.ignore=true')  # 让我们可以基于日志自行区分 fail 与 error。
    return flags  # 返回追加到 Maven 命令尾部的稳定性参数。


def _get_build_env(repo_dir: str) -> dict:  # 为本地构建与测试命令生成隔离后的环境变量集合。
    env = os.environ.copy()  # 先复制当前环境变量，避免覆盖调用方进程的环境。
    env['CI'] = 'true'  # 显式标记为 CI 风格环境，减少构建工具进入交互或花哨输出模式。
    cache_root = _local_cache_root(repo_dir)  # 基于当前工作区生成不会被 `git clean` 删除的工具缓存目录。
    maven_repo_dir = os.path.join(cache_root, 'm2-repository')  # 为本地 Maven 准备专用仓库目录，避免宿主机全局仓库污染结果。
    gradle_user_home = os.path.join(cache_root, 'gradle-user-home')  # 为本地 Gradle 准备专用用户目录，避免复用宿主机全局缓存与配置。
    os.makedirs(maven_repo_dir, exist_ok=True)  # 确保本地 Maven 专用仓库目录存在。
    os.makedirs(gradle_user_home, exist_ok=True)  # 确保本地 Gradle 专用用户目录存在。
    env['MAVEN_OPTS'] = _append_env_opt(_default_jvm_opts(env.get('MAVEN_OPTS')), f'-Dmaven.repo.local={maven_repo_dir}')  # 在保留原有 JVM 参数的同时强制 Maven 使用隔离仓库。
    if 'GRADLE_OPTS' not in env:
        env['GRADLE_OPTS'] = '-Xmx2g -Xms512m'  # 在未显式配置时为 Gradle 提供保守的 JVM 内存参数。
    env['GRADLE_USER_HOME'] = gradle_user_home  # 强制本地 Gradle 使用隔离后的用户目录与缓存。
    return env  # 返回隔离后的环境变量集合供本地命令复用。


def _local_cache_root(repo_dir: str) -> str:  # 计算当前工作区共享的本地构建缓存根目录。
    workspace_dir = os.path.dirname(os.path.abspath(repo_dir))  # 仓库根目录的父目录就是 test-runner 的共享 workspace。
    cache_root = os.path.join(workspace_dir, '.rerun_tool_cache')  # 将共享缓存放在工作区外层，避免被仓库 reset 清掉。
    os.makedirs(cache_root, exist_ok=True)  # 确保共享缓存根目录存在。
    return cache_root  # 返回构造完成的共享缓存根目录路径。


def _default_jvm_opts(existing_value: Optional[str]) -> str:  # 在保留用户已有配置的基础上补入默认 JVM 参数。
    normalized_value = (existing_value or '').strip()  # 先统一处理空值与首尾空白。
    if normalized_value:
        return normalized_value  # 用户已经显式提供 JVM 参数时尊重原配置。
    return '-Xmx2g -Xms512m'  # 否则使用当前工具默认的保守内存设置。


def _append_env_opt(existing_value: str, option: str) -> str:  # 将单个 JVM 选项幂等地追加到已有环境变量值中。
    normalized_value = (existing_value or '').strip()  # 先统一处理已有值的空白格式。
    if option in normalized_value:
        return normalized_value  # 已经存在相同选项时直接返回，避免重复追加。
    if not normalized_value:
        return option  # 原值为空时直接返回目标选项。
    return f'{normalized_value} {option}'  # 否则在原值末尾追加一个空格和目标选项。
