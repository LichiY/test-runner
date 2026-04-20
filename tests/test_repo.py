import subprocess  # 导入子进程结果类型用于伪造 Git 命令执行结果。
import tempfile  # 导入临时目录工具用于构造隔离工作区。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试目录创建。
from unittest import mock  # 导入 mock 以便隔离真实 Git 命令执行。

from rerun_tool.repo import (_run_git, clone_repo, ensure_revision_available,  # 导入待测的 Git 准备入口函数与底层 Git 执行器。
                             list_files_at_revision, read_file_at_revision, _remove_workspace)  # 导入 fixed_sha 辅助读取函数与工作区清理 helper。


class RepoBehaviorTests(unittest.TestCase):  # 测试 Git clone、fetch 与 checkout 加固逻辑。
    def test_clone_repo_retries_after_incomplete_workspace_and_recoverable_clone_failure(self):  # 验证残缺工作区会被清理，且可恢复 clone 错误会触发重试。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            target_dir = Path(tmp_dir) / 'workspace' / 'demo'  # 构造目标仓库目录路径。
            target_dir.mkdir(parents=True)  # 先创建一个残缺工作区目录。
            (target_dir / 'broken.txt').write_text('broken', encoding='utf-8')  # 在残缺目录中写入无关文件模拟污染现场。
            clone_fail = subprocess.CompletedProcess(args=['git', 'clone'], returncode=128, stdout='', stderr='RPC failed; early EOF')  # 伪造一次可恢复的网络型 clone 失败。
            clone_success = subprocess.CompletedProcess(args=['git', 'clone'], returncode=0, stdout='cloned', stderr='')  # 伪造第二次 clone 成功。
            checkout_success = subprocess.CompletedProcess(args=['git', 'checkout'], returncode=0, stdout='', stderr='')  # 伪造 clone 之后的 checkout 成功。
            with mock.patch('rerun_tool.repo._run_git', side_effect=[clone_fail, clone_success, checkout_success]) as mocked_git, mock.patch('rerun_tool.repo.time.sleep', return_value=None):  # 拦截 Git 命令并避免测试等待实际退避时间。
                result = clone_repo('https://example.com/repo.git', str(target_dir), 'a' * 40, timeout=30, max_retries=1)  # 执行带一次重试的 Git 准备流程。
        self.assertTrue(result.success)  # 断言最终会在重试后准备成功。
        self.assertTrue(result.repaired_workspace)  # 断言残缺工作区会被先清理掉。
        self.assertIn('Repaired workspace', result.message)  # 断言成功消息会保留“曾修复工作区”的诊断信息。
        self.assertEqual(mocked_git.call_count, 3)  # 断言当前流程经历了一次失败 clone、一次成功 clone 和一次 checkout。
        self.assertEqual(mocked_git.call_args_list[0].args[1][:2], ['git', 'clone'])  # 断言第一次调用确实是 clone 命令。
        self.assertEqual(mocked_git.call_args_list[1].args[1][:2], ['git', 'clone'])  # 断言第二次调用仍然是重试后的 clone 命令。
        self.assertEqual(mocked_git.call_args_list[2].args[1][:2], ['git', 'checkout'])  # 断言 clone 成功后会继续执行 checkout。

    def test_clone_repo_fetches_before_reusing_existing_repo(self):  # 验证已有仓库检出失败时会先 fetch 再重试 checkout。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            target_dir = Path(tmp_dir) / 'demo'  # 构造目标仓库目录路径。
            (target_dir / '.git').mkdir(parents=True)  # 创建最小 `.git` 目录以模拟已有仓库。
            validation_success = subprocess.CompletedProcess(args=['git', 'rev-parse'], returncode=0, stdout='true', stderr='')  # 伪造仓库校验成功。
            cleanup_checkout_success = subprocess.CompletedProcess(args=['git', 'checkout', '--', '.'], returncode=0, stdout='', stderr='')  # 伪造工作区清理成功。
            cleanup_clean_success = subprocess.CompletedProcess(args=['git', 'clean', '-fd'], returncode=0, stdout='', stderr='')  # 伪造未跟踪文件清理成功。
            initial_checkout_fail = subprocess.CompletedProcess(args=['git', 'checkout'], returncode=1, stdout='', stderr='fatal: reference is not a tree')  # 伪造目标提交当前未在本地可用。
            fetch_success = subprocess.CompletedProcess(args=['git', 'fetch'], returncode=0, stdout='fetched', stderr='')  # 伪造 fetch origin 成功。
            final_checkout_success = subprocess.CompletedProcess(args=['git', 'checkout'], returncode=0, stdout='', stderr='')  # 伪造 fetch 后 checkout 成功。
            with mock.patch('rerun_tool.repo._run_git', side_effect=[validation_success, cleanup_checkout_success, cleanup_clean_success, initial_checkout_fail, fetch_success, final_checkout_success]) as mocked_git:  # 拦截全部 Git 子命令。
                result = clone_repo('https://example.com/repo.git', str(target_dir), 'b' * 40, timeout=30, max_retries=0)  # 执行仅允许一次 fetch/check-out 流程的 Git 准备。
        self.assertTrue(result.success)  # 断言已有仓库会在 fetch 后成功被复用。
        self.assertTrue(result.reused_existing_repo)  # 断言结果会标记这是复用已有仓库的成功路径。
        self.assertIn('Reused existing repository', result.message)  # 断言成功消息会明确说明复用了已有仓库。
        self.assertEqual(mocked_git.call_args_list[4].args[1][:2], ['git', 'fetch'])  # 断言在首次 checkout 失败后确实执行了 fetch。
        self.assertEqual(mocked_git.call_args_list[5].args[1][:2], ['git', 'checkout'])  # 断言 fetch 之后会再次执行 checkout。

    def test_clone_repo_returns_detailed_checkout_error_after_fetch(self):  # 验证 fetch 之后仍然 checkout 失败时会返回具体阶段和错误尾部。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            target_dir = Path(tmp_dir) / 'demo'  # 构造目标仓库目录路径。
            (target_dir / '.git').mkdir(parents=True)  # 创建最小 `.git` 目录以模拟已有仓库。
            validation_success = subprocess.CompletedProcess(args=['git', 'rev-parse'], returncode=0, stdout='true', stderr='')  # 伪造仓库校验成功。
            cleanup_checkout_success = subprocess.CompletedProcess(args=['git', 'checkout', '--', '.'], returncode=0, stdout='', stderr='')  # 伪造工作区清理成功。
            cleanup_clean_success = subprocess.CompletedProcess(args=['git', 'clean', '-fd'], returncode=0, stdout='', stderr='')  # 伪造未跟踪文件清理成功。
            initial_checkout_fail = subprocess.CompletedProcess(args=['git', 'checkout'], returncode=1, stdout='', stderr='fatal: reference is not a tree')  # 伪造首次 checkout 失败。
            fetch_success = subprocess.CompletedProcess(args=['git', 'fetch'], returncode=0, stdout='fetched', stderr='')  # 伪造 fetch origin 成功。
            final_checkout_fail = subprocess.CompletedProcess(args=['git', 'checkout'], returncode=1, stdout='', stderr='fatal: reference is not a tree')  # 伪造 fetch 之后仍然 checkout 失败。
            with mock.patch('rerun_tool.repo._run_git', side_effect=[validation_success, cleanup_checkout_success, cleanup_clean_success, initial_checkout_fail, fetch_success, final_checkout_fail]):  # 拦截全部 Git 子命令并让最终 checkout 失败。
                result = clone_repo('https://example.com/repo.git', str(target_dir), 'c' * 40, timeout=30, max_retries=0)  # 执行当前 Git 准备流程。
        self.assertFalse(result.success)  # 断言最终结果会被标记为失败。
        self.assertEqual(result.stage, 'checkout')  # 断言失败阶段会被明确记录为 checkout。
        self.assertTrue(result.reused_existing_repo)  # 断言当前失败路径仍然属于复用已有仓库的场景。
        self.assertIn('reference is not a tree', result.message)  # 断言错误消息会保留关键 Git 尾部信息。
        self.assertIn('attempt 1/1', result.message)  # 断言错误消息会标明阶段性的尝试次数。

    def test_clone_repo_falls_back_when_remote_lacks_partial_clone_support(self):  # 验证远端不支持 partial clone 时会自动回退到普通 no-checkout clone。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            target_dir = Path(tmp_dir) / 'workspace' / 'demo'  # 构造目标仓库目录路径。
            partial_clone_fail = subprocess.CompletedProcess(args=['git', 'clone'], returncode=128, stdout='', stderr='fatal: server does not support filter')  # 伪造远端不支持 partial clone 的失败结果。
            fallback_clone_success = subprocess.CompletedProcess(args=['git', 'clone'], returncode=0, stdout='cloned', stderr='')  # 伪造回退到普通 clone 后成功。
            checkout_success = subprocess.CompletedProcess(args=['git', 'checkout'], returncode=0, stdout='', stderr='')  # 伪造 clone 之后的 checkout 成功。
            with mock.patch('rerun_tool.repo._run_git', side_effect=[partial_clone_fail, fallback_clone_success, checkout_success]) as mocked_git:  # 拦截 Git 命令并让第二条 clone 候选成功。
                result = clone_repo('https://example.com/repo.git', str(target_dir), 'd' * 40, timeout=30, max_retries=0)  # 执行 Git 准备流程并触发 partial clone 回退。
        self.assertTrue(result.success)  # 断言最终会在回退到普通 clone 后准备成功。
        self.assertIn('--filter=blob:none', mocked_git.call_args_list[0].args[1])  # 断言第一条 clone 候选确实使用了 partial clone 过滤参数。
        self.assertNotIn('--filter=blob:none', mocked_git.call_args_list[1].args[1])  # 断言第二条 clone 候选已经回退为普通 no-checkout clone。
        self.assertEqual(mocked_git.call_args_list[2].args[1][:2], ['git', 'checkout'])  # 断言回退 clone 成功后仍会继续执行 checkout。

    def test_run_git_aligns_http_low_speed_time_with_timeout(self):  # 验证底层 Git 执行器会把 HTTP 低速超时对齐到当前命令超时。
        fake_result = subprocess.CompletedProcess(args=['git'], returncode=0, stdout='ok', stderr='')  # 伪造一个最小成功结果供 _run_git 返回。
        with mock.patch('rerun_tool.repo.subprocess.run', return_value=fake_result) as mocked_run:  # 拦截底层 subprocess.run 以检查传入环境变量。
            _run_git(None, ['git', 'clone', 'https://example.com/repo.git', '/tmp/demo'], timeout=1800)  # 执行一次 Git 命令并传入较长超时。
        env = mocked_run.call_args.kwargs['env']  # 读取传给 subprocess.run 的环境变量字典。
        self.assertEqual(env['GIT_HTTP_LOW_SPEED_LIMIT'], '1')  # 断言 Git HTTP 低速阈值会被显式设置。
        self.assertEqual(env['GIT_HTTP_LOW_SPEED_TIME'], '1800')  # 断言 Git HTTP 低速容忍时间会与命令超时保持一致。

    def test_ensure_revision_available_fetches_missing_commit_on_demand(self):  # 验证 fixed_sha 在本地缺失时会按 revision 做一次补拉。
        missing_revision = subprocess.CompletedProcess(args=['git', 'cat-file'], returncode=128, stdout='', stderr='fatal: Not a valid object name')  # 伪造本地尚未包含目标 revision。
        fetch_success = subprocess.CompletedProcess(args=['git', 'fetch'], returncode=0, stdout='fetched', stderr='')  # 伪造按 revision fetch 成功。
        revision_now_available = subprocess.CompletedProcess(args=['git', 'cat-file'], returncode=0, stdout='', stderr='')  # 伪造 fetch 后 revision 已可读。
        with mock.patch('rerun_tool.repo._run_git', side_effect=[missing_revision, fetch_success, revision_now_available]) as mocked_git:  # 拦截全部 Git 调用。
            ok, message = ensure_revision_available('/tmp/demo', 'a' * 40, timeout=30, max_retries=0)  # 执行按 revision 补拉流程。
        self.assertTrue(ok)  # 断言最终会成功确认 revision 可用。
        self.assertIn('Fetched revision', message)  # 断言返回消息会明确说明这次成功来自按 revision fetch。
        self.assertEqual(mocked_git.call_args_list[1].args[1], ['git', 'fetch', 'origin', 'a' * 40])  # 断言中间确实执行了按 revision 的 fetch。

    def test_list_files_at_revision_uses_git_ls_tree_with_prefix(self):  # 验证列 revision 文件时会把路径前缀传给 git ls-tree。
        ls_tree_success = subprocess.CompletedProcess(  # 伪造一个最小的 ls-tree 成功结果。
            args=['git', 'ls-tree'],
            returncode=0,
            stdout='src/test/java/com/example/ExampleTest.java\nsrc/test/java/com/example/Helper.java\n',
            stderr='',
        )  # 完成伪造结果构造。
        with mock.patch('rerun_tool.repo._run_git', return_value=ls_tree_success) as mocked_git:  # 拦截底层 Git 调用。
            ok, files = list_files_at_revision('/tmp/demo', 'b' * 40, 'src/test/java/com/example', timeout=30)  # 执行 revision 文件列表读取。
        self.assertTrue(ok)  # 断言当前读取流程成功。
        self.assertEqual(files, ['src/test/java/com/example/ExampleTest.java', 'src/test/java/com/example/Helper.java'])  # 断言返回文件列表保持 Git 输出顺序。
        self.assertEqual(mocked_git.call_args.args[1], ['git', 'ls-tree', '-r', '--name-only', 'b' * 40, '--', 'src/test/java/com/example'])  # 断言命令行正确携带 revision 和 pathspec。

    def test_read_file_at_revision_uses_git_show_without_touching_worktree(self):  # 验证读取 revision 文件内容时会直接走 git show。
        git_show_success = subprocess.CompletedProcess(args=['git', 'show'], returncode=0, stdout='class ExampleTest {}\n', stderr='')  # 伪造 git show 成功结果。
        with mock.patch('rerun_tool.repo._run_git', return_value=git_show_success) as mocked_git:  # 拦截底层 Git 调用。
            ok, content = read_file_at_revision('/tmp/demo', 'c' * 40, 'src/test/java/com/example/ExampleTest.java', timeout=30)  # 执行 revision 文件读取。
        self.assertTrue(ok)  # 断言当前读取流程成功。
        self.assertEqual(content, 'class ExampleTest {}\n')  # 断言返回内容来自 git show 标准输出。
        self.assertEqual(mocked_git.call_args.args[1], ['git', 'show', f'{"c" * 40}:src/test/java/com/example/ExampleTest.java'])  # 断言不会通过 checkout 改写工作树。

    def test_remove_workspace_retries_after_docker_permission_repair(self):  # 验证工作区删除命中权限错误时会先做 Docker 权限修复再重试删除。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            target_dir = Path(tmp_dir) / 'demo'  # 构造工作区目录路径。
            target_dir.mkdir()  # 创建最小工作区目录。
            with mock.patch('rerun_tool.repo.shutil.rmtree', side_effect=[PermissionError('denied'), None]) as mocked_rmtree, mock.patch('rerun_tool.repo._repair_workspace_permissions_with_docker', return_value=(True, 'repaired')) as mocked_repair:  # 让第一次删除失败、权限修复成功、第二次删除成功。
                ok, message = _remove_workspace(str(target_dir))  # 执行工作区清理 helper。
        self.assertTrue(ok)  # 断言权限修复后当前工作区可以被删除。
        self.assertIn('after permission repair', message)  # 断言返回消息会明确说明删除成功来自权限修复后的重试。
        self.assertEqual(mocked_rmtree.call_count, 2)  # 断言当前会在权限修复前后各尝试一次删除。
        mocked_repair.assert_called_once_with(str(target_dir))  # 断言确实进入了 Docker 权限修复分支。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
