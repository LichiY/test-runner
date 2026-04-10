import unittest  # 导入标准库测试框架。 
from unittest import mock  # 导入 mock 以便拦截 CLI 执行。 

from rerun_tool import cli  # 导入待测的 CLI 模块。 


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


if __name__ == '__main__':  # 允许单文件直接运行测试。 
    unittest.main()  # 执行当前文件中的全部测试用例。 
