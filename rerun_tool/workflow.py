import logging  # 导入日志模块用于记录编排流程。 
import os  # 导入路径工具用于拼接工作区路径。 
import time  # 导入计时工具用于记录总耗时与阶段耗时。 
from dataclasses import dataclass  # 导入数据类装饰器用于封装执行配置。 
from typing import Optional, Tuple  # 导入类型注解工具。 

from .data import RunRequest, RunnerBackend, WorkflowKind  # 导入统一请求模型与工作流枚举。 
from .docker import should_use_docker  # 导入 Docker 自动决策函数。 
from .patch import apply_patch, find_test_file, fix_missing_imports  # 导入测试文件定位、补丁应用与缺失 import 修复函数。 
from .repo import clone_repo, reset_repo  # 导入仓库克隆与复位函数。 
from .runner import RerunExecutionSummary, RerunMode, TestRunResult, build_project, detect_build_tool, run_test_with_summary  # 导入执行层能力。 

logger = logging.getLogger(__name__)  # 创建当前模块的日志记录器。 


@dataclass  # 定义运行期执行配置。 
class ExecutionConfig:  # 该结构封装一次批处理或单次执行的全局参数。 
    rerun_count: int  # 保存目标测试的重跑次数。 
    mode: RerunMode  # 保存当前 JVM 复用模式。 
    docker_mode: str  # 保存 Docker 执行模式。 
    build_timeout: int  # 保存构建超时时间。 
    test_timeout: int  # 保存单次测试超时时间。 
    build_retries: int  # 保存构建阶段最大重试次数。 


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

    def run(self, repo_dir: str, request: RunRequest, config: ExecutionConfig, use_docker: bool) -> RerunExecutionSummary:  # 使用当前后端执行多次测试并返回摘要。 
        return run_test_with_summary(repo_dir=repo_dir, entry=request, rerun_count=config.rerun_count, mode=config.mode, use_docker=use_docker, timeout=config.test_timeout, runner_backend=self.runner_backend)  # 调用统一执行入口并指定后端。 


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
    if not clone_repo(request.repo_url, repo_dir, request.original_sha):  # 克隆或检出失败时直接返回。 
        return TestRunResult(entry=request, status="clone_failed", error_message=f"Failed to clone {request.repo_url} at {request.original_sha}", total_elapsed_seconds=_elapsed_since(request_started_at))  # 返回克隆失败结果。 
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
    use_docker = _resolve_use_docker(repo_dir, request, config.docker_mode)  # 根据命令行模式与仓库情况决定是否使用 Docker。 
    logger.info(f"  Execution: {'Docker' if use_docker else 'Local'}")  # 记录最终执行环境选择。 
    logger.info("Step 4: Building project...")  # 记录构建阶段开始。 
    build_ok, build_output = build_project(repo_dir=repo_dir, entry=request, use_docker=use_docker, timeout=config.build_timeout, max_retries=config.build_retries)  # 先编译测试与依赖。 
    if not build_ok and preparer.should_attempt_import_fix() and test_file:  # 只有带补丁流程才允许做 import 自动修复。 
        logger.warning("Initial build failed, attempting missing-import repair...")  # 记录进入 import 修复分支。 
        repaired, repair_msg = fix_missing_imports(test_file, build_output)  # 按编译错误尝试补全高置信度缺失 import。 
        if repaired:  # 只有实际修改了文件时才重新构建。 
            logger.info(f"Import repair applied: {repair_msg}")  # 记录本次自动修复详情。 
            build_ok, build_output = build_project(repo_dir=repo_dir, entry=request, use_docker=use_docker, timeout=config.build_timeout, max_retries=config.build_retries)  # 修复后立即重新构建。 
        else:  # 没有可修复 import 时只记录原因。 
            logger.info(f"Import repair skipped: {repair_msg}")  # 记录为何没有执行自动修复。 
    if not build_ok:  # 构建仍然失败时返回 build_failed。 
        logger.error(f"Build failed: {build_output[-500:]}")  # 记录构建尾部日志帮助排查。 
        reset_repo(repo_dir)  # 失败路径上恢复仓库状态。 
        return TestRunResult(entry=request, status="build_failed", error_message=build_output[-1000:], build_output=build_output, total_elapsed_seconds=_elapsed_since(request_started_at))  # 返回构建失败结果。 
    logger.info(f"Step 5: Running test {config.rerun_count} times (mode={config.mode.value}, runner={request.runner_backend_name})...")  # 记录测试执行阶段开始。 
    rerun_phase_started_at = time.perf_counter()  # 记录纯 rerun 阶段相对总流程的起始时间。 
    execution_summary = strategy.run(repo_dir=repo_dir, request=request, config=config, use_docker=use_docker)  # 使用所选执行策略执行多次测试。 
    reset_repo(repo_dir)  # 在成功路径上同样恢复仓库状态。 
    checkpoint_total_elapsed_seconds = {}  # 保存各关键阶段包含构建等在内的累计耗时。 
    for checkpoint, rerun_elapsed in execution_summary.checkpoint_rerun_elapsed_seconds.items():  # 将纯 rerun 阶段耗时换算为总流程累计耗时。 
        checkpoint_total_elapsed_seconds[checkpoint] = (rerun_phase_started_at - request_started_at) + rerun_elapsed  # 记录从请求开始到当前关键阶段的总耗时。 
    logger.info(f"Results: {execution_summary.results}")  # 记录当前请求的逐次运行结果。 
    return TestRunResult(entry=request, status="completed", results=execution_summary.results, total_elapsed_seconds=_elapsed_since(request_started_at), rerun_elapsed_seconds=execution_summary.rerun_elapsed_seconds, checkpoint_total_elapsed_seconds=checkpoint_total_elapsed_seconds, checkpoint_rerun_elapsed_seconds=execution_summary.checkpoint_rerun_elapsed_seconds)  # 返回成功完成且包含耗时统计的结果对象。 


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
