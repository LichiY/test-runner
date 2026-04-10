import tempfile  # 导入临时目录工具用于构造隔离输入文件。 
import unittest  # 导入标准库测试框架。 
from pathlib import Path  # 导入路径工具简化测试文件创建。 

from rerun_tool.data import RunnerBackend, WorkflowKind, build_cli_request, load_flaky_requests, load_patch_requests  # 导入统一数据加载与请求构造函数。 


class DataLoadingTests(unittest.TestCase):  # 测试统一请求模型与输入加载层。 
    def test_load_patch_requests_wraps_patch_csv(self):  # 验证补丁 CSV 会被正确包装为统一运行请求。 
        csv_content = (  # 构造一个最小可用的补丁数据集。 
            'repo_url,repo_owner,project_name,original_sha,fixed_sha,module,full_test_name,pr_link,flaky_code,fixed_code,diff,generated_patch,isCorrect,source_file\n'  # 写入补丁 CSV 表头。 
            'https://github.com/example/demo.git,example,demo,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb,.,com.example.ExampleTest.testCase,,old code,,diff text,"public void testCase() {}",1,\n'  # 写入单条补丁样本。 
        )  # 完成测试 CSV 内容构造。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。 
            csv_path = Path(tmp_dir) / 'patch.csv'  # 构造补丁 CSV 路径。 
            csv_path.write_text(csv_content, encoding='utf-8')  # 写入补丁 CSV 文件。 
            requests = load_patch_requests(str(csv_path), runner_backend=RunnerBackend.NONDEX)  # 按 NonDex 后端加载统一请求。 
        self.assertEqual(len(requests), 1)  # 断言只加载出一条请求。 
        self.assertEqual(requests[0].workflow, WorkflowKind.VERIFY_PATCH)  # 断言工作流被正确标记为补丁验证。 
        self.assertEqual(requests[0].runner_backend, RunnerBackend.NONDEX)  # 断言执行后端被正确写入请求。 
        self.assertEqual(requests[0].generated_patch, 'public void testCase() {}')  # 断言生成补丁文本被完整保留。 

    def test_load_flaky_requests_accepts_rows_without_generated_patch(self):  # 验证 patchless flaky CSV 不要求 generated_patch 字段。 
        csv_content = (  # 构造一个最小可用的 patchless 检测数据集。 
            'repo_url,original_sha,module,full_test_name\n'  # 写入 patchless CSV 表头。 
            'https://github.com/example/demo.git,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,.,com.example.ExampleTest.testCase\n'  # 写入单条 patchless 样本。 
        )  # 完成 patchless CSV 内容构造。 
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。 
            csv_path = Path(tmp_dir) / 'flaky.csv'  # 构造 patchless CSV 路径。 
            csv_path.write_text(csv_content, encoding='utf-8')  # 写入 patchless CSV 文件。 
            requests = load_flaky_requests(str(csv_path), runner_backend=RunnerBackend.STANDARD)  # 加载 patchless 检测请求列表。 
        self.assertEqual(len(requests), 1)  # 断言只加载出一条请求。 
        self.assertEqual(requests[0].workflow, WorkflowKind.DETECT_FLAKY)  # 断言工作流被正确标记为 flaky 检测。 
        self.assertEqual(requests[0].patch_mode, 'no_patch')  # 断言 patchless 请求不会携带补丁模式。 
        self.assertEqual(requests[0].project_name, 'demo')  # 断言项目名可以从 repo_url 自动推断。 

    def test_build_cli_request_infers_repo_identity(self):  # 验证单条 CLI 输入会自动推断 owner 与项目名。 
        request = build_cli_request(repo_url='https://github.com/example/demo.git', original_sha='a' * 40, full_test_name='com.example.ExampleTest.testCase', module='.')  # 构造单条 CLI 检测请求。 
        self.assertEqual(request.workflow, WorkflowKind.DETECT_FLAKY)  # 断言单条 CLI 请求默认属于 flaky 检测工作流。 
        self.assertEqual(request.repo_owner, 'example')  # 断言仓库 owner 可以从 repo_url 中推断。 
        self.assertEqual(request.project_name, 'demo')  # 断言项目名可以从 repo_url 中推断。 
        self.assertEqual(request.input_source, 'cli')  # 断言输入来源被标记为 CLI。 


if __name__ == '__main__':  # 允许单文件直接运行测试。 
    unittest.main()  # 执行当前文件中的全部测试用例。 
