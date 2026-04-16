import csv  # 导入 CSV 工具用于稳定写入测试结果文件。 
import json  # 导入 JSON 工具用于稳定构造结果列内容。 
import io  # 导入内存文本流工具用于捕获标准输出。 
import tempfile  # 导入临时目录工具用于构造隔离结果文件。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。
from contextlib import redirect_stdout  # 导入标准输出重定向工具。 

from rerun_tool.data import RunRequest, RunnerBackend, TestEntry, TestTarget, WorkflowKind  # 导入数据结构以构造最小测试样本。
from rerun_tool.results import load_results_csv, print_summary, write_results_csv  # 导入结果恢复、写出与摘要输出函数。
from rerun_tool.runner import TestRunResult  # 导入结果对象以构造摘要输出测试。


def _make_entry(index: int) -> TestEntry:  # 根据索引构造最小测试样本。
    return TestEntry(  # 返回只填充必要字段的测试条目。
        index=index,  # 写入指定的原始数据行号。
        repo_url='https://example.com/repo.git',  # 伪造仓库地址。
        repo_owner='example',  # 伪造仓库 owner。
        project_name='demo',  # 伪造项目名。
        original_sha='a' * 40,  # 提供长度正确的伪造提交号。
        fixed_sha='b' * 40,  # 提供长度正确的伪造修复提交号。
        module='.',  # 当前测试只关心单模块仓库。
        full_test_name=f'com.example.ExampleTest.test{index}',  # 提供可解析的测试方法名。
        pr_link='',  # 当前测试不依赖 PR 链接。
        flaky_code='public void test() {}',  # 提供最小 flaky 方法文本。
        fixed_code='',  # 当前测试不依赖 fixed_code。
        diff='',  # 当前测试不依赖 diff。
        generated_patch='public void test() {}',  # 提供最小生成补丁。
        is_correct='1',  # 伪造标签字段。
        source_file='',  # 当前测试不依赖 source_file。
    )  # 完成最小测试条目构造。


def _make_request(index: int) -> RunRequest:  # 根据索引构造一个带请求键的新架构运行请求。
    target = TestTarget(index=index, repo_url='https://example.com/repo.git', repo_owner='example', project_name='demo', original_sha='a' * 40, module='.', full_test_name=f'com.example.ExampleTest.test{index}', input_source='cli')  # 构造最小测试目标。
    return RunRequest(target=target, workflow=WorkflowKind.DETECT_FLAKY, runner_backend=RunnerBackend.NONDEX, patch=None)  # 返回不带补丁的检测请求。


class ResultsBehaviorTests(unittest.TestCase):  # 测试历史结果恢复逻辑。
    def test_load_results_csv_restores_existing_entries_for_resume(self):  # 验证可以从已有结果 CSV 中恢复历史结果对象。
        output_csv = (  # 构造一个最小可恢复的结果 CSV 内容。
            'index,repo_url,project_name,module,test_class,test_method,full_test_name,original_sha,pr_link,is_correct_label,status,rerun_results,pass_count,fail_count,error_count,total_runs,verdict,error_message,run_1\n'  # CSV 表头行。
            '5,https://example.com/repo.git,demo,.,com.example.ExampleTest,test5,com.example.ExampleTest.test5,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,,1,build_failed,"[]",0,0,0,0,BUILD_ERROR,boom,\n'  # 历史失败结果行。
        )  # 完成测试 CSV 内容构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            output_path = Path(tmp_dir) / 'results.csv'  # 构造结果文件路径。
            output_path.write_text(output_csv, encoding='utf-8')  # 写入历史结果 CSV。
            restored = load_results_csv(str(output_path), {5: _make_entry(5)})  # 执行历史结果恢复。
        self.assertEqual(len(restored), 1)  # 断言成功恢复出一条历史结果。
        self.assertEqual(restored[0].entry.index, 5)  # 断言恢复结果对应正确的样本索引。
        self.assertEqual(restored[0].status, 'build_failed')  # 断言历史状态值被正确恢复。
        self.assertEqual(restored[0].results, [])  # 断言历史重跑结果数组被正确恢复。

    def test_load_results_csv_prefers_request_key_when_present(self):  # 验证新格式结果文件会优先使用 request_key 做恢复。 
        request = _make_request(7)  # 构造一个带稳定请求键的新架构运行请求。 
        rerun_results_json = json.dumps(['pass'])  # 用 JSON 工具稳定生成 rerun_results 字段值。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。 
            output_path = Path(tmp_dir) / 'results.csv'  # 构造结果文件路径。 
            with output_path.open('w', newline='', encoding='utf-8') as f:  # 用 CSV 写入器稳定生成结果文件。 
                writer = csv.DictWriter(f, fieldnames=['request_key', 'index', 'workflow', 'runner_backend', 'input_source', 'patch_mode', 'repo_url', 'project_name', 'module', 'test_class', 'test_method', 'full_test_name', 'original_sha', 'pr_link', 'is_correct_label', 'original_rerun_consistency', 'status', 'rerun_results', 'pass_count', 'fail_count', 'error_count', 'total_runs', 'total_elapsed_seconds', 'rerun_elapsed_seconds', 'verdict', 'error_message', 'checkpoint_1_verdict', 'checkpoint_1_total_elapsed_seconds', 'checkpoint_1_rerun_elapsed_seconds'])  # 定义与新结果文件一致的表头字段。 
                writer.writeheader()  # 先写入表头。 
                writer.writerow({'request_key': request.request_key, 'index': 7, 'workflow': 'detect_flaky', 'runner_backend': 'nondex', 'input_source': 'cli', 'patch_mode': 'no_patch', 'repo_url': 'https://example.com/repo.git', 'project_name': 'demo', 'module': '.', 'test_class': 'com.example.ExampleTest', 'test_method': 'test7', 'full_test_name': 'com.example.ExampleTest.test7', 'original_sha': 'a' * 40, 'pr_link': '', 'is_correct_label': '', 'original_rerun_consistency': '', 'status': 'completed', 'rerun_results': rerun_results_json, 'pass_count': 1, 'fail_count': 0, 'error_count': 0, 'total_runs': 1, 'total_elapsed_seconds': '3.500', 'rerun_elapsed_seconds': '1.250', 'verdict': 'STABLE_PASS', 'error_message': '', 'checkpoint_1_verdict': 'STABLE_PASS', 'checkpoint_1_total_elapsed_seconds': '3.500', 'checkpoint_1_rerun_elapsed_seconds': '1.250'})  # 写入一条带 request_key 与耗时字段的历史结果。 
            restored = load_results_csv(str(output_path), {request.request_key: request})  # 按 request_key 执行历史结果恢复。 
        self.assertEqual(len(restored), 1)  # 断言成功恢复出一条历史结果。 
        self.assertEqual(restored[0].entry.request_key, request.request_key)  # 断言恢复条目与 request_key 精确匹配。 
        self.assertEqual(restored[0].results, ['pass'])  # 断言历史重跑结果数组被正确恢复。 
        self.assertEqual(restored[0].total_elapsed_seconds, 3.5)  # 断言总耗时字段被正确恢复。 
        self.assertEqual(restored[0].checkpoint_rerun_elapsed_seconds, {1: 1.25})  # 断言阶段性纯 rerun 耗时被正确恢复。 

    def test_write_results_csv_uses_checkpoint_columns_instead_of_per_run_columns(self):  # 验证结果写出只保留 JSON 数组和 checkpoint 统计列。 
        result = TestRunResult(entry=_make_request(3), status='completed', results=['pass'] * 10 + ['fail'] * 10, total_elapsed_seconds=18.5, rerun_elapsed_seconds=6.5, checkpoint_total_elapsed_seconds={10: 12.0, 20: 18.0}, checkpoint_rerun_elapsed_seconds={10: 3.0, 20: 6.0})  # 构造一个带阶段耗时的结果对象。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。 
            output_path = Path(tmp_dir) / 'results.csv'  # 构造结果文件路径。 
            write_results_csv([result], str(output_path), rerun_count=20)  # 将结果写出到结果 CSV。 
            with output_path.open('r', newline='', encoding='utf-8') as f:  # 重新读取写出的结果文件。 
                reader = csv.DictReader(f)  # 使用字典读取器解析结果文件。 
                header = reader.fieldnames or []  # 读取表头字段列表。 
                row = next(reader)  # 读取唯一的一行结果。 
        self.assertNotIn('run_1', header)  # 断言新格式不再展开单轮结果列。 
        self.assertEqual(row['rerun_results'], json.dumps(['pass'] * 10 + ['fail'] * 10))  # 断言逐轮结果仅保存在 JSON 数组列中。 
        self.assertEqual(row['total_elapsed_seconds'], '18.500')  # 断言总耗时按固定格式写出。 
        self.assertEqual(row['rerun_elapsed_seconds'], '6.500')  # 断言纯 rerun 耗时按固定格式写出。 
        self.assertEqual(row['checkpoint_10_verdict'], 'STABLE_PASS')  # 断言前 10 次阶段 verdict 会被正确汇总。 
        self.assertEqual(row['checkpoint_20_verdict'], 'FLAKY')  # 断言前 20 次阶段 verdict 会被正确汇总。 
        self.assertEqual(row['checkpoint_20_total_elapsed_seconds'], '18.000')  # 断言关键阶段总耗时会被写出。 
        self.assertEqual(row['checkpoint_20_rerun_elapsed_seconds'], '6.000')  # 断言关键阶段纯 rerun 耗时会被写出。 

    def test_write_results_csv_preserves_error_tail_instead_of_front_truncation(self):  # 验证长错误信息写出时优先保留最后的失败点，而不是前部噪声。 
        noisy_prefix = 'bootstrap\n' * 900  # 构造大量前部噪声，模拟框架初始化日志。 
        important_tail = 'FINAL_FAILURE_MARKER\nTests run: 1, Failures: 1, Errors: 0\n'  # 构造必须保留下来的最终失败摘要。 
        result = TestRunResult(entry=_make_request(9), status='completed', results=['error'], error_message=noisy_prefix + important_tail)  # 构造一条长 RUN_ERROR 结果。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离临时目录。 
            output_path = Path(tmp_dir) / 'results.csv'  # 构造结果文件路径。 
            write_results_csv([result], str(output_path), rerun_count=1)  # 将结果写出到 CSV。 
            with output_path.open('r', newline='', encoding='utf-8') as f:  # 重新读取写出的结果文件。 
                row = next(csv.DictReader(f))  # 读取唯一一条结果。 
        self.assertIn('FINAL_FAILURE_MARKER', row['error_message'])  # 断言最终失败标记仍然存在。 
        self.assertTrue(row['error_message'].endswith('Tests run: 1, Failures: 1, Errors: 0'))  # 断言尾部摘要不会再被前部截断吞掉。 

    def test_write_results_csv_includes_original_rerun_consistency(self):  # 验证结果文件会显式带出原始输入里的 rerun_consistency 字段。
        result = TestRunResult(entry=_make_entry(11), status='build_failed', error_message='boom')  # 构造一个最小构建失败结果。
        result.entry.rerun_consistency = 'INCONSISTENT'  # 手动写入原始输入里的 rerun_consistency。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            output_path = Path(tmp_dir) / 'results.csv'  # 构造结果文件路径。
            write_results_csv([result], str(output_path), rerun_count=1)  # 将结果写出到 CSV。
            with output_path.open('r', newline='', encoding='utf-8') as f:  # 重新读取写出的结果文件。
                row = next(csv.DictReader(f))  # 读取唯一一条结果。
        self.assertEqual(row['original_rerun_consistency'], 'INCONSISTENT')  # 断言原始 rerun_consistency 会被写出到结果 CSV。

    def test_print_summary_outputs_verdict_counts(self):  # 验证结果摘要会完整打印数量与 verdict 统计。 
        result = TestRunResult(entry=_make_request(1), status='completed', results=['pass', 'fail'])  # 构造一个会被判定为 FLAKY 的结果对象。 
        buffer = io.StringIO()  # 创建内存文本流用于捕获标准输出。 
        with redirect_stdout(buffer):  # 将摘要输出重定向到内存流。 
            print_summary([result])  # 打印单条结果的摘要信息。 
        output = buffer.getvalue()  # 读取捕获到的摘要输出文本。 
        self.assertIn('Total entries:     1', output)  # 断言摘要包含总条目数量。 
        self.assertIn('Completed:         1', output)  # 断言摘要包含完成数量。 
        self.assertIn('FLAKY', output)  # 断言摘要包含当前 verdict 分类。 


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
