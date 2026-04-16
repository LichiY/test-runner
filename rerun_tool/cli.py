import argparse  # 导入命令行参数解析工具。 
import logging  # 导入日志模块。 
import os  # 导入路径工具。 
import sys  # 导入系统接口工具。 
import time  # 导入计时工具。 
from datetime import datetime  # 导入时间戳生成工具。 
from typing import List, Optional, Tuple  # 导入类型注解工具。 

from .data import RunnerBackend, TestEntry, build_cli_request, load_flaky_requests, load_patch_requests, request_from_test_entry  # 导入统一请求构造与加载函数。 
from .results import load_results_csv, print_summary, write_results_csv  # 导入结果读写与摘要打印函数。 
from .runner import RerunMode, TestRunResult  # 导入运行模式枚举与结果对象。 
from .workflow import ExecutionConfig, process_request  # 导入统一工作流编排层。 

logger = logging.getLogger(__name__)  # 创建当前模块的日志记录器。 

RESUME_RETRYABLE_STATUSES = {'clone_failed', 'build_failed'}  # 定义在断点续跑时需要自动重跑的失败状态集合。 


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:  # 配置控制台与文件日志。 
    level = logging.DEBUG if verbose else logging.INFO  # 根据命令行参数决定日志级别。 
    fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'  # 定义统一日志格式。 
    handlers = [logging.StreamHandler(sys.stdout)]  # 默认始终向标准输出打印日志。 
    if log_file:  # 用户显式指定日志文件时再附加文件处理器。 
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)  # 确保日志目录存在。 
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))  # 将 UTF-8 文件日志处理器加入列表。 
    logging.basicConfig(level=level, format=fmt, handlers=handlers)  # 一次性初始化全局日志系统。 


def process_entry(entry: TestEntry, workspace_dir: str, rerun_count: int, mode: RerunMode, docker_mode: str, build_timeout: int, test_timeout: int, build_retries: int, runner_backend: RunnerBackend = RunnerBackend.STANDARD, git_timeout: int = 1800, git_retries: int = 2) -> TestRunResult:  # 保留旧版 process_entry 兼容包装器，并补充 Git 重试控制。 
    request = request_from_test_entry(entry, runner_backend=runner_backend)  # 先把旧版条目转换成统一运行请求。 
    config = ExecutionConfig(rerun_count=rerun_count, mode=mode, docker_mode=docker_mode, build_timeout=build_timeout, test_timeout=test_timeout, build_retries=build_retries, git_timeout=git_timeout, git_retries=git_retries)  # 构造包含 Git 超时与重试的统一执行配置。 
    return process_request(request=request, workspace_dir=workspace_dir, config=config)  # 将实际处理委托给工作流编排层。 


def main(argv: Optional[List[str]] = None) -> None:  # 作为 `python -m rerun_tool` 的统一入口。 
    raw_args = list(argv) if argv is not None else sys.argv[1:]  # 允许测试时注入自定义参数列表。 
    if raw_args and raw_args[0] in {'verify-patch', 'detect-flaky'}:  # 新架构下显式子命令优先走新解析器。 
        args = _build_subcommand_parser().parse_args(raw_args)  # 解析带子命令的新 CLI 形式。 
        _execute_args(args=args, legacy_mode=False)  # 按新模式执行请求。 
        return  # 子命令执行完毕后直接返回。 
    args = _build_legacy_parser().parse_args(raw_args)  # 旧命令格式继续使用兼容解析器。 
    args.command = 'verify-patch'  # 旧命令默认映射到补丁验证工作流。 
    _execute_args(args=args, legacy_mode=True)  # 按兼容模式执行请求。 


def _execute_args(args: argparse.Namespace, legacy_mode: bool) -> None:  # 执行已经解析完成的一组命令行参数。 
    setup_logging(verbose=args.verbose, log_file=args.log_file)  # 先初始化日志系统。 
    runner_backend = RunnerBackend(args.runner)  # 将字符串后端转换成枚举。 
    mode = _resolve_rerun_mode(args.mode, runner_backend)  # 校验并解析 JVM 复用模式。 
    _validate_input_shape(args)  # 在真正加载请求前校验输入形态是否合法。 
    _ensure_output_path(args, legacy_mode=legacy_mode)  # 如果未指定输出路径则生成默认结果文件名。 
    row_indices = _parse_row_indices(args.rows)  # 解析可选的行号过滤参数。 
    requests = _load_requests_from_args(args=args, row_indices=row_indices, runner_backend=runner_backend)  # 根据当前命令行选择正确的输入加载器。 
    requests = _apply_project_filter(requests=requests, project_filter=getattr(args, 'project', None), limit=getattr(args, 'limit', None))  # 对 CSV 输入执行项目过滤与数量截断。 
    if not requests:  # 没有任何有效请求时直接结束。 
        logger.error("No requests to process after filtering.")  # 记录当前过滤结果为空。 
        sys.exit(1)  # 返回非零退出码提示调用方。 
    logger.info(f"Loaded {len(requests)} entries to process")  # 记录当前批次请求数量。 
    logger.info(f"Rerun count: {args.rerun} | Mode: {args.mode} | Runner: {args.runner} | Docker: {args.docker_mode}")  # 记录核心运行参数。 
    logger.info(f"Build timeout: {args.build_timeout}s | Test timeout: {args.test_timeout}s | Git timeout: {args.git_timeout}s | Git retries: {args.git_retries}")  # 记录构建、测试与 Git 阶段的时间和重试配置。 
    logger.info(f"Workflow: {args.command} | Workspace: {args.workspace} | Output: {args.output}")  # 记录当前工作流与输出位置。 
    _run_requests(requests=requests, args=args, mode=mode)  # 执行统一请求主循环。 


def _run_requests(requests: List[object], args: argparse.Namespace, mode: RerunMode) -> None:  # 统一执行一组已经加载完成的运行请求。 
    workspace = os.path.abspath(args.workspace)  # 先规范化工作区路径。 
    os.makedirs(workspace, exist_ok=True)  # 确保工作区目录存在。 
    config = ExecutionConfig(rerun_count=args.rerun, mode=mode, docker_mode=args.docker_mode, build_timeout=args.build_timeout, test_timeout=args.test_timeout, build_retries=args.build_retries, git_timeout=args.git_timeout, git_retries=args.git_retries)  # 构造包含 Git 超时与重试的统一执行配置。 
    total_requests = len(requests)  # 保存当前批次的原始总请求数，便于后续统一显示整体进度。 
    active_requests = list(requests)  # 复制一份请求列表便于在 resume 时裁剪。 
    all_results: List[TestRunResult] = []  # 保存本次运行及恢复得到的全部结果对象。 
    skipped_results: List[TestRunResult] = []  # 保存 resume 后仍然保留且无需重跑的历史结果。 
    retry_results: List[TestRunResult] = []  # 保存 resume 时识别出的需要重跑的历史失败结果。 
    if args.resume:  # 只有显式开启 resume 时才读取历史结果文件。 
        entry_lookup = _build_resume_lookup(active_requests)  # 为历史恢复建立请求键和索引双映射。 
        restored_results = load_results_csv(args.output, entry_lookup)  # 从已有结果文件恢复历史结果。 
        if restored_results:  # 只有恢复到历史结果时才跳过对应请求。 
            skipped_results, retry_results = _partition_restored_results(restored_results)  # 将历史结果拆分为“保留跳过”和“失败重跑”两部分。 
            skipped_keys = {_result_request_key(result) for result in skipped_results}  # 仅对无需重跑的历史结果提取稳定请求键。 
            active_requests = [request for request in active_requests if request.request_key not in skipped_keys]  # 只跳过真正不需要重跑的历史结果。 
            all_results.extend(skipped_results)  # 仅将无需重跑的历史结果放入本次结果集合中。 
            logger.info(f"Resume enabled: kept {len(skipped_results)} existing results, scheduled {len(retry_results)} previous clone/build failures for rerun")  # 记录当前保留历史结果和重跑失败样本的数量。 
        else:  # 没有恢复到任何历史结果时仅做说明。 
            logger.info("Resume enabled but no existing results were restored")  # 记录当前没有可恢复结果。 
    logger.info(_format_overall_progress(total_requests=total_requests, all_results=all_results, skipped_results=skipped_results, retry_results=retry_results, active_requests=active_requests))  # 在正式执行前先输出一次整体进度摘要。 
    if not active_requests:  # 如果 resume 后所有请求都已经完成，则无需再进入主循环。 
        logger.info("No pending requests remain after resume filtering")  # 记录当前没有待执行请求。 
    start_time = time.time()  # 记录统一主循环起始时间。 
    for i, request in enumerate(active_requests):  # 逐条处理当前请求列表。 
        logger.info(f"\n[Batch {i + 1}/{len(active_requests)} | Overall {len(all_results) + 1}/{total_requests}] Processing entry {request.index}")  # 同时输出批内与整体位置，便于长任务观察总进度。 
        result = process_request(request=request, workspace_dir=workspace, config=config)  # 调用统一工作流编排层处理单个请求。 
        all_results.append(result)  # 将当前结果加入总结果列表。 
        write_results_csv(all_results, args.output, args.rerun)  # 每处理完一条请求就落盘一次以增强崩溃安全性。 
        logger.info(_format_overall_progress(total_requests=total_requests, all_results=all_results, skipped_results=skipped_results, retry_results=retry_results, active_requests=active_requests))  # 在每条请求结束后刷新一次整体进度摘要。 
    elapsed = time.time() - start_time  # 计算主循环实际耗时。 
    write_results_csv(all_results, args.output, args.rerun)  # 最终再次完整写出结果文件。 
    print_summary(all_results)  # 打印最终结果摘要。 
    logger.info(f"\nTotal time: {elapsed:.1f}s ({elapsed / 60:.1f}min)")  # 记录总耗时。 
    logger.info(f"Results saved to: {args.output}")  # 记录结果文件路径。 


def _build_subcommand_parser() -> argparse.ArgumentParser:  # 构造新架构下的子命令解析器。 
    parser = argparse.ArgumentParser(description="Flaky test rerun toolkit with verify-patch and detect-flaky workflows")  # 创建根解析器。 
    subparsers = parser.add_subparsers(dest='command', required=True)  # 创建强制要求的子命令集合。 
    verify_parser = subparsers.add_parser('verify-patch', help='Apply generated patches and verify stability via rerun')  # 创建补丁验证子命令。 
    verify_parser.add_argument('--csv', required=True, help='Path to the patch dataset CSV file')  # 补丁验证模式必须提供补丁 CSV。 
    _add_shared_cli_arguments(verify_parser)  # 为补丁验证子命令追加共享参数。 
    detect_parser = subparsers.add_parser('detect-flaky', help='Detect flaky tests without applying any patch')  # 创建 patchless 检测子命令。 
    detect_parser.add_argument('--csv', default=None, help='Path to the flaky detection CSV file')  # patchless 检测模式可选地从 CSV 批量加载。 
    detect_parser.add_argument('--repo-url', default=None, help='Repository URL for single-test detection mode')  # 单条 CLI 模式下的仓库地址。 
    detect_parser.add_argument('--sha', dest='original_sha', default=None, help='Commit SHA for single-test detection mode')  # 单条 CLI 模式下的目标提交号。 
    detect_parser.add_argument('--full-test-name', default=None, help='Fully qualified test name for single-test detection mode')  # 单条 CLI 模式下的完整测试名。 
    detect_parser.add_argument('--module', default='.', help='Module path for single-test detection mode')  # 单条 CLI 模式下的模块名。 
    detect_parser.add_argument('--repo-owner', default='', help='Optional repository owner for single-test detection mode')  # 单条 CLI 模式下可显式提供仓库 owner。 
    detect_parser.add_argument('--project-name', default='', help='Optional project name for single-test detection mode')  # 单条 CLI 模式下可显式提供项目名。 
    detect_parser.add_argument('--source-file', default='', help='Optional source file hint for single-test detection mode')  # 单条 CLI 模式下可显式提供源文件提示。 
    _add_shared_cli_arguments(detect_parser)  # 为 patchless 检测子命令追加共享参数。 
    return parser  # 返回构造完成的子命令解析器。 


def _build_legacy_parser() -> argparse.ArgumentParser:  # 构造与旧版本兼容的单命令解析器。 
    parser = argparse.ArgumentParser(description="Flaky Test Rerun Tool - legacy verify-patch entrypoint", formatter_class=argparse.RawDescriptionHelpFormatter, epilog="Examples:\n  python -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 5 --rerun 10\n  python -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --rows 0,1,2 --rerun 5\n  python -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 3 --rerun 10 --runner nondex")  # 创建兼容旧命令的解析器。 
    parser.add_argument('--csv', required=True, help='Path to the patch dataset CSV file')  # 旧命令继续要求提供补丁 CSV。 
    _add_shared_cli_arguments(parser)  # 旧命令同样复用共享参数定义。 
    return parser  # 返回构造完成的旧版解析器。 


def _add_shared_cli_arguments(parser: argparse.ArgumentParser) -> None:  # 为不同命令统一追加共享参数。 
    parser.add_argument('--output', '-o', default=None, help='Path to output CSV file')  # 添加结果文件路径参数。 
    parser.add_argument('--workspace', '-w', default='workspace', help='Directory to clone repositories into')  # 添加工作区目录参数。 
    parser.add_argument('--rows', type=str, default=None, help='Comma-separated row indices (0-based)')  # 添加行号过滤参数。 
    parser.add_argument('--limit', type=int, default=None, help='Maximum number of entries to process')  # 添加数量上限参数。 
    parser.add_argument('--project', type=str, default=None, help='Filter by project name (substring match)')  # 添加项目名过滤参数。 
    parser.add_argument('--rerun', '-n', type=int, default=10, help='Number of reruns per test (default: 10)')  # 添加重跑次数参数。 
    parser.add_argument('--mode', type=str, choices=['isolated', 'same_jvm'], default='isolated', help='isolated: separate JVM per run; same_jvm: reuse JVM for standard backend only')  # 添加 JVM 复用模式参数。 
    parser.add_argument('--runner', choices=['standard', 'nondex'], default='standard', help='Test execution backend: standard or nondex')  # 添加执行后端参数。 
    parser.add_argument('--docker', dest='docker_mode', type=str, choices=['auto', 'always', 'never'], default='auto', help='Docker mode: auto, always, or never')  # 添加 Docker 模式参数。 
    parser.add_argument('--build-timeout', type=int, default=600, help='Build timeout in seconds (default: 600)')  # 添加构建超时参数。 
    parser.add_argument('--test-timeout', type=int, default=300, help='Per-test-run timeout in seconds (default: 300)')  # 添加测试超时参数。 
    parser.add_argument('--build-retries', type=int, default=2, help='Max build retry attempts (default: 2)')  # 添加构建重试参数。 
    parser.add_argument('--git-timeout', type=int, default=2400, help='Git clone/fetch/checkout timeout in seconds (default: 2400)')  # 添加 Git 阶段统一超时参数。 
    parser.add_argument('--git-retries', type=int, default=2, help='Max Git retry attempts for clone/fetch/checkout (default: 2)')  # 添加 Git 阶段最大重试次数参数。 
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose/debug logging')  # 添加详细日志开关。 
    parser.add_argument('--log-file', type=str, default=None, help='Path to log file')  # 添加日志文件路径参数。 
    parser.add_argument('--resume', action='store_true', help='Resume from an existing output CSV by skipping kept results and rerunning prior clone/build failures')  # 添加断点续跑参数并说明会自动重跑历史 clone/build 失败项。 


def _load_requests_from_args(args: argparse.Namespace, row_indices: Optional[List[int]], runner_backend: RunnerBackend) -> List[object]:  # 根据当前命令行参数选择正确的请求加载方式。 
    load_limit = None if getattr(args, 'project', None) else getattr(args, 'limit', None)  # 项目过滤场景下先完整读取再截断数量。 
    if args.command == 'verify-patch':  # 补丁验证模式统一走补丁 CSV 加载器。 
        logger.info(f"Loading patch data from {args.csv}...")  # 记录当前补丁数据集路径。 
        return load_patch_requests(args.csv, rows=row_indices, limit=load_limit, runner_backend=runner_backend)  # 返回补丁验证请求列表。 
    if args.csv:  # patchless 检测模式下如果提供了 CSV 则走批量加载。 
        logger.info(f"Loading flaky detection data from {args.csv}...")  # 记录当前 patchless 数据集路径。 
        return load_flaky_requests(args.csv, rows=row_indices, limit=load_limit, runner_backend=runner_backend)  # 返回 patchless 检测请求列表。 
    logger.info("Building single flaky detection request from CLI arguments...")  # 记录当前采用单条 CLI 输入构造请求。 
    return [build_cli_request(repo_url=args.repo_url, original_sha=args.original_sha, full_test_name=args.full_test_name, module=args.module, repo_owner=args.repo_owner, project_name=args.project_name, source_file=args.source_file, runner_backend=runner_backend)]  # 返回单条 patchless 检测请求。 


def _apply_project_filter(requests: List[object], project_filter: Optional[str], limit: Optional[int]) -> List[object]:  # 在统一请求列表上执行项目过滤与数量截断。 
    filtered_requests = requests  # 默认保留原始请求列表。 
    if project_filter:  # 用户显式指定项目过滤字符串时才进行过滤。 
        lowered_project = project_filter.lower()  # 先统一转小写以便做不区分大小写匹配。 
        filtered_requests = [request for request in filtered_requests if lowered_project in request.project_name.lower()]  # 只保留项目名命中的请求。 
    if limit is not None:  # 用户显式指定数量上限时再做截断。 
        filtered_requests = filtered_requests[:limit]  # 保留前 N 条请求。 
    return filtered_requests  # 返回过滤后的请求列表。 


def _parse_row_indices(rows_arg: Optional[str]) -> Optional[List[int]]:  # 解析逗号分隔的行号参数。 
    if not rows_arg:  # 未提供行号过滤时直接返回空值。 
        return None  # 表示保留全部行。 
    return [int(item.strip()) for item in rows_arg.split(',') if item.strip()]  # 将逗号分隔文本转换成整数列表。 


def _resolve_rerun_mode(mode_name: str, runner_backend: RunnerBackend) -> RerunMode:  # 校验并解析 JVM 复用模式。 
    if runner_backend == RunnerBackend.NONDEX and mode_name != 'isolated':  # NonDex 当前只支持隔离式执行语义。 
        logger.error("NonDex backend currently only supports --mode isolated")  # 明确记录当前参数冲突。 
        sys.exit(2)  # 以参数错误退出。 
    return RerunMode.ISOLATED if mode_name == 'isolated' else RerunMode.SAME_JVM  # 返回解析后的运行模式枚举。 


def _validate_input_shape(args: argparse.Namespace) -> None:  # 校验当前命令行输入形态是否合法。 
    if args.command != 'detect-flaky':  # 只有 patchless 检测模式存在双输入入口。 
        return  # 其余模式无需额外校验。 
    has_csv = bool(args.csv)  # 判断当前是否提供了 CSV 批量输入。 
    has_single = bool(args.repo_url and args.original_sha and args.full_test_name)  # 判断当前是否提供了单条 CLI 必需字段。 
    if has_csv == has_single:  # 两者同时存在或同时缺失都属于非法输入。 
        logger.error("detect-flaky requires exactly one input source: either --csv or (--repo-url, --sha, --full-test-name)")  # 明确提示用户当前输入冲突。 
        sys.exit(2)  # 以参数错误退出。 


def _ensure_output_path(args: argparse.Namespace, legacy_mode: bool) -> None:  # 在未指定输出文件时生成默认路径。 
    if args.output is not None:  # 用户已经指定输出文件时无需再生成默认路径。 
        return  # 直接保留用户配置。 
    os.makedirs('results', exist_ok=True)  # 确保默认结果目录存在。 
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')  # 生成用于结果文件名的时间戳。 
    if legacy_mode:  # 兼容旧命令时保留历史默认命名。 
        args.output = f'results/rerun_results_{timestamp}.csv'  # 生成旧版风格默认结果文件名。 
        return  # 兼容模式下到此结束。 
    prefix = 'verify_patch' if args.command == 'verify-patch' else 'detect_flaky'  # 为新子命令选择更清晰的文件名前缀。 
    args.output = f'results/{prefix}_{timestamp}.csv'  # 生成新子命令的默认结果文件名。 


def _build_resume_lookup(requests: List[object]) -> dict:  # 为 resume 逻辑构建请求查找映射。 
    lookup = {}  # 初始化空映射。 
    for request in requests:  # 遍历全部待执行请求。 
        lookup[request.request_key] = request  # 先按稳定请求键建立映射。 
        lookup[request.index] = request  # 再按旧版索引建立兼容映射。 
    return lookup  # 返回构造完成的查找字典。 


def _partition_restored_results(restored_results: List[TestRunResult]) -> Tuple[List[TestRunResult], List[TestRunResult]]:  # 将历史结果拆分为保留跳过和需要重跑两类。 
    skipped_results: List[TestRunResult] = []  # 保存 resume 时继续保留的历史结果。 
    retry_results: List[TestRunResult] = []  # 保存 resume 时需要重新执行的历史结果。 
    for result in restored_results:  # 逐条检查恢复出的历史结果。 
        if _should_rerun_on_resume(result):  # 当前历史状态属于需要自动重跑的失败类型时放入重跑集合。 
            retry_results.append(result)  # 将 clone/build 失败的历史结果加入重跑列表。 
            continue  # 当前结果已经归类完成，继续处理下一条。 
        skipped_results.append(result)  # 其余历史结果继续作为已完成结果保留。 
    return skipped_results, retry_results  # 返回拆分后的两类历史结果。 


def _should_rerun_on_resume(result: TestRunResult) -> bool:  # 判断某条历史结果在 resume 时是否应该自动重跑。 
    return result.status in RESUME_RETRYABLE_STATUSES  # 当前仅对 clone_failed 与 build_failed 自动安排重跑。 


def _format_overall_progress(total_requests: int, all_results: List[TestRunResult], skipped_results: List[TestRunResult], retry_results: List[TestRunResult], active_requests: List[object]) -> str:  # 构造统一的整体进度展示文本。 
    finished_count = len(all_results)  # 当前已经确认完成的总条目数，包含保留的历史结果和本轮新执行结果。 
    skipped_count = len(skipped_results)  # 当前继续保留且不再执行的历史结果数量。 
    retry_count = len(retry_results)  # 当前因 clone/build 失败而被重新排入执行队列的历史结果数量。 
    batch_finished_count = max(0, finished_count - skipped_count)  # 计算本轮实际新执行完成的条目数量。 
    completed_count = sum(1 for result in all_results if result.status == 'completed')  # 统计当前全部已完成结果中真正成功完成的条目数。 
    non_completed_count = finished_count - completed_count  # 统计当前全部已完成结果中仍然处于失败状态的条目数。 
    remaining_count = max(0, total_requests - finished_count)  # 计算当前还剩多少条请求尚未完成。 
    percent = (finished_count / total_requests * 100.0) if total_requests else 100.0  # 计算当前整体进度百分比。 
    return f"Overall progress: {finished_count}/{total_requests} ({percent:.1f}%) | batch_done={batch_finished_count}/{len(active_requests)} | skipped_kept={skipped_count} | rerun_failed={retry_count} | completed={completed_count} | non_completed={non_completed_count} | remaining={remaining_count}"  # 返回适合直接打印到控制台的整体进度摘要。 


def _result_request_key(result: TestRunResult) -> str:  # 从结果对象中提取稳定请求键。 
    return getattr(result.entry, 'request_key', f"legacy:{getattr(result.entry, 'index', '')}")  # 优先使用条目对象自带请求键。 


if __name__ == '__main__':  # 允许直接作为脚本运行当前模块。 
    main()  # 执行统一 CLI 入口。 
