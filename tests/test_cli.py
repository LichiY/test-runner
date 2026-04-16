import argparse  # 导入参数命名空间工具以便直接调用 CLI 内部函数。 
import tempfile  # 导入临时目录工具用于构造隔离工作区。 
import unittest  # 导入标准库测试框架。 
from unittest import mock  # 导入 mock 以便拦截 CLI 执行。 

from rerun_tool import cli  # 导入待测的 CLI 模块。 
from rerun_tool.data import RunRequest, RunnerBackend, TestTarget, WorkflowKind  # 导入最小请求对象所需的数据结构。 
from rerun_tool.runner import RerunMode, TestRunResult  # 导入运行模式与结果对象构造测试样本。 


def _make_request(index: int) -> RunRequest:  # 构造最小可用的新架构运行请求。 
    target = TestTarget(index=index, repo_url='https://example.com/repo.git', repo_owner='example', project_name='demo', original_sha='a' * 40, module='.', full_test_name=f'com.example.ExampleTest.test{index}', input_source='cli')  # 构造最小测试目标对象。 
    return RunRequest(target=target, workflow=WorkflowKind.DETECT_FLAKY, runner_backend=RunnerBackend.STANDARD, patch=None)  # 返回标准后端的 patchless 检测请求。 


class CliBehaviorTests(unittest.TestCase):  # 测试新旧 CLI 入口的参数分流逻辑。 
    def test_main_legacy_args_map_to_verify_patch_command(self):  # 验证旧命令格式会自动映射到 verify-patch 工作流。 
        with mock.patch('rerun_tool.cli._execute_args') as mocked_execute:  # 拦截真实执行逻辑只观察解析结果。 
            cli.main(['--csv', 'patch-data/demo.csv'])  # 使用旧命令格式调用统一入口。 
        parsed_args = mocked_execute.call_args.kwargs['args']  # 读取 CLI 解析后的参数对象。 
        self.assertEqual(parsed_args.command, 'verify-patch')  # 断言旧命令被映射到 verify-patch 子命令。 
        self.assertEqual(parsed_args.csv, 'patch-data/demo.csv')  # 断言 CSV 参数被完整保留。 
        self.assertTrue(mocked_execute.call_args.kwargs['legacy_mode'])  # 断言当前执行路径被标记为兼容模式。 

    def test_main_detect_flaky_subcommand_uses_new_parser(self):  # 验证 detect-flaky 子命令会走新解析器路径。 
        with mock.patch('rerun_tool.cli._execute_args') as mocked_execute:  # 拦截真实执行逻辑只观察解析结果。 
            cli.main(['detect-flaky', '--repo-url', 'https://github.com/example/demo.git', '--sha', 'a' * 40, '--full-test-name', 'com.example.ExampleTest.testCase'])  # 使用单条 CLI patchless 输入调用统一入口。 
        parsed_args = mocked_execute.call_args.kwargs['args']  # 读取 CLI 解析后的参数对象。 
        self.assertEqual(parsed_args.command, 'detect-flaky')  # 断言命令被正确识别为 detect-flaky。 
        self.assertEqual(parsed_args.repo_url, 'https://github.com/example/demo.git')  # 断言单条 CLI 输入参数被完整保留。 
        self.assertFalse(mocked_execute.call_args.kwargs['legacy_mode'])  # 断言当前执行路径不再属于兼容模式。 

    def test_partition_restored_results_reruns_clone_and_build_failures(self):  # 验证 resume 时只会重新执行 clone/build 失败的历史结果。 
        completed_result = TestRunResult(entry=_make_request(1), status='completed', results=['pass'])  # 构造一条应该继续保留的完成结果。 
        clone_failed_result = TestRunResult(entry=_make_request(2), status='clone_failed', error_message='clone failed')  # 构造一条需要重新执行的 clone 失败结果。 
        build_failed_result = TestRunResult(entry=_make_request(3), status='build_failed', error_message='build failed')  # 构造一条需要重新执行的 build 失败结果。 
        skipped_results, retry_results = cli._partition_restored_results([completed_result, clone_failed_result, build_failed_result])  # 执行历史结果拆分逻辑。 
        self.assertEqual([result.entry.index for result in skipped_results], [1])  # 断言只有真正完成的历史结果会被继续保留。 
        self.assertEqual([result.entry.index for result in retry_results], [2, 3])  # 断言 clone/build 失败结果都会被重新排入执行队列。 

    def test_run_requests_resume_keeps_completed_but_reruns_build_failures(self):  # 验证 resume 时旧的 build 失败不会被永久跳过，而是会用新的执行结果替换。 
        request_one = _make_request(1)  # 构造第一条请求。 
        request_two = _make_request(2)  # 构造第二条请求。 
        restored_completed = TestRunResult(entry=request_one, status='completed', results=['pass'])  # 构造一条应该被保留的历史完成结果。 
        restored_build_failed = TestRunResult(entry=request_two, status='build_failed', error_message='old build failed')  # 构造一条应该被重跑的历史构建失败结果。 
        rerun_completed = TestRunResult(entry=request_two, status='completed', results=['pass'])  # 构造重跑后新的成功结果。 
        args = argparse.Namespace(workspace='workspace', rerun=1, docker_mode='never', build_timeout=30, test_timeout=30, build_retries=1, git_timeout=30, git_retries=1, resume=True, output='results.csv')  # 构造调用 `_run_requests` 所需的最小参数对象。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。 
            args.workspace = tmp_dir  # 将工作区改到当前临时目录中。 
            args.output = f'{tmp_dir}/results.csv'  # 将输出文件改到当前临时目录中。 
            with mock.patch('rerun_tool.cli.load_results_csv', return_value=[restored_completed, restored_build_failed]), mock.patch('rerun_tool.cli.process_request', return_value=rerun_completed) as mocked_process, mock.patch('rerun_tool.cli.write_results_csv') as mocked_write, mock.patch('rerun_tool.cli.print_summary'), mock.patch('rerun_tool.cli.time.time', side_effect=[100.0, 101.0]):  # 拦截外部副作用并固定总耗时。 
                cli._run_requests([request_one, request_two], args=args, mode=RerunMode.ISOLATED)  # 执行带 resume 的统一请求主循环。 
        mocked_process.assert_called_once()  # 断言当前只会对需要重跑的失败样本再执行一次。 
        self.assertEqual(mocked_process.call_args.kwargs['request'].index, 2)  # 断言被重新执行的是原先 build_failed 的那条请求。 
        final_results = mocked_write.call_args_list[-1].args[0]  # 读取最终一次结果落盘时的结果对象列表。 
        self.assertEqual(len(final_results), 2)  # 断言最终结果列表只包含“保留的旧完成结果 + 新重跑结果”。 
        self.assertEqual([result.status for result in final_results], ['completed', 'completed'])  # 断言旧的 build_failed 行已经被新的重跑结果替换掉。 

    def test_format_overall_progress_includes_skip_and_retry_counts(self):  # 验证整体进度摘要会包含跳过数、重跑失败数和剩余数。 
        kept_result = TestRunResult(entry=_make_request(1), status='completed', results=['pass'])  # 构造一条保留的历史完成结果。 
        rerun_result = TestRunResult(entry=_make_request(2), status='completed', results=['pass'])  # 构造一条本轮新完成的结果。 
        progress_text = cli._format_overall_progress(total_requests=4, all_results=[kept_result, rerun_result], skipped_results=[kept_result], retry_results=[TestRunResult(entry=_make_request(3), status='clone_failed')], active_requests=[_make_request(2), _make_request(3), _make_request(4)])  # 生成一条整体进度摘要文本。 
        self.assertIn('Overall progress: 2/4 (50.0%)', progress_text)  # 断言文本中包含整体百分比进度。 
        self.assertIn('skipped_kept=1', progress_text)  # 断言文本中包含保留历史结果数量。 
        self.assertIn('rerun_failed=1', progress_text)  # 断言文本中包含需要重跑的失败样本数量。 
        self.assertIn('remaining=2', progress_text)  # 断言文本中包含剩余待处理数量。 


if __name__ == '__main__':  # 允许单文件直接运行测试。 
    unittest.main()  # 执行当前文件中的全部测试用例。 
