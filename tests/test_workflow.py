import tempfile  # 导入临时目录工具用于构造隔离工作区。 
import unittest  # 导入标准库测试框架。 
from pathlib import Path  # 导入路径工具简化测试路径创建。 
from unittest import mock  # 导入 mock 以便隔离外部依赖。 

from rerun_tool.data import PatchSpec, RunRequest, RunnerBackend, TestTarget, WorkflowKind  # 导入统一请求模型与枚举。 
from rerun_tool.runner import RerunExecutionSummary, RerunMode  # 导入执行摘要与 JVM 复用模式枚举。 
from rerun_tool.workflow import ExecutionConfig, process_request  # 导入统一工作流编排入口。 


def _make_target() -> TestTarget:  # 构造最小可用的测试目标对象。 
    return TestTarget(index=0, repo_url='https://example.com/repo.git', repo_owner='example', project_name='demo', original_sha='a' * 40, module='.', full_test_name='com.example.ExampleTest.testCase', input_source='cli')  # 返回单模块最小测试目标。 


def _make_config() -> ExecutionConfig:  # 构造最小可用的执行配置对象。 
    return ExecutionConfig(rerun_count=2, mode=RerunMode.ISOLATED, docker_mode='never', build_timeout=30, test_timeout=30, build_retries=1)  # 返回一个适合单元测试的小配置。 


class WorkflowBehaviorTests(unittest.TestCase):  # 测试工作流编排层的关键行为。 
    def test_process_request_patchless_does_not_apply_patch(self):  # 验证 patchless 流程不会触发任何补丁应用。 
        request = RunRequest(target=_make_target(), workflow=WorkflowKind.DETECT_FLAKY, runner_backend=RunnerBackend.STANDARD, patch=None)  # 构造 patchless 检测请求。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的工作区目录。 
            execution_summary = RerunExecutionSummary(results=['pass', 'pass'], rerun_elapsed_seconds=0.2, checkpoint_rerun_elapsed_seconds={2: 0.2})  # 构造最小执行摘要。 
            with mock.patch('rerun_tool.workflow.clone_repo', return_value=True), mock.patch('rerun_tool.workflow.find_test_file', return_value=str(Path(tmp_dir) / 'ExampleTest.java')), mock.patch('rerun_tool.workflow.apply_patch', return_value=(True, 'OK')) as mocked_apply_patch, mock.patch('rerun_tool.workflow.build_project', return_value=(True, 'compiled')), mock.patch('rerun_tool.workflow.run_test_with_summary', return_value=execution_summary) as mocked_run_test, mock.patch('rerun_tool.workflow.reset_repo', return_value=True):  # 拦截所有外部副作用调用。 
                result = process_request(request=request, workspace_dir=tmp_dir, config=_make_config())  # 执行 patchless 工作流。 
        self.assertEqual(result.status, 'completed')  # 断言 patchless 工作流可以成功完成。 
        self.assertEqual(result.results, ['pass', 'pass'])  # 断言测试结果被正确透传。 
        mocked_apply_patch.assert_not_called()  # 断言 patchless 流程完全不会触发补丁应用。 
        self.assertEqual(mocked_run_test.call_args.kwargs['runner_backend'], RunnerBackend.STANDARD)  # 断言标准执行后端被正确下发到执行层。 

    def test_process_request_verify_patch_can_retry_after_import_fix(self):  # 验证补丁验证流程会在首次构建失败后尝试 import 修复。 
        request = RunRequest(target=_make_target(), workflow=WorkflowKind.VERIFY_PATCH, runner_backend=RunnerBackend.STANDARD, patch=PatchSpec(generated_patch='public void testCase() {}'))  # 构造最小补丁验证请求。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的工作区目录。 
            execution_summary = RerunExecutionSummary(results=['pass', 'pass'], rerun_elapsed_seconds=0.2, checkpoint_rerun_elapsed_seconds={2: 0.2})  # 构造最小执行摘要。 
            with mock.patch('rerun_tool.workflow.clone_repo', return_value=True), mock.patch('rerun_tool.workflow.find_test_file', return_value=str(Path(tmp_dir) / 'ExampleTest.java')), mock.patch('rerun_tool.workflow.apply_patch', return_value=(True, 'OK')), mock.patch('rerun_tool.workflow.build_project', side_effect=[(False, 'cannot find symbol'), (True, 'compiled')]) as mocked_build, mock.patch('rerun_tool.workflow.fix_missing_imports', return_value=(True, 'added imports')) as mocked_fix_imports, mock.patch('rerun_tool.workflow.run_test_with_summary', return_value=execution_summary), mock.patch('rerun_tool.workflow.reset_repo', return_value=True):  # 拦截所有外部副作用调用。 
                result = process_request(request=request, workspace_dir=tmp_dir, config=_make_config())  # 执行补丁验证工作流。 
        self.assertEqual(result.status, 'completed')  # 断言补丁验证流程在修复后可以成功完成。 
        self.assertEqual(mocked_build.call_count, 2)  # 断言 import 修复后确实触发了第二次构建。 
        mocked_fix_imports.assert_called_once_with(str(Path(tmp_dir) / 'ExampleTest.java'), 'cannot find symbol')  # 断言 import 修复收到的是目标测试文件与首次构建输出。 

    def test_process_request_rejects_nondex_for_gradle_projects(self):  # 验证 Gradle 项目显式选择 NonDex 时会尽早拒绝执行。 
        request = RunRequest(target=_make_target(), workflow=WorkflowKind.DETECT_FLAKY, runner_backend=RunnerBackend.NONDEX, patch=None)  # 构造一个使用 NonDex 的 patchless 请求。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的工作区目录。 
            with mock.patch('rerun_tool.workflow.clone_repo', return_value=True), mock.patch('rerun_tool.workflow.detect_build_tool', return_value='gradle'), mock.patch('rerun_tool.workflow.reset_repo', return_value=True):  # 拦截克隆与仓库探测逻辑。 
                result = process_request(request=request, workspace_dir=tmp_dir, config=_make_config())  # 执行当前工作流。 
        self.assertEqual(result.status, 'unsupported_runner')  # 断言当前结果会被明确标记为后端不支持。 
        self.assertIn('NonDex', result.error_message)  # 断言错误信息会明确指出 NonDex 能力边界。 

    def test_process_request_records_total_and_checkpoint_timings(self):  # 验证工作流会把总耗时与阶段耗时写入结果对象。 
        request = RunRequest(target=_make_target(), workflow=WorkflowKind.DETECT_FLAKY, runner_backend=RunnerBackend.STANDARD, patch=None)  # 构造一个最小 patchless 检测请求。 
        execution_summary = RerunExecutionSummary(results=['pass'] * 10, rerun_elapsed_seconds=5.0, checkpoint_rerun_elapsed_seconds={10: 5.0})  # 构造一个带阶段耗时的执行摘要。 
        perf_counter_values = [100.0, 112.0, 118.5]  # 分别模拟请求开始、rerun 开始与请求结束的时间点。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的工作区目录。 
            with mock.patch('rerun_tool.workflow.clone_repo', return_value=True), mock.patch('rerun_tool.workflow.find_test_file', return_value=str(Path(tmp_dir) / 'ExampleTest.java')), mock.patch('rerun_tool.workflow.build_project', return_value=(True, 'compiled')), mock.patch('rerun_tool.workflow.run_test_with_summary', return_value=execution_summary), mock.patch('rerun_tool.workflow.reset_repo', return_value=True), mock.patch('rerun_tool.workflow.time.perf_counter', side_effect=perf_counter_values):  # 拦截外部副作用并固定时间轴。 
                result = process_request(request=request, workspace_dir=tmp_dir, config=ExecutionConfig(rerun_count=10, mode=RerunMode.ISOLATED, docker_mode='never', build_timeout=30, test_timeout=30, build_retries=1))  # 执行带阶段计时的工作流。 
        self.assertEqual(result.total_elapsed_seconds, 18.5)  # 断言总耗时等于请求结束减请求开始。 
        self.assertEqual(result.rerun_elapsed_seconds, 5.0)  # 断言纯 rerun 耗时被正确透传。 
        self.assertEqual(result.checkpoint_total_elapsed_seconds, {10: 17.0})  # 断言关键阶段总耗时会加上 rerun 前置阶段时间。 
        self.assertEqual(result.checkpoint_rerun_elapsed_seconds, {10: 5.0})  # 断言关键阶段纯 rerun 耗时被正确透传。 


if __name__ == '__main__':  # 允许单文件直接运行测试。 
    unittest.main()  # 执行当前文件中的全部测试用例。 
