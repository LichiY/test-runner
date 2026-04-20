import tempfile  # 导入临时目录工具用于构造隔离测试仓库。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。
from unittest import mock  # 导入 mock 以便隔离外部依赖。

from rerun_tool.data import RunnerBackend, TestEntry  # 导入数据结构构造最小测试样本。
from rerun_tool.runner import (ExecutionEnvironment, _maven_stability_flags, _parse_test_result,  # 导入待测的结果判定和 Maven 降噪参数函数。
                               _nondex_batch_timeout, _run_maven_nondex_batch_with_summary, build_project,
                               run_test_with_summary)  # 导入构建入口与 NonDex 批量执行入口以验证关键行为。


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
        self.assertIn('-DskipCheckstyle=true', flags)  # 断言常见的 Checkstyle 别名开关也会被统一补上。
        self.assertIn('-Dspring-javaformat.skip=true', flags)  # 断言 Spring Java Format 一类前置格式检查也会被跳过。

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

    def test_run_maven_nondex_batch_with_summary_recovers_internal_run_results(self):  # 验证 NonDex 批量实验会从 manifest 和 surefire XML 恢复完整结果序列。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            entry = _make_entry(project_name='fastjson')  # 构造一个最小 Maven 测试条目。

            def _write_report(run_dir: Path, failures: int) -> None:  # 写入最小 surefire XML 报告。
                run_dir.mkdir(parents=True, exist_ok=True)  # 确保当前 run 目录存在。
                (run_dir / f'TEST-{entry.test_class}.xml').write_text(  # 写入最小 surefire XML。
                    f'<?xml version="1.0" encoding="UTF-8"?><testsuite name="{entry.test_class}" tests="1" failures="{failures}" errors="0" skipped="0" time="0.010"></testsuite>',
                    encoding='utf-8',
                )  # 完成报告写入。

            def fake_subprocess_run(*args, **kwargs):  # 在命令真正执行前伪造 NonDex 输出目录与结果文件。
                nondex_dir = repo_dir / '.nondex'  # 定位当前仓库的 `.nondex` 目录。
                nondex_dir.mkdir(parents=True, exist_ok=True)  # 创建 `.nondex` 目录。
                (nondex_dir / 'seed1.run').write_text('seed1\nseed2\nclean_seed1\n', encoding='utf-8')  # 写入本次批次 manifest，刻意把 clean 放在最后以验证重排逻辑。
                _write_report(nondex_dir / 'seed1', failures=1)  # 第一轮扰动失败。
                _write_report(nondex_dir / 'seed2', failures=0)  # 第二轮扰动通过。
                _write_report(nondex_dir / 'clean_seed1', failures=0)  # clean 基线通过。
                return mock.Mock(returncode=0, stdout='[INFO] [NonDex] The id of this run is: seed1\n', stderr='')  # 返回带首个 run id 的最小 NonDex 输出。

            with mock.patch('rerun_tool.runner.subprocess.run', side_effect=fake_subprocess_run), mock.patch('rerun_tool.runner.time.perf_counter', side_effect=[10.0, 13.0]):  # 固定命令执行与壁钟时间。
                summary = _run_maven_nondex_batch_with_summary(str(repo_dir), entry, total_runs=3, timeout=30, use_docker=False, docker_image=None)  # 调用待测的批量 NonDex 执行入口。
        self.assertEqual(summary.results, ['pass', 'fail', 'pass'])  # 断言 clean 基线被放到最前，其余扰动结果顺序保持不变。
        self.assertAlmostEqual(summary.rerun_elapsed_seconds, 3.0)  # 断言整批实验壁钟耗时被正确记录。
        self.assertEqual(summary.checkpoint_rerun_elapsed_seconds, {3: 3.0})  # 断言小样本场景只记录最终阶段耗时。

    def test_run_maven_nondex_batch_with_summary_falls_back_to_output_when_manifest_reports_are_missing(self):  # 验证 `.nondex` 目录缺失时也能从命令输出恢复批量结果，避免整批误判成 RUN_ERROR。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            entry = _make_entry(project_name='jetcache')  # 构造一个最小 Maven 测试条目。
            output = (
                '[INFO] Running com.example.SampleTest\n'
                '[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n'
                'CONFIG: nondexFilter=.*\n'
                'nondexExecid=seed1\n'
                'Running com.example.SampleTest\n'
                'Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n'
                'CONFIG: nondexFilter=.*\n'
                'nondexExecid=seed2\n'
                'Running com.example.SampleTest\n'
                'Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n'
            )  # 构造 manifest 缺失但标准输出仍保留每轮测试摘要的 NonDex 批量日志。
            with mock.patch('rerun_tool.runner.subprocess.run', return_value=mock.Mock(returncode=0, stdout=output, stderr='')), mock.patch('rerun_tool.runner.time.perf_counter', side_effect=[10.0, 14.0]):  # 伪造 NonDex 执行和壁钟时间。
                summary = _run_maven_nondex_batch_with_summary(str(repo_dir), entry, total_runs=3, timeout=30, use_docker=False, docker_image=None)  # 调用待测的批量 NonDex 执行入口。
        self.assertEqual(summary.results, ['pass', 'pass', 'fail'])  # 断言当前会从输出中恢复出 clean baseline 和两轮扰动结果。
        self.assertAlmostEqual(summary.rerun_elapsed_seconds, 4.0)  # 断言输出恢复场景仍会正确记录整批壁钟耗时。
        self.assertEqual(summary.error_outputs, [])  # 断言没有基础设施 error 时不会错误写入 RUN_ERROR 诊断。

    def test_run_maven_nondex_batch_with_summary_scales_timeout_and_keeps_project_flags(self):  # 验证批量 NonDex 会按 rerun 次数放宽超时，并保留项目特定系统属性。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            (repo_dir / 'pom.xml').write_text(  # 写入一个显式依赖 os classifier 的最小 pom。
                '<project>'
                '<build><extensions><extension><groupId>kr.motd.maven</groupId><artifactId>os-maven-plugin</artifactId></extension></extensions></build>'
                '<dependencies><dependency><groupId>io.netty</groupId><artifactId>netty-tcnative</artifactId><classifier>${os.detected.classifier}</classifier></dependency></dependencies>'
                '</project>',
                encoding='utf-8',
            )  # 完成最小 pom 写入。
            entry = _make_entry(project_name='timely', module='server')  # 构造一个最小 Maven 测试条目。
            with mock.patch('rerun_tool.runner.platform.machine', return_value='x86_64'), mock.patch('rerun_tool.runner.subprocess.run', return_value=mock.Mock(returncode=0, stdout='[INFO] [NonDex] The id of this run is: seed1\n', stderr='')) as mocked_run, mock.patch('rerun_tool.runner.time.perf_counter', side_effect=[10.0, 12.0]):  # 固定当前平台架构、拦截 Maven 命令并冻结壁钟时间。
                summary = _run_maven_nondex_batch_with_summary(str(repo_dir), entry, total_runs=50, timeout=300, use_docker=False, docker_image=None)  # 调用批量 NonDex 执行入口。
        self.assertEqual(summary.results, ['pass'] * 50)  # manifest 和输出都无法恢复时会退回到单条输出解析，此时应被扩展成 50 个 pass。
        self.assertEqual(mocked_run.call_args.kwargs['timeout'], 1800)  # 断言 50 轮批量扰动会把整体超时预算提升到 6 倍。
        self.assertIn('-Dos.detected.classifier=linux-x86_64', mocked_run.call_args.args[0])  # 断言批量 NonDex 命令同样会继承项目特定系统属性。

    def test_nondex_batch_timeout_caps_multiplier(self):  # 验证批量 NonDex 超时预算会按 10 轮一档放大，并在极端大批次时封顶。
        self.assertEqual(_nondex_batch_timeout(300, 1), 300)  # 单轮 clean baseline 不需要额外放大超时。
        self.assertEqual(_nondex_batch_timeout(300, 20), 600)  # 20 轮批量实验放大到 2 倍。
        self.assertEqual(_nondex_batch_timeout(300, 50), 1800)  # 50 轮批量实验放大到 6 倍上限。

    def test_run_test_with_summary_uses_single_nondex_batch_for_maven(self):  # 验证主入口在 NonDex 模式下会走一次批量实验而不是外层循环单跑。
        entry = _make_entry(project_name='fastjson')  # 构造一个最小 Maven 测试条目。
        execution_env = ExecutionEnvironment(build_tool='maven', use_docker=False)  # 固定当前测试走本地 Maven 分支。
        expected_summary = mock.Mock(results=['pass', 'fail', 'pass'], rerun_elapsed_seconds=2.5, checkpoint_rerun_elapsed_seconds={3: 2.5}, error_outputs=[])  # 构造一个最小批量执行摘要。
        with mock.patch('rerun_tool.runner._run_maven_nondex_batch_with_summary', return_value=expected_summary) as mocked_batch:  # 拦截实际的 NonDex 批量执行逻辑。
            summary = run_test_with_summary(repo_dir='/tmp/demo', entry=entry, rerun_count=3, mode=mock.Mock(), use_docker=False, timeout=30, runner_backend=RunnerBackend.NONDEX, execution_env=execution_env)  # 调用统一执行入口。
        self.assertIs(summary, expected_summary)  # 断言主入口直接返回批量执行摘要。
        mocked_batch.assert_called_once_with(repo_dir='/tmp/demo', entry=entry, total_runs=3, timeout=30, use_docker=False, docker_image=None)  # 断言当前只会发起一次批量 NonDex 实验。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试。
