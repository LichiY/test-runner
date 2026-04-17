"""Repository cloning and management."""

from dataclasses import dataclass  # 导入数据类装饰器以结构化返回 Git 诊断信息。
import logging  # 导入日志模块记录 Git 准备过程中的关键阶段。
import os  # 导入路径工具判断工作区与仓库状态。
import shutil  # 导入目录清理工具修复残缺工作区。
import subprocess  # 导入子进程工具执行 Git 命令。
import time  # 导入时间工具实现有限退避重试。
from typing import Optional, Tuple  # 导入可选类型注解工具。

logger = logging.getLogger(__name__)  # 创建当前模块的日志记录器。

DEFAULT_GIT_TIMEOUT = 1800  # 将默认 Git 超时提升到 30 分钟以覆盖大仓库慢网络场景。
PARTIAL_CLONE_FLAGS = ['--filter=blob:none', '--no-checkout']  # 优先使用 partial clone 仅拉取提交与树对象以显著降低首轮 clone 体积。
FALLBACK_CLONE_FLAGS = ['--no-checkout']  # 当远端不支持 partial clone 时回退到普通 no-checkout clone。
PARTIAL_FETCH_FLAGS = ['--tags', '--prune', '--filter=blob:none', 'origin']  # 对已有仓库的 fetch 同样优先走轻量拉取。
FALLBACK_FETCH_FLAGS = ['--tags', '--prune', 'origin']  # 当远端不支持过滤 fetch 时再回退到普通 fetch。


@dataclass  # 定义 Git 准备阶段的结构化返回结果。
class GitPrepareResult:  # 该结构用于向工作流暴露 clone/fetch/checkout 的详细诊断。
    success: bool  # 保存当前 Git 准备阶段是否成功。
    stage: str  # 保存最终停留或完成的 Git 阶段名称。
    message: str  # 保存可直接写入日志或结果文件的诊断消息。
    attempts: int = 0  # 保存当前阶段实际经历的尝试次数。
    reused_existing_repo: bool = False  # 标记是否复用了已有本地仓库。
    repaired_workspace: bool = False  # 标记是否清理过残缺工作区。


def clone_repo(repo_url: str, target_dir: str, sha: str, timeout: int = DEFAULT_GIT_TIMEOUT, max_retries: int = 2) -> GitPrepareResult:  # 克隆仓库并检出到目标提交，同时返回详细诊断信息。
    normalized_target_dir = os.path.abspath(target_dir)  # 先将目标目录规范化为绝对路径。
    repaired_workspace = False  # 记录当前是否修复过残缺工作区。
    total_attempts = max(1, max_retries + 1)  # 统一计算 clone 与 fetch 的总尝试次数。
    try:  # 统一捕获工作区修复阶段的异常。
        if os.path.exists(normalized_target_dir) and not _has_valid_git_repo(normalized_target_dir):  # 发现目录存在但不是有效 Git 仓库时需要先清理。
            logger.warning(f"Incomplete workspace detected at {normalized_target_dir}, removing before clone")  # 记录发现残缺工作区。
            cleanup_ok, cleanup_message = _remove_workspace(normalized_target_dir)  # 尝试删除当前残缺工作区。
            if not cleanup_ok:  # 无法删除残缺工作区时直接返回失败。
                return GitPrepareResult(success=False, stage='workspace_cleanup', message=cleanup_message, attempts=1, repaired_workspace=True)  # 返回工作区清理失败结果。
            repaired_workspace = True  # 标记当前请求已经修复过工作区。
    except Exception as e:  # 捕获工作区修复过程中的意外异常。
        logger.error(f"Workspace inspection failed for {normalized_target_dir}: {e}")  # 记录工作区检查失败。
        return GitPrepareResult(success=False, stage='workspace_inspection', message=f"Failed to inspect workspace {normalized_target_dir}: {e}", attempts=1)  # 返回工作区检查失败结果。
    if _has_valid_git_repo(normalized_target_dir):  # 已有有效 Git 仓库时优先复用本地副本。
        existing_result = _prepare_existing_repo(repo_url=repo_url, target_dir=normalized_target_dir, sha=sha, timeout=timeout, max_retries=max_retries, repaired_workspace=repaired_workspace)  # 尝试在已有仓库上完成清理与检出。
        if existing_result.success:  # 已有仓库准备成功时直接返回。
            return existing_result  # 结束当前 Git 准备流程。
        if existing_result.stage in {'workspace_validation', 'cleanup'}:  # 仓库本地状态损坏时回退到删除并重新 clone。
            logger.warning(f"Existing repository at {normalized_target_dir} is not reusable, recloning from scratch")  # 记录需要放弃本地仓库重建。
            cleanup_ok, cleanup_message = _remove_workspace(normalized_target_dir)  # 尝试删除当前损坏仓库目录。
            if not cleanup_ok:  # 删除失败时无法继续新 clone。
                return GitPrepareResult(success=False, stage='workspace_cleanup', message=cleanup_message, attempts=existing_result.attempts or 1, reused_existing_repo=True, repaired_workspace=True)  # 返回删除损坏仓库失败结果。
            repaired_workspace = True  # 标记当前已经通过删除目录修复工作区。
        else:  # 其余 fetch 或 checkout 失败直接上报，避免误删可诊断现场。
            return existing_result  # 返回已有仓库准备失败的详细信息。
    os.makedirs(os.path.dirname(normalized_target_dir), exist_ok=True)  # 确保目标仓库的父目录存在。
    last_message = f"Failed to clone {repo_url} at {sha}"  # 初始化最终失败信息，供极端分支兜底。
    for attempt in range(total_attempts):  # 对新 clone 执行有限次数的重试。
        attempt_number = attempt + 1  # 转为从 1 开始的人类可读尝试编号。
        if os.path.exists(normalized_target_dir):  # 每次 clone 前都先确保目标目录为空。
            cleanup_ok, cleanup_message = _remove_workspace(normalized_target_dir)  # 删除上一次失败留下的目录残片。
            if not cleanup_ok:  # 目标目录无法清理时后续 clone 没有继续意义。
                return GitPrepareResult(success=False, stage='workspace_cleanup', message=cleanup_message, attempts=attempt_number, repaired_workspace=True)  # 返回目录清理失败结果。
            repaired_workspace = True  # 记录当前请求清理过失败残留目录。
        logger.info(f"Cloning {repo_url} to {normalized_target_dir} (attempt {attempt_number}/{total_attempts})")  # 记录当前 clone 尝试编号。
        try:  # 单独捕获每次 clone 的超时异常。
            clone_result = _run_clone_command(repo_url=repo_url, target_dir=normalized_target_dir, timeout=timeout)  # 优先执行更轻量的 partial clone，并在能力不足时自动回退。
        except subprocess.TimeoutExpired:  # clone 超时时将其视作可恢复的基础设施错误。
            last_message = _format_timeout_message(stage='clone', repo_url=repo_url, sha=sha, timeout=timeout, attempt=attempt_number, total_attempts=total_attempts)  # 构造 clone 超时诊断信息。
            logger.warning(last_message)  # 记录本次 clone 超时。
            if attempt < total_attempts - 1:  # 仍有剩余重试次数时继续。
                _sleep_before_retry(attempt)  # 在下一次 clone 前做短暂退避。
                continue  # 进入下一次 clone 尝试。
            return GitPrepareResult(success=False, stage='clone', message=last_message, attempts=attempt_number, repaired_workspace=repaired_workspace)  # 用尽重试后返回 clone 超时失败。
        if clone_result.returncode != 0:  # clone 返回非零时按恢复性错误分类处理。
            last_message = _format_git_failure(stage='clone', repo_url=repo_url, sha=sha, output=_combined_output(clone_result), attempt=attempt_number, total_attempts=total_attempts)  # 构造本次 clone 失败信息。
            logger.warning(last_message)  # 记录本次 clone 失败原因。
            if attempt < total_attempts - 1 and _is_recoverable_git_error(last_message):  # 仅对网络类或传输类错误执行下一轮 clone。
                _sleep_before_retry(attempt)  # 在下一次 clone 前做有限退避。
                continue  # 继续下一次 clone 尝试。
            return GitPrepareResult(success=False, stage='clone', message=last_message, attempts=attempt_number, repaired_workspace=repaired_workspace)  # 返回不可恢复或最终失败的 clone 结果。
        checkout_result = _checkout_target_sha(repo_url=repo_url, target_dir=normalized_target_dir, sha=sha, timeout=timeout, max_retries=max_retries, reused_existing_repo=False, repaired_workspace=repaired_workspace)  # clone 成功后继续检出目标提交。
        if checkout_result.success:  # 检出成功时整个准备流程完成。
            return checkout_result  # 返回成功的 Git 准备结果。
        if checkout_result.stage == 'fetch' and attempt < total_attempts - 1 and _is_recoverable_git_error(checkout_result.message):  # fetch 失败且仍可通过全新 clone 再试时继续。
            logger.warning("Fresh clone finished but fetch for target SHA failed, retrying full clone")  # 记录需要通过重新 clone 再试的情况。
            _sleep_before_retry(attempt)  # 在重新 clone 前做有限退避。
            continue  # 继续下一轮新的 clone 尝试。
        return checkout_result  # 其余 checkout 或 fetch 失败直接向上返回详细信息。
    return GitPrepareResult(success=False, stage='clone', message=last_message, attempts=total_attempts, repaired_workspace=repaired_workspace)  # 理论兜底返回最终 clone 失败结果。


def reset_repo(repo_dir: str, timeout: int = 120) -> bool:  # 将仓库恢复到干净状态以便后续继续复用。
    try:  # 捕获 reset 过程中的异常。
        _run_git(repo_dir, ['git', 'checkout', '--', '.'], timeout=timeout)  # 尝试撤销工作区中的文件修改。
        _run_git(repo_dir, ['git', 'clean', '-fd'], timeout=timeout)  # 尝试删除未跟踪文件和目录。
        return True  # 成功执行完最佳努力清理后返回成功。
    except Exception as e:  # 捕获 reset 过程中的异常。
        logger.error(f"Reset failed: {e}")  # 记录仓库复位失败原因。
        return False  # 将 reset 失败反馈给调用方。


def ensure_revision_available(repo_dir: str, revision: str, timeout: int = DEFAULT_GIT_TIMEOUT, max_retries: int = 1) -> Tuple[bool, str]:  # 确保指定 revision 在当前本地仓库里可读，供 fixed_sha 辅助检索等场景复用。
    normalized_revision = (revision or '').strip()  # 先规整待检查的 revision 文本。
    if not normalized_revision:  # 缺失 revision 时无法继续检查。
        return False, 'No revision provided'  # 返回明确错误信息，避免上层误判成 Git 读取失败。
    probe_result = _run_git(repo_dir, ['git', 'cat-file', '-e', f'{normalized_revision}^{{commit}}'], timeout=timeout)  # 先探测当前本地仓库是否已经包含该提交对象。
    if probe_result.returncode == 0:  # 本地已经可读时无需再次 fetch。
        return True, f"Revision {normalized_revision[:8]} already available"  # 直接返回成功结果。
    total_attempts = max(1, max_retries + 1)  # 统一计算“按 revision fetch”的总尝试次数。
    last_message = _format_git_failure(stage='fetch_revision', repo_url='origin', sha=normalized_revision, output=_combined_output(probe_result), attempt=1, total_attempts=total_attempts)  # 用首次 probe 失败初始化兜底消息。
    for attempt in range(total_attempts):  # 对按 revision fetch 执行有限次数的重试。
        attempt_number = attempt + 1  # 转成人类可读的尝试编号。
        try:  # 单独捕获按 revision fetch 的超时异常。
            fetch_result = _run_git(repo_dir, ['git', 'fetch', 'origin', normalized_revision], timeout=timeout)  # 优先尝试只拉取目标 revision，避免再次下载整个远端引用集合。
        except subprocess.TimeoutExpired:  # 网络慢或远端卡住时把它视为可恢复的 Git 问题。
            last_message = _format_timeout_message(stage='fetch_revision', repo_url='origin', sha=normalized_revision, timeout=timeout, attempt=attempt_number, total_attempts=total_attempts)  # 统一构造超时消息。
            if attempt < total_attempts - 1:  # 仍有剩余尝试次数时继续。
                _sleep_before_retry(attempt)  # 在下一轮之前做短退避。
                continue  # 进入下一次尝试。
            return False, last_message  # 用尽重试后返回失败。
        if fetch_result.returncode != 0:  # 直接按 revision fetch 失败时记录当前错误并在可恢复情况下继续。
            last_message = _format_git_failure(stage='fetch_revision', repo_url='origin', sha=normalized_revision, output=_combined_output(fetch_result), attempt=attempt_number, total_attempts=total_attempts)  # 构造当前失败消息。
            if attempt < total_attempts - 1 and _is_recoverable_git_error(last_message):  # 只对可恢复错误继续重试。
                _sleep_before_retry(attempt)  # 在下一轮前做退避。
                continue  # 继续下一次尝试。
            return False, last_message  # 其余情况直接返回当前失败消息。
        probe_result = _run_git(repo_dir, ['git', 'cat-file', '-e', f'{normalized_revision}^{{commit}}'], timeout=timeout)  # fetch 成功后再次确认当前 revision 真的已经可读。
        if probe_result.returncode == 0:  # revision 已经落到本地对象库时返回成功。
            return True, f"Fetched revision {normalized_revision[:8]} from origin"  # 返回成功消息。
        last_message = _format_git_failure(stage='fetch_revision', repo_url='origin', sha=normalized_revision, output=_combined_output(probe_result), attempt=attempt_number, total_attempts=total_attempts)  # 记录“fetch 后仍不可读”的异常场景。
    return False, last_message  # 理论兜底返回最终失败消息。


def list_files_at_revision(repo_dir: str, revision: str, path_prefix: str = '', timeout: int = 120) -> Tuple[bool, list[str] | str]:  # 列出某个 revision 下指定前缀路径内的文件，供 fixed_sha helper 检索复用。
    normalized_revision = (revision or '').strip()  # 先规整 revision 文本。
    if not normalized_revision:  # 缺失 revision 时无法继续。
        return False, 'No revision provided'  # 返回明确错误。
    normalized_prefix = (path_prefix or '').strip().strip('/')  # 统一规整路径前缀，避免多余斜杠影响 Git pathspec。
    cmd = ['git', 'ls-tree', '-r', '--name-only', normalized_revision]  # 先准备列树命令。
    if normalized_prefix:  # 只有真的给出路径前缀时才追加 pathspec。
        cmd.extend(['--', normalized_prefix])  # 用 pathspec 将扫描范围限制到目标目录。
    result = _run_git(repo_dir, cmd, timeout=timeout)  # 执行 Git 列树命令。
    if result.returncode != 0:  # Git 读取失败时返回带阶段信息的错误消息。
        return False, _format_git_failure(stage='ls_tree', repo_url='origin', sha=normalized_revision, output=_combined_output(result), attempt=1, total_attempts=1)  # 将错误包装成可直接透传的诊断文本。
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]  # 过滤空行并保持 Git 返回顺序。
    return True, files  # 返回当前 revision 下的文件列表。


def read_file_at_revision(repo_dir: str, revision: str, relative_path: str, timeout: int = 120) -> Tuple[bool, str]:  # 读取某个 revision 下的单个文件内容，供 fixed_sha helper 回溯和离线分析复用。
    normalized_revision = (revision or '').strip()  # 先规整 revision 文本。
    normalized_path = (relative_path or '').strip().lstrip('./').replace(os.sep, '/')  # 统一规整相对路径，避免 Windows 风格分隔符影响 Git show。
    if not normalized_revision or not normalized_path:  # revision 或路径缺失时都无法继续。
        return False, 'Missing revision or relative path'  # 返回明确错误信息。
    result = _run_git(repo_dir, ['git', 'show', f'{normalized_revision}:{normalized_path}'], timeout=timeout)  # 直接从对象库中读取目标文件内容，不污染当前工作树。
    if result.returncode != 0:  # 读取失败时包装成统一错误消息。
        return False, _format_git_failure(stage='git_show', repo_url='origin', sha=f'{normalized_revision}:{normalized_path}', output=_combined_output(result), attempt=1, total_attempts=1)  # 返回包含具体 revision:path 的错误。
    return True, result.stdout  # 返回成功读取到的文件内容。


def _prepare_existing_repo(repo_url: str, target_dir: str, sha: str, timeout: int, max_retries: int, repaired_workspace: bool) -> GitPrepareResult:  # 在已有仓库副本上执行校验、清理和检出。
    logger.info(f"Repo already exists at {target_dir}, attempting reuse for {sha[:8]}")  # 记录当前正在复用已有仓库。
    validation_result = _run_git(target_dir, ['git', 'rev-parse', '--is-inside-work-tree'], timeout=timeout)  # 先验证当前目录确实还是有效 Git 仓库。
    if validation_result.returncode != 0:  # 本地目录已损坏或 `.git` 结构不完整时标记为不可复用。
        return GitPrepareResult(success=False, stage='workspace_validation', message=_format_git_failure(stage='workspace_validation', repo_url=repo_url, sha=sha, output=_combined_output(validation_result), attempt=1, total_attempts=1), attempts=1, reused_existing_repo=True, repaired_workspace=repaired_workspace)  # 返回仓库校验失败结果。
    for cleanup_cmd in [['git', 'checkout', '--', '.'], ['git', 'clean', '-fd']]:  # 依次执行最佳努力清理命令。
        cleanup_result = _run_git(target_dir, cleanup_cmd, timeout=timeout)  # 执行当前清理命令。
        if cleanup_result.returncode != 0:  # 清理失败时放弃继续复用当前仓库。
            return GitPrepareResult(success=False, stage='cleanup', message=_format_git_failure(stage='cleanup', repo_url=repo_url, sha=sha, output=_combined_output(cleanup_result), attempt=1, total_attempts=1), attempts=1, reused_existing_repo=True, repaired_workspace=repaired_workspace)  # 返回本地清理失败结果。
    return _checkout_target_sha(repo_url=repo_url, target_dir=target_dir, sha=sha, timeout=timeout, max_retries=max_retries, reused_existing_repo=True, repaired_workspace=repaired_workspace)  # 在清理完成后继续检出目标提交。


def _checkout_target_sha(repo_url: str, target_dir: str, sha: str, timeout: int, max_retries: int, reused_existing_repo: bool, repaired_workspace: bool) -> GitPrepareResult:  # 负责检出目标提交并在需要时执行 fetch。
    initial_checkout = _run_git(target_dir, ['git', 'checkout', sha], timeout=timeout)  # 先尝试直接检出目标提交。
    if initial_checkout.returncode == 0:  # 目标提交已在本地可用时无需额外 fetch。
        return GitPrepareResult(success=True, stage='ready', message=_success_message(reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace), attempts=1, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回直接检出成功结果。
    logger.info(f"Initial checkout of {sha[:8]} failed, fetching remote updates before retrying")  # 记录需要通过 fetch 更新远端引用。
    fetch_result = _fetch_origin(repo_url=repo_url, target_dir=target_dir, sha=sha, timeout=timeout, max_retries=max_retries, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 对 origin 执行有限次数的 fetch 重试。
    if not fetch_result.success:  # fetch 最终失败时直接返回。
        return fetch_result  # 将 fetch 阶段的详细错误向上透传。
    total_checkout_attempts = max(1, max_retries + 1)  # 计算 fetch 之后 checkout 的总尝试次数。
    last_message = _format_git_failure(stage='checkout', repo_url=repo_url, sha=sha, output=_combined_output(initial_checkout), attempt=1, total_attempts=total_checkout_attempts)  # 用首次 checkout 的失败信息初始化兜底消息。
    for attempt in range(total_checkout_attempts):  # 对 checkout 阶段执行有限次数的重试。
        attempt_number = attempt + 1  # 转为从 1 开始的人类可读尝试编号。
        try:  # 单独捕获 checkout 的超时异常。
            checkout_result = _run_git(target_dir, ['git', 'checkout', sha], timeout=timeout)  # 执行当前 checkout 尝试。
        except subprocess.TimeoutExpired:  # checkout 超时时同样返回阶段性错误。
            last_message = _format_timeout_message(stage='checkout', repo_url=repo_url, sha=sha, timeout=timeout, attempt=attempt_number, total_attempts=total_checkout_attempts)  # 构造 checkout 超时消息。
            logger.warning(last_message)  # 记录 checkout 超时。
            if attempt < total_checkout_attempts - 1:  # 仍有剩余重试时继续。
                _sleep_before_retry(attempt)  # 在下一次 checkout 前退避。
                continue  # 进入下一次 checkout 尝试。
            return GitPrepareResult(success=False, stage='checkout', message=last_message, attempts=attempt_number, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回 checkout 超时失败结果。
        if checkout_result.returncode == 0:  # checkout 成功时整个 Git 准备流程结束。
            return GitPrepareResult(success=True, stage='ready', message=_success_message(reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace), attempts=attempt_number, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回成功结果。
        last_message = _format_git_failure(stage='checkout', repo_url=repo_url, sha=sha, output=_combined_output(checkout_result), attempt=attempt_number, total_attempts=total_checkout_attempts)  # 记录当前 checkout 失败原因。
        logger.warning(last_message)  # 输出当前 checkout 失败日志。
        if attempt < total_checkout_attempts - 1 and _is_recoverable_git_error(last_message):  # 仅对超时或锁竞争等可恢复错误继续重试。
            _sleep_before_retry(attempt)  # 在下一次 checkout 前执行有限退避。
            continue  # 继续下一次 checkout 尝试。
        return GitPrepareResult(success=False, stage='checkout', message=last_message, attempts=attempt_number, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回最终 checkout 失败结果。
    return GitPrepareResult(success=False, stage='checkout', message=last_message, attempts=total_checkout_attempts, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 理论兜底返回 checkout 失败结果。


def _fetch_origin(repo_url: str, target_dir: str, sha: str, timeout: int, max_retries: int, reused_existing_repo: bool, repaired_workspace: bool) -> GitPrepareResult:  # 对 origin 执行有限次数的 fetch 重试。
    total_attempts = max(1, max_retries + 1)  # 计算 fetch 阶段总尝试次数。
    last_message = f"Failed to fetch origin for {repo_url} at {sha}"  # 初始化 fetch 阶段的兜底错误信息。
    for attempt in range(total_attempts):  # 按有限次数执行 fetch。
        attempt_number = attempt + 1  # 转为从 1 开始的人类可读尝试编号。
        logger.info(f"Fetching origin for {sha[:8]} (attempt {attempt_number}/{total_attempts})")  # 记录当前 fetch 尝试。
        try:  # 单独捕获 fetch 的超时异常。
            fetch_result = _run_fetch_command(target_dir=target_dir, timeout=timeout)  # 优先执行轻量 fetch，并在远端不支持过滤时安全回退。
        except subprocess.TimeoutExpired:  # fetch 超时通常属于可恢复网络问题。
            last_message = _format_timeout_message(stage='fetch', repo_url=repo_url, sha=sha, timeout=timeout, attempt=attempt_number, total_attempts=total_attempts)  # 构造 fetch 超时消息。
            logger.warning(last_message)  # 记录当前 fetch 超时。
            if attempt < total_attempts - 1:  # 剩余重试次数存在时继续。
                _sleep_before_retry(attempt)  # 在下一次 fetch 前执行有限退避。
                continue  # 进入下一次 fetch 尝试。
            return GitPrepareResult(success=False, stage='fetch', message=last_message, attempts=attempt_number, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回最终 fetch 超时结果。
        if fetch_result.returncode == 0:  # fetch 成功时直接返回。
            return GitPrepareResult(success=True, stage='fetch', message=f"Fetched remote updates for {sha[:8]}", attempts=attempt_number, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回成功的 fetch 结果。
        last_message = _format_git_failure(stage='fetch', repo_url=repo_url, sha=sha, output=_combined_output(fetch_result), attempt=attempt_number, total_attempts=total_attempts)  # 构造当前 fetch 失败消息。
        logger.warning(last_message)  # 记录当前 fetch 失败原因。
        if attempt < total_attempts - 1 and _is_recoverable_git_error(last_message):  # 仅对网络类或传输类错误继续 fetch 重试。
            _sleep_before_retry(attempt)  # 在下一次 fetch 前做有限退避。
            continue  # 继续下一次 fetch 尝试。
        return GitPrepareResult(success=False, stage='fetch', message=last_message, attempts=attempt_number, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 返回不可恢复或最终失败的 fetch 结果。
    return GitPrepareResult(success=False, stage='fetch', message=last_message, attempts=total_attempts, reused_existing_repo=reused_existing_repo, repaired_workspace=repaired_workspace)  # 理论兜底返回 fetch 失败结果。


def _has_valid_git_repo(target_dir: str) -> bool:  # 判断目标目录是否包含可复用的 `.git` 元数据目录。
    return os.path.isdir(os.path.join(target_dir, '.git'))  # 仅在 `.git` 目录存在时认为当前目录是 Git 仓库。


def _remove_workspace(target_dir: str) -> tuple[bool, str]:  # 删除残缺工作区或损坏仓库目录。
    try:  # 捕获删除目录或文件时的异常。
        if os.path.isdir(target_dir) and not os.path.islink(target_dir):  # 普通目录需要递归删除。
            shutil.rmtree(target_dir)  # 删除整个目录树。
        elif os.path.exists(target_dir):  # 其余文件或符号链接按单文件删除。
            os.remove(target_dir)  # 删除单个路径节点。
        return True, f"Removed workspace {target_dir}"  # 返回删除成功消息。
    except Exception as e:  # 捕获删除过程中的异常。
        logger.error(f"Failed to remove workspace {target_dir}: {e}")  # 记录工作区删除失败原因。
        return False, f"Failed to remove workspace {target_dir}: {e}"  # 返回工作区删除失败消息。


def _success_message(reused_existing_repo: bool, repaired_workspace: bool) -> str:  # 根据当前路径选择成功诊断文本。
    if reused_existing_repo and repaired_workspace:  # 同时复用过仓库并修理过目录时给出完整说明。
        return "Reused existing repository after repairing workspace and checked out target SHA"  # 返回包含复用与修复信息的成功消息。
    if reused_existing_repo:  # 单纯复用了已有仓库时说明没有重新 clone。
        return "Reused existing repository and checked out target SHA"  # 返回复用仓库成功消息。
    if repaired_workspace:  # 发生过目录修复但最终走的是全新 clone。
        return "Repaired workspace, recloned repository, and checked out target SHA"  # 返回修复后重新 clone 成功消息。
    return "Cloned repository and checked out target SHA"  # 返回最普通的 clone 成功消息。


def _sleep_before_retry(attempt: int) -> None:  # 在 Git 重试前执行有限指数退避。
    time.sleep(min(5, 1 + attempt))  # 将退避时间控制在 1 到 5 秒之间。


def _run_clone_command(repo_url: str, target_dir: str, timeout: int) -> subprocess.CompletedProcess:  # 按优先级执行 clone 命令并在能力不支持时自动回退。
    last_result: Optional[subprocess.CompletedProcess] = None  # 保存最后一次 clone 结果以便在所有候选都失败时返回。
    for clone_cmd in _clone_command_variants(repo_url=repo_url, target_dir=target_dir):  # 依次尝试 partial clone 与普通 no-checkout clone 两种候选。
        clone_result = _run_git(None, clone_cmd, timeout=timeout)  # 执行当前 clone 候选命令。
        if clone_result.returncode == 0:  # 当前候选成功时立即返回结果。
            return clone_result  # 将成功的 clone 结果交回调用方。
        last_result = clone_result  # 记录当前失败结果便于最后兜底返回。
        if _is_partial_clone_capability_error(_combined_output(clone_result)) and '--filter=blob:none' in clone_cmd:  # 只有 partial clone 能力不支持时才值得尝试普通 clone 回退。
            logger.warning("Remote does not support partial clone, falling back to standard no-checkout clone")  # 记录当前发生了 partial clone 回退。
            continue  # 继续尝试下一条 clone 候选命令。
        return clone_result  # 其余错误直接返回，避免用更重的 clone 掩盖真实故障。
    return last_result or subprocess.CompletedProcess(args=['git', 'clone'], returncode=1, stdout='', stderr='No clone command variants executed')  # 理论兜底返回最后一次失败结果。


def _clone_command_variants(repo_url: str, target_dir: str) -> list[list[str]]:  # 生成按优先级排列的 clone 命令候选列表。
    return [  # 先尝试 partial clone，再回退到普通 no-checkout clone。
        ['git', 'clone', *PARTIAL_CLONE_FLAGS, repo_url, target_dir],  # partial clone 会显著降低大仓库首次 clone 所需的数据量。
        ['git', 'clone', *FALLBACK_CLONE_FLAGS, repo_url, target_dir],  # 回退路径继续保留 no-checkout 以避免无意义的首次工作树展开。
    ]  # 返回当前 clone 候选序列。


def _run_fetch_command(target_dir: str, timeout: int) -> subprocess.CompletedProcess:  # 按优先级执行 fetch 命令并在能力不支持时自动回退。
    last_result: Optional[subprocess.CompletedProcess] = None  # 保存最后一次 fetch 结果以便全部失败时返回。
    for fetch_cmd in _fetch_command_variants():  # 依次尝试 partial fetch 与普通 fetch。
        fetch_result = _run_git(target_dir, fetch_cmd, timeout=timeout)  # 执行当前 fetch 候选命令。
        if fetch_result.returncode == 0:  # 当前 fetch 候选成功时立即返回。
            return fetch_result  # 将成功的 fetch 结果交回调用方。
        last_result = fetch_result  # 记录当前失败结果以供兜底。
        if _is_partial_clone_capability_error(_combined_output(fetch_result)) and '--filter=blob:none' in fetch_cmd:  # 只有过滤 fetch 能力不支持时才值得回退到普通 fetch。
            logger.warning("Remote does not support filtered fetch, falling back to standard fetch")  # 记录当前发生了 filtered fetch 回退。
            continue  # 继续尝试普通 fetch。
        return fetch_result  # 其余 fetch 错误直接返回给上层处理。
    return last_result or subprocess.CompletedProcess(args=['git', 'fetch'], returncode=1, stdout='', stderr='No fetch command variants executed')  # 理论兜底返回最后一次失败结果。


def _fetch_command_variants() -> list[list[str]]:  # 生成按优先级排列的 fetch 命令候选列表。
    return [  # 先尝试 filtered fetch，再回退到普通 fetch。
        ['git', 'fetch', *PARTIAL_FETCH_FLAGS],  # filtered fetch 在 partial clone 仓库中可以继续保持低数据量。
        ['git', 'fetch', *FALLBACK_FETCH_FLAGS],  # 回退路径保留原有 fetch 语义确保兼容性。
    ]  # 返回当前 fetch 候选序列。


def _combined_output(result: subprocess.CompletedProcess) -> str:  # 合并标准输出与错误输出以便统一诊断。
    return '\n'.join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()  # 返回去掉空白后的组合输出文本。


def _format_timeout_message(stage: str, repo_url: str, sha: str, timeout: int, attempt: int, total_attempts: int) -> str:  # 构造 Git 阶段超时时的统一错误信息。
    return f"{stage} timed out after {timeout}s while preparing {repo_url} at {sha} (attempt {attempt}/{total_attempts})"  # 返回包含阶段、仓库、提交与尝试编号的超时消息。


def _format_git_failure(stage: str, repo_url: str, sha: str, output: str, attempt: int, total_attempts: int) -> str:  # 构造 Git 阶段失败时的统一错误信息。
    normalized_output = _tail_text(output, limit=600) or 'No git output captured'  # 仅保留尾部关键信息避免错误消息过长。
    return f"{stage} failed while preparing {repo_url} at {sha} (attempt {attempt}/{total_attempts}): {normalized_output}"  # 返回包含阶段、仓库、提交与错误尾部的失败消息。


def _tail_text(output: str, limit: int = 600) -> str:  # 截取日志尾部用于写入紧凑错误信息。
    normalized_output = (output or '').strip()  # 先将空值与首尾空白统一处理掉。
    if len(normalized_output) <= limit:  # 短消息无需再裁剪。
        return normalized_output  # 直接返回完整消息。
    return normalized_output[-limit:]  # 仅保留尾部更有诊断价值的内容。


def _is_recoverable_git_error(output: str) -> bool:  # 判断 Git 错误是否更像瞬时网络或传输问题。
    output_lower = (output or '').lower()  # 统一转小写便于做大小写无关匹配。
    recoverable_indicators = [  # 这些关键字大多来自网络抖动、TLS 传输失败或临时锁竞争。
        'connection timed out',  # 常见的连接超时错误。
        'timed out',  # 更泛化的超时提示。
        'connection reset',  # 连接被远端重置。
        'connection refused',  # 远端暂时拒绝连接。
        'early eof',  # Git 大仓库传输时常见的 early EOF。
        'unexpected disconnect',  # 远端异常断开连接。
        'remote end hung up unexpectedly',  # Git 远端提前断连。
        'rpc failed',  # Git RPC 传输失败。
        'http/2 stream',  # HTTP/2 传输异常。
        'transfer closed with outstanding read data remaining',  # 传输过程中提前关闭连接。
        'ssl_read',  # OpenSSL 读取错误。
        'ssl peer shut down incorrectly',  # TLS 连接被异常关闭。
        'gnutls recv error',  # GnuTLS 接收错误。
        'failed to connect',  # 连接远端失败。
        'could not resolve host',  # DNS 解析失败。
        'network is unreachable',  # 网络不可达。
        'unable to access',  # Git 访问远端失败的笼统提示。
        'temporary failure',  # 临时性基础设施错误。
        'resource temporarily unavailable',  # 临时资源不可用。
        'index.lock',  # Git 锁文件冲突通常可通过重试恢复。
        'operation too slow',  # 大仓库或慢网络下 libcurl 常见的速度过慢错误。
    ]  # 完成可恢复错误关键字列表定义。
    return any(indicator in output_lower for indicator in recoverable_indicators)  # 命中任一关键字时判定为可恢复错误。


def _is_partial_clone_capability_error(output: str) -> bool:  # 判断失败是否只是远端不支持 partial clone/filter 能力。
    output_lower = (output or '').lower()  # 统一转小写便于做大小写无关匹配。
    capability_markers = [  # 这些提示通常意味着过滤能力不可用，而不是网络或权限故障。
        'filtering not recognized by server',  # 旧版远端可能直接提示不识别 filtering。
        'server does not support filter',  # GitHub 兼容实现常见的能力提示。
        'filter-spec',  # 一些 Git 版本会在 filter-spec 解析失败时给出该提示。
        'did not send all necessary objects',  # 某些 partial clone 兼容问题会表现为 promisor 对象不完整。
    ]  # 完成 partial clone 能力不足关键字列表定义。
    return any(marker in output_lower for marker in capability_markers)  # 命中任一能力关键字时判为可回退错误。


def _run_git(repo_dir: Optional[str], cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:  # 在给定目录中执行 Git 命令并禁用交互式提示。
    env = os.environ.copy()  # 复制当前环境变量以便在其上追加 Git 选项。
    env.setdefault('GIT_TERMINAL_PROMPT', '0')  # 禁止 Git 在非交互场景下等待输入凭据。
    env.setdefault('GIT_HTTP_LOW_SPEED_LIMIT', '1')  # 将 Git HTTP 低速阈值设置为 1 字节每秒，避免大仓库慢网络时被默认低速保护过早终止。
    env.setdefault('GIT_HTTP_LOW_SPEED_TIME', str(timeout))  # 将 Git HTTP 低速容忍时间对齐到当前命令超时，避免 libcurl 在 subprocess 超时之前先于 300 秒中断。
    return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=timeout, env=env)  # 返回命令执行结果供上层分类处理。
