import tempfile  # 导入临时目录工具用于构造隔离测试仓库。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。
from unittest import mock  # 导入 mock 以便隔离外部依赖。

from rerun_tool.data import TestEntry  # 导入数据结构构造最小测试样本。
from rerun_tool.runner import (ExecutionEnvironment, _maven_stability_flags, _parse_test_result,  # 导入待测的结果判定和 Maven 降噪参数函数。
                               build_project)  # 导入构建入口以验证项目级恢复逻辑。


def _make_entry(project_name: str = 'demo', module: str = '.') -> TestEntry:  # 创建用于测试的最小数据样本。
    return TestEntry(  # 返回带默认字段的测试条目。
        index=0,  # 伪造 CSV 行号。
        repo_url='https://example.com/repo.git',  # 伪造仓库地址。
        repo_owner='example',  # 伪造仓库 owner。
        project_name=project_name,  # 允许按测试场景覆盖项目名。
        original_sha='a' * 40,  # 提供长度正确的伪造提交号。
        fixed_sha='b' * 40,  # 提供长度正确的伪造修复提交号。
        module=module,  # 允许测试时覆盖模块字段。
        full_test_name='com.example.SampleTest.testCase',  # 提供最小可解析的测试名。
        pr_link='',  # 当前测试不依赖 PR 链接。
        flaky_code='public void testCase() { assertEquals(1, 1); }',  # 提供最小 flaky 方法文本。
        fixed_code='',  # 当前测试不依赖 fixed_code。
        diff='',  # 当前测试不依赖 diff。
        generated_patch='public void testCase() { assertEquals(1, 1); }',  # 提供最小生成补丁。
        is_correct='1',  # 伪造标签字段。
        source_file='',  # 当前测试不依赖 source_file。
    )  # 结束最小测试条目构造。


class RunnerBehaviorTests(unittest.TestCase):  # 覆盖结果判定与 Maven 构建降噪的关键行为。
    def test_parse_test_result_treats_successful_target_test_as_pass_even_with_reactor_failure(self):  # 验证多模块 reactor 噪声不会把已通过的目标测试误判为 RUN_ERROR。
        output = (
            '[INFO] Running org.example.MyTest\n'  # 模拟目标测试实际执行。
            '[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n'  # 模拟目标测试已经通过。
            '[INFO] Reactor Summary for Demo:\n'  # 模拟多模块 reactor 汇总。
            '[INFO] demo-module .................................... FAILURE\n'  # 模拟后续 reactor 汇总中的失败噪声。
            '[INFO] BUILD FAILURE\n'  # 模拟 Maven 最终返回非零退出码。
        )  # 完成“目标测试已过但 reactor 汇总失败”的日志构造。
        self.assertEqual(_parse_test_result(1, output), 'pass')  # 断言当前场景仍然应被认定为 pass。

    def test_parse_test_result_treats_surefire_failure_summary_as_fail(self):  # 验证 surefire 汇总中的失败会被明确归类为 fail。
        output = (
            '[INFO] Results:\n'  # 模拟 surefire 结果区块开始。
            '[ERROR] Failures:\n'  # 模拟失败列表头。
            '[ERROR] DemoTest.testSomething:42 expected:<1> but was:<2>\n'  # 模拟真实断言失败明细。
            '[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n'  # 模拟带失败数量的 surefire 汇总。
            '[ERROR] There are test failures.\n'  # 模拟 Maven sure-fire 统一失败提示。
        )  # 完成“测试真实失败”的日志构造。
        self.assertEqual(_parse_test_result(1, output), 'fail')  # 断言当前场景应被归类为 fail 而不是 error。

    def test_parse_test_result_treats_compilation_failure_as_error(self):  # 验证编译失败仍然会被正确归类为 RUN_ERROR。
        output = (
            '[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.8.1:testCompile\n'  # 模拟 compiler plugin 失败头。
            '[ERROR] Compilation failure\n'  # 模拟 Maven 的统一编译失败提示。
            '[ERROR] /workspace/src/test/java/org/example/DemoTest.java:[5,9] cannot find symbol\n'  # 模拟缺失符号错误。
        )  # 完成“测试尚未真正开始执行”的编译失败日志构造。
        self.assertEqual(_parse_test_result(1, output), 'error')  # 断言编译失败应保持为 error。

    def test_maven_stability_flags_skip_format_related_plugins(self):  # 验证 rerun 构建会统一跳过常见格式检查插件。
        flags = _maven_stability_flags()  # 读取当前默认的 Maven 稳定性参数集合。
        self.assertIn('-Dbasepom.check.skip-prettier=true', flags)  # 断言 HubSpot basepom 的 prettier 检查会被跳过。
        self.assertIn('-Dfmt.skip=true', flags)  # 断言 fmt-maven-plugin 的检查会被跳过。
        self.assertIn('-Dimpsort.skip=true', flags)  # 断言 impsort-maven-plugin 的检查会被跳过。
        self.assertIn('-Dformatter.skip=true', flags)  # 断言 formatter-maven-plugin 的检查会被跳过。

    def test_build_project_adds_os_classifier_override_for_os_maven_projects(self):  # 验证依赖 `${os.detected.classifier}` 的仓库会自动追加稳定的 Linux classifier。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于创建 pom。
            (repo_dir / 'pom.xml').write_text(  # 写入一个最小的 os-maven-plugin 场景 pom。
                '<project>'
                '<build><extensions><extension><groupId>kr.motd.maven</groupId><artifactId>os-maven-plugin</artifactId></extension></extensions></build>'
                '<dependencies><dependency><groupId>io.netty</groupId><artifactId>netty-tcnative</artifactId><classifier>${os.detected.classifier}</classifier></dependency></dependencies>'
                '</project>',
                encoding='utf-8',
            )  # 完成 pom 写入。
            entry = _make_entry(project_name='timely', module='server')  # 构造一个最小 Maven 测试条目。
            execution_env = ExecutionEnvironment(build_tool='maven', use_docker=False)  # 固定当前测试走本地 Maven 分支。
            with mock.patch('rerun_tool.runner.platform.machine', return_value='x86_64'), mock.patch('rerun_tool.runner._run_local', return_value=(True, 'ok')) as mocked_run:  # 固定当前平台架构并拦截本地命令执行。
                success, output = build_project(str(repo_dir), entry, use_docker=False, timeout=30, max_retries=0, execution_env=execution_env)  # 执行构建入口以生成实际 Maven 命令。
        self.assertTrue(success)  # 断言伪造构建结果被正确透传。
        self.assertEqual(output, 'ok')  # 断言伪造输出被正确透传。
        local_cmd = mocked_run.call_args.args[0]  # 读取传入本地执行器的命令列表。
        self.assertIn('-Dos.detected.classifier=linux-x86_64', local_cmd)  # 断言命令中追加了稳定的 Linux classifier 覆盖。

    def test_build_project_can_install_seatunnel_target_reactor_before_retry(self):  # 验证 seatunnel 这类需要先 install 目标 reactor 的项目会触发恢复分支。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            shade_dir = repo_dir / 'seatunnel-config' / 'seatunnel-config-shade'  # 构造 seatunnel shaded 模块目录。
            shade_dir.mkdir(parents=True)  # 创建 seatunnel shaded 模块目录树。
            (shade_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 shaded 模块 pom。
            entry = _make_entry(project_name='seatunnel', module='seatunnel-api')  # 构造一个最小 seatunnel 测试条目。
            execution_env = ExecutionEnvironment(build_tool='maven', use_docker=False)  # 固定当前测试走本地 Maven 分支。
            initial_failure = 'seatunnel-config-shade ConfigParser.java AbstractConfigValue'  # 伪造 seatunnel shaded 模块未 install 时的缺失符号日志。
            with mock.patch('rerun_tool.runner._build_maven', side_effect=[(False, initial_failure), (True, 'rebuilt ok')]) as mocked_build, mock.patch('rerun_tool.runner._run_maven_auxiliary_goal', return_value=(True, 'preinstalled')) as mocked_aux:  # 让第一次构建失败、预安装成功、第二次构建成功。
                success, output = build_project(str(repo_dir), entry, use_docker=False, timeout=30, max_retries=0, execution_env=execution_env)  # 执行构建入口。
        self.assertTrue(success)  # 断言 seatunnel 恢复分支最终可以返回成功。
        self.assertEqual(output, 'rebuilt ok')  # 断言最终输出来自恢复后的第二次构建。
        self.assertEqual(mocked_build.call_count, 2)  # 断言恢复分支会在 reactor install 后重新执行目标构建。
        mocked_aux.assert_called_once()  # 断言 seatunnel 恢复分支确实执行了额外的 Maven install。
        cmd_parts = mocked_aux.call_args.kwargs['cmd_parts']  # 读取传给辅助 Maven 命令的参数列表。
        self.assertIn('install', cmd_parts)  # 断言恢复命令会把目标 reactor install 到隔离仓库。
        self.assertEqual(cmd_parts[-3:], ['-pl', 'seatunnel-api', '-am'])  # 断言恢复范围是目标模块及其上游依赖，而不是只装单个 shade 模块。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试。
