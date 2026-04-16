import logging  # 导入日志模块用于记录编排流程。 
import os  # 导入路径工具用于拼接工作区路径。 
import time  # 导入计时工具用于记录总耗时与阶段耗时。 
from dataclasses import dataclass  # 导入数据类装饰器用于封装执行配置。 
from typing import Optional, Tuple  # 导入类型注解工具。 

from .data import PatchSpec, RunRequest, RunnerBackend, WorkflowKind  # 导入统一请求模型与工作流枚举。 
from .docker import should_use_docker  # 导入 Docker 自动决策函数。 
from .patch import (apply_generated_patch_context, apply_patch, find_test_file,  # 导入测试文件定位、补丁应用与基于原始 generated_patch 的上下文补全能力。
                    fix_missing_imports, fix_unreported_exception_declaration, restore_backup)  # 继续导入保守自动修复和原始文件恢复函数。 
from .repo import clone_repo, reset_repo  # 导入仓库克隆与复位函数。 
from .runner import ExecutionEnvironment, RerunExecutionSummary, RerunMode, TestRunResult, build_project, detect_build_tool, resolve_execution_environment, run_test_with_summary  # 导入执行层能力与统一环境决策结构。 

logger = logging.getLogger(__name__)  # 创建当前模块的日志记录器。 

MAX_AUTOMATIC_BUILD_REPAIR_ROUNDS = 3  # 将保守自动修复限制在最多 3 轮，避免在不可修复样本上无止境反复构建。 


@dataclass  # 定义运行期执行配置。 
class ExecutionConfig:  # 该结构封装一次批处理或单次执行的全局参数。 
    rerun_count: int  # 保存目标测试的重跑次数。 
    mode: RerunMode  # 保存当前 JVM 复用模式。 
    docker_mode: str  # 保存 Docker 执行模式。 
    build_timeout: int  # 保存构建超时时间。 
    test_timeout: int  # 保存单次测试超时时间。 
    build_retries: int  # 保存构建阶段最大重试次数。 
    git_timeout: int = 1800  # 保存 Git clone、fetch 与 checkout 的统一超时时间。 
    git_retries: int = 2  # 保存 Git 阶段最大重试次数。 


class WorkspacePreparer:  # 定义工作区准备策略的抽象基类。 
    def prepare(self, repo_dir: str, request: RunRequest) -> Tuple[bool, Optional[str], str]:  # 准备当前请求所需的工作区状态。 
        raise NotImplementedError()  # 由具体子类实现准备逻辑。 

    def should_attempt_import_fix(self) -> bool:  # 声明当前准备策略是否允许自动 import 修复。 
        return False  # 默认不允许修改源码来修复构建。 


class NoPatchPreparer(WorkspacePreparer):  # 定义 patchless 检测模式的准备策略。 
    def prepare(self, repo_dir: str, request: RunRequest) -> Tuple[bool, Optional[str], str]:  # 在不改源码的前提下验证目标测试文件存在。 
        logger.info("Step 2: Finding test file...")  # 记录测试文件定位阶段开始。 
        test_file = find_test_file(repo_dir, request)  # 复用现有文件定位逻辑寻找目标测试文件。 
        if test_file is None:  # 目标测试文件不存在时直接失败。 
            return False, None, f"Test file not found for {request.test_class}"  # 返回文件未找到错误。 
        logger.info("Step 3: Patchless mode enabled, keeping repository unchanged")  # 明确说明当前流程不会修改源码。 
        return True, test_file, "OK"  # 返回准备成功结果。 


class ApplyGeneratedPatchPreparer(WorkspacePreparer):  # 定义补丁验证模式的准备策略。 
    def prepare(self, repo_dir: str, request: RunRequest) -> Tuple[bool, Optional[str], str]:  # 在工作区中定位并应用目标补丁。 
        logger.info("Step 2: Finding test file...")  # 记录测试文件定位阶段开始。 
        test_file = find_test_file(repo_dir, request)  # 先定位目标测试文件。 
        if test_file is None:  # 没有找到目标测试文件时直接失败。 
            return False, None, f"Test file not found for {request.test_class}"  # 返回文件未找到错误。 
        logger.info("Step 3: Applying patch...")  # 记录补丁应用阶段开始。 
        patch_ok, patch_msg = apply_patch(test_file, request)  # 在目标测试文件上应用生成补丁。 
        if not patch_ok:  # 补丁应用失败时直接返回错误。 
            return False, test_file, f"Patch failed: {patch_msg}"  # 返回补丁失败信息。 
        return True, test_file, "OK"  # 返回准备成功结果。 

    def should_attempt_import_fix(self) -> bool:  # 声明补丁验证流程允许尝试修复缺失 import。 
        return True  # 带补丁流程可以在首次构建失败时做保守 import 修复。 


class TestExecutionStrategy:  # 定义测试执行策略的抽象基类。 
    runner_backend = RunnerBackend.STANDARD  # 默认执行后端为标准重跑。 

    def validate(self, repo_dir: str, request: RunRequest) -> Optional[str]:  # 在真正执行前校验当前后端是否支持该仓库。 
        return None  # 默认无需额外校验。 

    def run(self, repo_dir: str, request: RunRequest, config: ExecutionConfig, use_docker: bool, execution_env: Optional[ExecutionEnvironment] = None) -> RerunExecutionSummary:  # 使用当前后端执行多次测试并返回摘要。 
        return run_test_with_summary(repo_dir=repo_dir, entry=request, rerun_count=config.rerun_count, mode=config.mode, use_docker=use_docker, timeout=config.test_timeout, runner_backend=self.runner_backend, execution_env=execution_env)  # 调用统一执行入口并指定后端与环境决策。 


class StandardRerunStrategy(TestExecutionStrategy):  # 定义标准 surefire 或 gradle test 执行策略。 
    runner_backend = RunnerBackend.STANDARD  # 显式声明当前策略对应标准执行后端。 


class NonDexRerunStrategy(TestExecutionStrategy):  # 定义 Maven NonDex 执行策略。 
    runner_backend = RunnerBackend.NONDEX  # 显式声明当前策略对应 NonDex 后端。 

    def validate(self, repo_dir: str, request: RunRequest) -> Optional[str]:  # 只有 Maven 项目才支持 NonDex。 
        build_tool = detect_build_tool(repo_dir, request.module)  # 先探测当前仓库的构建工具。 
        if build_tool != 'maven':  # Gradle 等非 Maven 项目当前暂不支持 NonDex。 
            return "NonDex backend is currently only supported for Maven projects"  # 返回明确的能力边界错误。 
        return None  # Maven 项目可以继续执行。 


def process_request(request: RunRequest, workspace_dir: str, config: ExecutionConfig) -> TestRunResult:  # 处理单个统一运行请求。 
    request_started_at = time.perf_counter()  # 记录当前请求从克隆开始的总耗时起点。 
    project_id = f"{request.repo_owner}_{request.project_name}" if request.repo_owner else request.project_name  # 为工作区目录构造稳定项目标识。 
    repo_dir = os.path.join(workspace_dir, project_id)  # 拼出当前请求对应的本地仓库目录。 
    logger.info(f"\n{'=' * 60}")  # 打印请求分隔线。 
    logger.info(f"Processing: {request.full_test_name}")  # 记录当前处理的测试名称。 
    logger.info(f"  Project: {request.project_name} | Module: {request.module}")  # 记录当前项目和模块。 
    logger.info(f"  SHA: {request.original_sha[:8]}")  # 记录当前提交号前缀。 
    logger.info(f"  Workflow: {request.workflow_name} | Runner: {request.runner_backend_name}")  # 记录当前工作流与执行后端。 
    logger.info("Step 1: Cloning repository...")  # 记录克隆阶段开始。 
    clone_result = clone_repo(request.repo_url, repo_dir, request.original_sha, timeout=config.git_timeout, max_retries=config.git_retries)  # 使用统一 Git 超时与重试参数准备目标仓库。 
    if not clone_result.success:  # 克隆或检出失败时直接返回详细诊断。 
        return TestRunResult(entry=request, status="clone_failed", error_message=clone_result.message, total_elapsed_seconds=_elapsed_since(request_started_at))  # 返回包含具体阶段与错误尾部的克隆失败结果。 
    logger.info(f"  Repository ready: {clone_result.message}")  # 记录仓库准备成功路径是复用还是重新克隆。 
    preparer = _select_preparer(request)  # 按工作流选择工作区准备策略。 
    strategy = _select_execution_strategy(request)  # 按执行后端选择测试执行策略。 
    validation_error = strategy.validate(repo_dir, request)  # 在真正构建前校验执行策略是否适配当前仓库。 
    if validation_error:  # 后端不支持当前仓库时提前结束。 
        reset_repo(repo_dir)  # 尽量把仓库恢复为干净状态。 
        return TestRunResult(entry=request, status="unsupported_runner", error_message=validation_error, total_elapsed_seconds=_elapsed_since(request_started_at))  # 返回配置不支持错误。 
    prepare_ok, test_file, prepare_msg = preparer.prepare(repo_dir, request)  # 执行工作区准备逻辑。 
    if not prepare_ok:  # 准备失败时根据错误类型返回不同状态。 
        reset_repo(repo_dir)  # 在失败路径上恢复仓库状态。 
        status = "patch_failed" if request.workflow == WorkflowKind.VERIFY_PATCH and test_file else "file_not_found"  # 带补丁流程优先标记 patch 失败，其余情况视为文件未找到。 
        return TestRunResult(entry=request, status=status, error_message=prepare_msg, total_elapsed_seconds=_elapsed_since(request_started_at))  # 返回准备阶段错误。 
    requested_use_docker = _resolve_use_docker(repo_dir, request, config.docker_mode)  # 先根据 CLI 模式与仓库情况得出期望执行环境。 
    execution_env = resolve_execution_environment(repo_dir=repo_dir, entry=request, requested_use_docker=requested_use_docker, docker_fallback_allowed=config.docker_mode != 'always')  # 在构建前统一解析出真正要使用的执行环境。 
    if execution_env.error_message:  # 环境无法安全确定时提前结束，避免静默回退到错误环境。 
        logger.error(execution_env.error_message)  # 记录当前环境阻断原因。 
        reset_repo(repo_dir)  # 在环境失败路径上同样恢复仓库状态。 
        return TestRunResult(entry=request, status="build_failed", error_message=execution_env.error_message, build_output=execution_env.error_message, total_elapsed_seconds=_elapsed_since(request_started_at))  # 将环境决策失败显式写成构建阶段错误。 
    logger.info(f"  Execution: {'Docker' if execution_env.use_docker else 'Local'}")  # 记录最终执行环境选择。 
    logger.info(f"  Environment decision: {execution_env.decision_reason}")  # 记录执行环境选择背后的原因。 
    logger.info("Step 4: Building project...")  # 记录构建阶段开始。 
    build_ok, build_output = build_project(repo_dir=repo_dir, entry=request, use_docker=execution_env.use_docker, timeout=config.build_timeout, max_retries=config.build_retries, execution_env=execution_env)  # 先编译测试与依赖，并复用统一环境决策。 
    build_ok, build_output = _repair_build_if_possible(repo_dir=repo_dir, request=request, test_file=test_file, preparer=preparer, execution_env=execution_env, config=config, build_ok=build_ok, build_output=build_output)  # 在首次构建失败后执行有限轮次的保守自动修复。 
    build_ok, build_output = _augment_generated_patch_context_if_possible(repo_dir=repo_dir, request=request, test_file=test_file, preparer=preparer, execution_env=execution_env, config=config, build_ok=build_ok, build_output=build_output)  # 对构建失败的原始补丁再尝试从 generated_patch 自身推断 import/pom 上下文。 
    if not build_ok:  # 构建仍然失败时返回 build_failed。 
        logger.error(f"Build failed: {build_output[-500:]}")  # 记录构建尾部日志帮助排查。 
        reset_repo(repo_dir)  # 失败路径上恢复仓库状态。 
        return TestRunResult(entry=request, status="build_failed", error_message=_compact_error_message(build_output), build_output=build_output, total_elapsed_seconds=_elapsed_since(request_started_at))  # 返回构建失败结果并尽量同时保留修复历史与最终错误尾部。 
    logger.info(f"Step 5: Running test {config.rerun_count} times (mode={config.mode.value}, runner={request.runner_backend_name})...")  # 记录测试执行阶段开始。 
    rerun_phase_started_at = time.perf_counter()  # 记录纯 rerun 阶段相对总流程的起始时间。 
    execution_summary = strategy.run(repo_dir=repo_dir, request=request, config=config, use_docker=execution_env.use_docker, execution_env=execution_env)  # 使用所选执行策略执行多次测试并复用同一环境决策。 
    reset_repo(repo_dir)  # 在成功路径上同样恢复仓库状态。 
    checkpoint_total_elapsed_seconds = {}  # 保存各关键阶段包含构建等在内的累计耗时。 
    for checkpoint, rerun_elapsed in execution_summary.checkpoint_rerun_elapsed_seconds.items():  # 将纯 rerun 阶段耗时换算为总流程累计耗时。 
        checkpoint_total_elapsed_seconds[checkpoint] = (rerun_phase_started_at - request_started_at) + rerun_elapsed  # 记录从请求开始到当前关键阶段的总耗时。 
    logger.info(f"Results: {execution_summary.results}")  # 记录当前请求的逐次运行结果。 
    rerun_error_message = _summarize_rerun_errors(execution_summary.error_outputs)  # 将测试阶段的 error 输出整理成可直接写入结果 CSV 的诊断信息。 
    return TestRunResult(entry=request, status="completed", results=execution_summary.results, error_message=rerun_error_message, total_elapsed_seconds=_elapsed_since(request_started_at), rerun_elapsed_seconds=execution_summary.rerun_elapsed_seconds, checkpoint_total_elapsed_seconds=checkpoint_total_elapsed_seconds, checkpoint_rerun_elapsed_seconds=execution_summary.checkpoint_rerun_elapsed_seconds)  # 返回成功完成且包含耗时统计与 RUN_ERROR 诊断的结果对象。 


def _select_preparer(request: RunRequest) -> WorkspacePreparer:  # 根据工作流选择工作区准备策略。 
    if request.workflow == WorkflowKind.VERIFY_PATCH:  # 补丁验证流程需要先应用补丁。 
        return ApplyGeneratedPatchPreparer()  # 返回带补丁准备策略。 
    return NoPatchPreparer()  # 其余流程默认使用不改源码的准备策略。 


def _select_execution_strategy(request: RunRequest) -> TestExecutionStrategy:  # 根据执行后端选择测试执行策略。 
    if request.runner_backend == RunnerBackend.NONDEX:  # 显式选择 NonDex 时返回 NonDex 策略。 
        return NonDexRerunStrategy()  # 返回 NonDex 执行策略实例。 
    return StandardRerunStrategy()  # 默认返回标准执行策略。 


def _resolve_use_docker(repo_dir: str, request: RunRequest, docker_mode: str) -> bool:  # 统一封装 Docker 执行环境决策逻辑。 
    if docker_mode == 'always':  # 用户显式要求始终使用 Docker。 
        return True  # 直接返回 True。 
    if docker_mode == 'never':  # 用户显式要求始终使用本地环境。 
        return False  # 直接返回 False。 
    return should_use_docker(repo_dir, request.module)  # 自动模式下沿用现有模块级 Docker 判断逻辑。 


def _elapsed_since(started_at: float) -> float:  # 计算从起点到当前时刻的稳定耗时。 
    return time.perf_counter() - started_at  # 返回高精度计时器测得的秒数差值。 


def _repair_build_if_possible(repo_dir: str, request: RunRequest, test_file: Optional[str], preparer: WorkspacePreparer, execution_env: ExecutionEnvironment, config: ExecutionConfig, build_ok: bool, build_output: str) -> Tuple[bool, str]:  # 在构建失败后执行有限轮次的保守源码修复。 
    if build_ok or not preparer.should_attempt_import_fix() or not test_file:  # 非失败路径、非补丁流程或缺少测试文件时无需进入自动修复。 
        return build_ok, build_output  # 直接返回当前构建结果。 
    repair_history = []  # 收集每一轮自动修复的摘要说明，便于最终失败时写入诊断。 
    current_build_ok = build_ok  # 保存当前轮次的构建是否成功。 
    current_build_output = build_output  # 保存当前轮次的构建输出。 
    for repair_round in range(1, MAX_AUTOMATIC_BUILD_REPAIR_ROUNDS + 1):  # 在有限轮次内重复执行“修复后重构建”流程。 
        logger.warning(f"Build failed, attempting automatic repair ({repair_round}/{MAX_AUTOMATIC_BUILD_REPAIR_ROUNDS})...")  # 记录当前进入第几轮自动修复。 
        repaired, repair_msg = fix_unreported_exception_declaration(test_file, request.test_method, current_build_output)  # 先处理“必须捕获或声明抛出”的 checked exception 问题，避免被错误 import 带偏。
        if not repaired:  # 当前轮没有命中 checked exception 时，再退回现有的缺失 import 与符号修复。
            repaired, repair_msg = fix_missing_imports(test_file, current_build_output)  # 根据当前构建输出尝试补全缺失 import、static import 或符号大小写。 
        if not repaired:  # 当前错误已经没有可安全自动修复的动作时停止循环。 
            logger.info(f"Automatic repair skipped: {repair_msg}")  # 记录本轮为何没有继续修复。 
            break  # 结束后续自动修复尝试。 
        repair_history.append(f"round {repair_round}: {repair_msg}")  # 记录当前轮成功应用的修复摘要。 
        logger.info(f"Automatic repair applied: {repair_msg}")  # 记录本轮自动修复详情。 
        current_build_ok, current_build_output = build_project(repo_dir=repo_dir, entry=request, use_docker=execution_env.use_docker, timeout=config.build_timeout, max_retries=config.build_retries, execution_env=execution_env)  # 修复后立即复用同一环境重新构建。 
        if current_build_ok:  # 任一轮修复后构建成功就提前返回。 
            return True, current_build_output  # 返回成功构建结果。 
    if repair_history:  # 只有真的做过自动修复时才把修复历史附加到最终错误信息中。 
        current_build_output = f"Automatic repair history: {' | '.join(repair_history)}\n\n{current_build_output}"  # 将修复历史前缀附加到最终构建输出中便于结果 CSV 直接排查。 
    return current_build_ok, current_build_output  # 返回最终构建结果与可能增强过诊断的构建输出。 


def _augment_generated_patch_context_if_possible(repo_dir: str, request: RunRequest, test_file: Optional[str], preparer: WorkspacePreparer, execution_env: ExecutionEnvironment, config: ExecutionConfig, build_ok: bool, build_output: str) -> Tuple[bool, str]:  # 当原始 generated_patch 仍然构建失败时，仅基于它自身推断缺失的 import 与 pom 上下文。
    if build_ok or request.workflow != WorkflowKind.VERIFY_PATCH or not test_file:  # 只有 verify-patch 且当前仍然构建失败时才需要尝试补全补丁上下文。
        return build_ok, build_output  # 其余场景保持原结果不变。
    logger.warning("Build still failing after automatic repair, retrying with context inferred from the original generated_patch...")  # 记录当前进入“从原始补丁自身推断上下文”的回退流程。
    fallback_history = []  # 记录这次上下文补全链路里发生了什么，便于最终失败时直接诊断。
    if not restore_backup(test_file):  # 必须先回到原始测试文件，再重新应用原始 generated_patch。
        fallback_history.append('restore_backup_failed')  # 记录当前无法恢复原始测试文件。
        return build_ok, f"Generated patch context history: {' | '.join(fallback_history)}\n\n{build_output}"  # 保留原始错误输出并补充失败历史。
    patch_ok, patch_msg = apply_patch(test_file, request)  # 把当前被评估的原始 generated_patch 重新应用回干净工作区。
    if not patch_ok:  # 原始补丁无法重放时，说明当前样本无法继续做上下文增强。
        fallback_history.append(f"reapply_original_patch_failed ({patch_msg})")  # 记录无法重放原始补丁。
        return build_ok, f"Generated patch context history: {' | '.join(fallback_history)}\n\n{build_output}"  # 返回保留原始构建错误的结果。
    context_ok, context_msg = apply_generated_patch_context(repo_dir, request, test_file)  # 仅根据原始 generated_patch 自身推断 import 和依赖上下文。
    if not context_ok:  # 没有成功补齐上下文时直接保留原始失败结果。
        fallback_history.append(f"context_inference_failed ({context_msg})")  # 记录上下文推断失败原因。
        return build_ok, f"Generated patch context history: {' | '.join(fallback_history)}\n\n{build_output}"  # 保留原始构建错误，并写入新的上下文失败信息。
    normalized_context_msg = (context_msg or '').replace('Reference patch context', 'Generated patch context')  # 统一把底层通用 helper 的消息改写成当前真实语义。
    if not normalized_context_msg or normalized_context_msg == 'Generated patch context already satisfied':  # 当前补丁自身没有推断出任何新的 import 或依赖时，不需要再重复构建相同状态。
        fallback_history.append('no_additional_context_inferred')  # 记录本轮没有学到新的上下文。
        return build_ok, f"Generated patch context history: {' | '.join(fallback_history)}\n\n{build_output}"  # 返回原始失败结果并附带当前上下文推断轨迹。
    candidate_build_ok, candidate_build_output = build_project(repo_dir=repo_dir, entry=request, use_docker=execution_env.use_docker, timeout=config.build_timeout, max_retries=config.build_retries, execution_env=execution_env)  # 用“原始补丁 + 从自身推断出的上下文”重新构建当前案例。
    candidate_build_ok, candidate_build_output = _repair_build_if_possible(repo_dir=repo_dir, request=request, test_file=test_file, preparer=preparer, execution_env=execution_env, config=config, build_ok=candidate_build_ok, build_output=candidate_build_output)  # 若还只差 import 或 checked exception，再复用现有保守修复逻辑补齐。
    if candidate_build_ok:  # 一旦补丁自身的上下文增强可以构建成功，就直接保留这次成功结果。
        logger.info("Generated patch context augmentation succeeded")  # 记录这次成功来自原始补丁自身的上下文推断。
        prefix = f"Generated patch context augmentation: {normalized_context_msg}"  # 在最终构建输出前附加成功的上下文增强摘要。
        return True, f"{prefix}\n\n{candidate_build_output}" if candidate_build_output else prefix  # 返回成功构建结果并保留上下文增强摘要。
    fallback_history.append(f"build_failed ({normalized_context_msg})")  # 记录当前上下文增强后的构建仍然失败。
    return False, f"Generated patch context history: {' | '.join(fallback_history)}\n\n{candidate_build_output}"  # 返回增强后的最终构建错误，便于后续继续分析。 


def _summarize_rerun_errors(error_outputs: list[str]) -> str:  # 将测试阶段的 error 输出整理成紧凑的结果字段文本。 
    if not error_outputs:  # 没有任何测试阶段 error 输出时返回空串。 
        return ''  # 保持结果 CSV 的 error_message 为空。 
    summarized_outputs = []  # 保存去重后的 error 输出尾部。 
    for output in error_outputs:  # 逐个处理每次 error 的输出片段。 
        normalized_output = (output or '').strip()  # 去掉首尾空白后再做去重。 
        if not normalized_output or normalized_output in summarized_outputs:  # 空输出或重复输出无需再次追加。 
            continue  # 跳过当前输出片段。 
        summarized_outputs.append(normalized_output)  # 保留当前唯一的 error 输出。 
    return '\n\n---\n\n'.join(summarized_outputs)  # 使用清晰分隔符拼接多个 error 输出片段。 


def _compact_error_message(message: str, limit: int = 4000) -> str:  # 在长度受限时尽量同时保留修复历史前缀与最终错误尾部。 
    normalized_message = (message or '').strip()  # 先统一去掉空值与首尾空白。 
    if len(normalized_message) <= limit:  # 短消息无需进一步压缩。 
        return normalized_message  # 直接返回完整错误信息。 
    diagnostic_header, message_body = _split_diagnostic_header_and_body(normalized_message)  # 尝试分离前置诊断头和真正的构建输出主体。 
    if not diagnostic_header:  # 没有前置诊断头时继续沿用尾部截断策略。 
        return normalized_message[-limit:]  # 仅保留尾部更有诊断价值的内容。 
    available_suffix = limit - len(diagnostic_header) - 2  # 预留两个换行字符后计算还可保留多少尾部内容。 
    if available_suffix <= 0:  # 极端情况下前缀本身已经接近上限。 
        return diagnostic_header[:limit]  # 至少保留修复历史前缀。 
    if not message_body:  # 只有诊断头没有主体时直接返回诊断头。
        return diagnostic_header[:limit]
    return f"{diagnostic_header}\n\n{message_body[-available_suffix:]}"  # 拼接诊断头与最终错误尾部。 


def _split_diagnostic_header_and_body(message: str) -> Tuple[str, str]:  # 将前置诊断头与真实构建输出主体拆开，避免压缩时丢掉关键回退轨迹。
    diagnostic_prefixes = ('Generated patch context history:', 'Reference patch context history:', 'Reference patch fallback history:', 'Automatic repair history:')  # 同时保留当前前缀和旧 reference 前缀，避免读取历史结果或旧错误文本时丢失诊断头。
    parts = [part.strip() for part in (message or '').split('\n\n')]  # 按空行切分多个前置诊断块与后续真实日志主体。
    diagnostic_parts = []  # 收集连续出现在最前面的诊断块。
    body_start = 0  # 记录真正日志主体从哪一块开始。
    for idx, part in enumerate(parts):  # 顺序扫描切分后的区块。
        if part.startswith(diagnostic_prefixes):  # 只要当前区块还是诊断头就继续收集。
            diagnostic_parts.append(part)  # 保留当前诊断头。
            body_start = idx + 1  # 主体起点顺延。
            continue
        break  # 一旦遇到普通构建日志，后续都视为主体内容。
    if not diagnostic_parts:  # 没有诊断头时返回空前缀让上层走原有尾部截断逻辑。
        return '', message
    message_body = '\n\n'.join(part for part in parts[body_start:] if part).strip()  # 重新拼接真实构建输出主体。
    return '\n\n'.join(diagnostic_parts), message_body  # 返回诊断头和主体日志。
