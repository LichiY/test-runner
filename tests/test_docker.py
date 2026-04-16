import subprocess  # 导入子进程结果类型用于伪造命令执行结果。
import tempfile  # 导入临时目录工具用于构造隔离测试仓库。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。
from unittest import mock  # 导入 mock 以便隔离外部依赖。

from rerun_tool import docker  # 导入 Docker 相关模块进行行为测试。
from rerun_tool import runner  # 导入执行链模块验证 wrapper 选择逻辑。
from rerun_tool.data import TestEntry  # 导入数据结构构造最小测试样本。


def _make_entry(module: str = '.') -> TestEntry:  # 创建用于测试的最小数据样本。
    return TestEntry(  # 返回带默认字段的测试条目。
        index=0,  # 伪造 CSV 行号。
        repo_url='https://example.com/repo.git',  # 伪造仓库地址。
        repo_owner='example',  # 伪造仓库 owner。
        project_name='demo',  # 伪造项目名。
        original_sha='a' * 40,  # 提供长度正确的伪造提交号。
        fixed_sha='b' * 40,  # 提供长度正确的伪造修复提交号。
        module=module,  # 允许测试时覆盖模块字段。
        full_test_name='com.example.SampleTest.testCase',  # 提供最小可解析的测试名。
        pr_link='',  # 该测试不需要 PR 链接。
        flaky_code='public void testCase() { assertEquals(1, 1); }',  # 提供最小 flaky 方法文本。
        fixed_code='',  # 当前测试不依赖 fixed_code。
        diff='',  # 当前测试不依赖 diff。
        generated_patch='public void testCase() { assertEquals(1, 1); }',  # 提供最小生成补丁。
        is_correct='1',  # 伪造标签字段。
        source_file='',  # 当前测试不依赖 source_file。
    )  # 结束最小测试条目构造。


class DockerBehaviorTests(unittest.TestCase):  # 测试 Docker 环境检测与 wrapper 选择逻辑。
    def test_detect_java_version_prefers_module_property(self):  # 验证模块级 pom 属性会覆盖仓库根配置。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于操作。
            (repo_dir / 'pom.xml').write_text(  # 在根目录写入较旧的 Java 版本配置。
                '<project><properties><java.version>8</java.version></properties></project>',  # 根 pom 指向 Java 8。
                encoding='utf-8',  # 使用 UTF-8 编码写入文本。
            )  # 完成根 pom 写入。
            module_dir = repo_dir / 'module-a'  # 构造模块目录路径。
            module_dir.mkdir()  # 创建模块目录。
            (module_dir / 'pom.xml').write_text(  # 在模块 pom 中写入属性引用形式的版本配置。
                '<project><properties><custom.java>17</custom.java></properties><maven.compiler.release>${custom.java}</maven.compiler.release></project>',  # 模块 pom 通过属性引用 Java 17。
                encoding='utf-8',  # 使用 UTF-8 编码写入文本。
            )  # 完成模块 pom 写入。
            detected = docker.detect_java_version(str(repo_dir), 'module-a')  # 执行模块级 Java 版本检测。
            self.assertEqual(detected, '17')  # 断言检测结果优先采用模块级配置。

    def test_detect_java_version_follows_parent_relative_path(self):  # 验证 Maven 模块可以沿 parent relativePath 继承 Java 版本。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于操作。
            parent_dir = repo_dir / 'build-parent'  # 构造本地 parent pom 所在目录。
            parent_dir.mkdir()  # 创建 parent pom 目录。
            (parent_dir / 'pom.xml').write_text(  # 在 parent pom 中写入 Java 8 配置。
                '<project><properties><maven.compiler.source>1.8</maven.compiler.source></properties></project>',  # parent pom 直接声明 Java 8。
                encoding='utf-8',  # 使用 UTF-8 编码写入文本。
            )  # 完成 parent pom 写入。
            module_dir = repo_dir / 'module-a'  # 构造模块目录路径。
            module_dir.mkdir()  # 创建模块目录。
            (module_dir / 'pom.xml').write_text(  # 在模块 pom 中通过 parent relativePath 引用上面的 parent pom。
                '<project><parent><groupId>x</groupId><artifactId>y</artifactId><version>1</version><relativePath>../build-parent</relativePath></parent></project>',  # 模块 pom 只声明 parent 目录而不直接写 pom.xml。
                encoding='utf-8',  # 使用 UTF-8 编码写入文本。
            )  # 完成模块 pom 写入。
            detected = docker.detect_java_version(str(repo_dir), 'module-a')  # 执行模块级 Java 版本检测。
            self.assertEqual(detected, '1.8')  # 断言检测结果可以沿 parent relativePath 找到 Java 版本。

    def test_get_docker_image_overrides_json_java_to_jdk11(self):  # 验证 JSON-java 会绕开 JDK8 编译器边界并改用 JDK11 镜像。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于写入 pom。
            (repo_dir / 'pom.xml').write_text(  # 写入 JSON-java 的最小项目签名和旧源码级别配置。
                '<project>'
                '<name>JSON in Java</name>'
                '<url>https://github.com/douglascrockford/JSON-java</url>'
                '<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration><source>1.6</source><target>1.6</target></configuration></plugin></plugins></build>'
                '</project>',
                encoding='utf-8',
            )  # 完成测试 pom 写入。
            image = docker.get_docker_image(str(repo_dir), build_tool='maven')  # 读取当前仓库会选择的 Docker 镜像。
        self.assertEqual(image, 'maven:3.8.6-openjdk-11')  # 断言 JSON-java 不再走 JDK8 镜像。

    def test_should_use_docker_prefers_container_when_version_unknown(self):  # 验证未知版本时会优先选择 Docker 提高可复现性。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建一个没有构建配置的空仓库目录。
            with mock.patch('rerun_tool.docker.is_docker_available', return_value=True):  # 模拟 Docker 可用。
                self.assertTrue(docker.should_use_docker(tmp_dir))  # 断言未知版本时默认走 Docker。

    def test_check_local_jdk_rejects_modern_jdk_for_legacy_source_level(self):  # 验证过新的本地 JDK 不会被误判为兼容 Java 5/6/7 项目。
        fake_version = subprocess.CompletedProcess(args=['java', '-version'], returncode=0, stdout='', stderr='openjdk version "17.0.1"')  # 伪造本地 JDK 17 的版本输出。
        with mock.patch('rerun_tool.docker.subprocess.run', return_value=fake_version):  # 拦截 java -version 调用。
            self.assertFalse(docker.check_local_jdk('1.5'))  # 断言 Java 5 项目不会再被误判为本地可编译。

    def test_detect_build_tool_prefers_module_build_files(self):  # 验证构建工具探测会优先参考模块目录而不是只看仓库根目录。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            (repo_dir / 'build.gradle').write_text('plugins {}', encoding='utf-8')  # 在仓库根目录写入 Gradle 构建脚本作为干扰项。
            module_dir = repo_dir / 'module-a'  # 构造模块目录路径。
            module_dir.mkdir()  # 创建模块目录。
            (module_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 在模块目录写入 Maven 配置文件。
            detected = runner.detect_build_tool(str(repo_dir), 'module-a')  # 按模块路径执行构建工具探测。
        self.assertEqual(detected, 'maven')  # 断言最靠近模块的 Maven 配置会覆盖仓库根目录的 Gradle 干扰项。

    def test_build_project_blocks_local_fallback_when_docker_is_required(self):  # 验证显式要求 Docker 时不会再静默回退到本地执行。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            entry = _make_entry()  # 构造最小测试条目。
            with mock.patch('rerun_tool.runner.is_docker_available', return_value=False), mock.patch('rerun_tool.runner._build_maven') as mocked_build:  # 模拟 Docker 不可用并拦截后续构建调用。
                success, output = runner.build_project(str(repo_dir), entry, use_docker=True, timeout=30, max_retries=0, docker_fallback_allowed=False)  # 在严格 Docker 模式下执行构建。
        self.assertFalse(success)  # 断言当前构建会被明确阻断。
        self.assertIn('explicitly required', output)  # 断言错误信息会明确指出 Docker 是被显式要求的。
        mocked_build.assert_not_called()  # 断言在严格模式下不会继续偷偷走本地构建。

    def test_build_project_falls_back_to_local_only_when_compatible(self):  # 验证自动模式下只有在本地 JDK 明确兼容时才允许回退到本地构建。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            entry = _make_entry()  # 构造最小测试条目。
            with mock.patch('rerun_tool.runner.is_docker_available', return_value=False), mock.patch('rerun_tool.runner.detect_java_version', return_value='11'), mock.patch('rerun_tool.runner.check_local_jdk', return_value=True), mock.patch('rerun_tool.runner._build_maven', return_value=(True, 'ok')) as mocked_build:  # 模拟 Docker 不可用但本地 JDK 明确兼容。
                success, output = runner.build_project(str(repo_dir), entry, use_docker=True, timeout=30, max_retries=0, docker_fallback_allowed=True)  # 在允许回退的自动模式下执行构建。
        self.assertTrue(success)  # 断言在本地 JDK 兼容时可以安全回退并成功构建。
        self.assertEqual(output, 'ok')  # 断言构建输出会被正确透传。
        self.assertFalse(mocked_build.call_args.args[3])  # 断言最终传给 Maven 构建器的是本地执行模式。
        self.assertIsNone(mocked_build.call_args.args[4])  # 断言回退到本地后不会再携带 Docker 镜像。 

    def test_build_maven_uses_wrapper_inside_docker(self):  # 验证 Maven 构建在容器内也优先使用 mvnw。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            (repo_dir / 'mvnw').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')  # 创建可执行的 Maven wrapper 文件。
            entry = _make_entry()  # 构造最小测试条目。
            with mock.patch('rerun_tool.runner._run_in_docker', return_value=(True, 'ok')) as mocked_run:  # 拦截容器执行调用。
                success, output = runner._build_maven(str(repo_dir), entry, 30, True, 'demo-image')  # 调用待测的 Maven 构建逻辑。
            self.assertTrue(success)  # 断言伪造构建结果被正确透传。
            self.assertEqual(output, 'ok')  # 断言伪造输出被正确透传。
            docker_cmd = mocked_run.call_args.args[2]  # 读取传入容器的命令列表。
            self.assertEqual(docker_cmd[0], './mvnw')  # 断言容器内使用了项目 wrapper。

    def test_build_maven_includes_stability_skip_flags(self):  # 验证 Maven 构建命令会携带保守的稳定性降噪参数。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            entry = _make_entry()  # 构造最小测试条目。
            with mock.patch('rerun_tool.runner._run_local', return_value=(True, 'ok')) as mocked_run:  # 拦截本地构建命令执行。
                success, output = runner._build_maven(str(repo_dir), entry, 30, False, None)  # 调用待测的 Maven 构建逻辑。
            self.assertTrue(success)  # 断言伪造构建结果被正确透传。
            self.assertEqual(output, 'ok')  # 断言伪造输出被正确透传。
            local_cmd = mocked_run.call_args.args[0]  # 读取传入本地执行器的命令列表。
            self.assertIn('-U', local_cmd)  # 断言 Maven 会强制重新检查此前缓存的缺失依赖。
            self.assertIn('.rerun_tool.maven-settings.xml', ' '.join(local_cmd))  # 断言 Maven 会显式使用工具生成的隔离 settings 文件。
            self.assertIn('-Dstyle.color=never', local_cmd)  # 断言会关闭彩色输出以稳定日志解析。
            self.assertIn('-Dcheckstyle.skip=true', local_cmd)  # 断言会跳过 Checkstyle 这类无关质量插件。
            self.assertIn('-Dspotbugs.skip=true', local_cmd)  # 断言会跳过 SpotBugs 这类无关质量插件。
            self.assertIn('-Ddependency-check.skip=true', local_cmd)  # 断言会跳过依赖安全扫描。
            self.assertIn('-Dskip.web.build=true', local_cmd)  # 断言会显式关闭 Graylog 一类与目标测试无关的前端构建 profile。
            self.assertIn('-Dfrontend.skip=true', local_cmd)  # 断言会尽量关闭 frontend-maven-plugin 这类前端步骤。

    def test_get_build_env_uses_isolated_local_caches(self):  # 验证本地构建环境会使用工作区内隔离的 Maven 和 Gradle 缓存目录。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir) / 'workspace' / 'demo-repo'  # 构造一个位于 workspace 目录下的仓库根路径。
            repo_dir.mkdir(parents=True)  # 创建伪造仓库根目录。
            with mock.patch.dict('os.environ', {}, clear=True):  # 清空环境变量以便稳定断言默认值。
                env = runner._get_build_env(str(repo_dir))  # 生成待测的本地构建环境变量集合。
        self.assertEqual(env['CI'], 'true')  # 断言会显式标记当前环境为 CI。
        self.assertIn('-Xmx2g -Xms512m', env['MAVEN_OPTS'])  # 断言 Maven JVM 默认内存参数仍然保留。
        self.assertIn('.rerun_tool_cache/m2-repository', env['MAVEN_OPTS'])  # 断言 Maven 会强制使用隔离后的本地仓库。
        self.assertEqual(env['GRADLE_OPTS'], '-Xmx2g -Xms512m')  # 断言默认 Gradle JVM 参数仍然保留。
        self.assertTrue(env['GRADLE_USER_HOME'].endswith('.rerun_tool_cache/gradle-user-home'))  # 断言 Gradle 也会使用隔离后的用户目录。

    def test_build_maven_falls_back_to_plain_maven_after_wrapper_bootstrap_failure(self):  # 验证 Docker 中 wrapper 下载失败后会自动退回镜像自带 mvn。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            (repo_dir / 'mvnw').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')  # 创建可执行的 Maven wrapper 文件。
            entry = _make_entry()  # 构造最小测试条目。
            wrapper_error = (  # 伪造 Maven wrapper 下载分发包时的网络失败输出。
                'org.apache.maven.wrapper.DefaultDownloader\n'  # 模拟 wrapper 下载栈信息。
                'java.io.EOFException: SSL peer shut down incorrectly\n'  # 模拟典型的 SSL EOF 网络错误。
            )  # 完成 wrapper 失败输出构造。
            with mock.patch('rerun_tool.runner._run_in_docker', side_effect=[(False, wrapper_error), (True, 'ok')]) as mocked_run:  # 让第一次 wrapper 失败、第二次 plain mvn 成功。
                success, output = runner._build_maven(str(repo_dir), entry, 30, True, 'demo-image')  # 调用待测的 Maven 构建逻辑。
            self.assertTrue(success)  # 断言回退后的第二次构建成功。
            self.assertEqual(output, 'ok')  # 断言成功输出被正确透传。
            self.assertEqual(mocked_run.call_count, 2)  # 断言确实先后尝试了两个 Docker 命令候选。
            first_cmd = mocked_run.call_args_list[0].args[2]  # 读取第一次调用的命令列表。
            second_cmd = mocked_run.call_args_list[1].args[2]  # 读取第二次调用的命令列表。
            self.assertEqual(first_cmd[0], './mvnw')  # 断言第一候选仍然是项目 wrapper。
            self.assertEqual(second_cmd[0], 'mvn')  # 断言第二候选正确回退到镜像自带 mvn。

    def test_get_docker_maven_cmd_variants_rewrites_settings_path_to_workspace(self):  # 验证 Docker 中 Maven settings 参数会被改写成容器内可见路径。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以便生成 Maven settings 文件。
            cmd_parts = ['test'] + runner._maven_cli_args(str(repo_dir))  # 生成本地 Maven 参数序列。
            variants = runner._get_docker_maven_cmd_variants(str(repo_dir), cmd_parts)  # 转换出 Docker 中实际使用的命令候选。
        docker_cmd = variants[0]  # 读取首个 Docker 命令候选。
        self.assertIn('/workspace/.rerun_tool.maven-settings.xml', docker_cmd)  # 断言 settings 文件路径已经被改写成容器路径。
        self.assertNotIn(f'{repo_dir}/.rerun_tool.maven-settings.xml', docker_cmd)  # 断言不会再把宿主机绝对路径直接传进容器。

    def test_run_gradle_test_uses_wrapper_inside_docker(self):  # 验证 Gradle 测试在容器内也优先使用 gradlew。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'build.gradle').write_text('plugins {}', encoding='utf-8')  # 写入最小 Gradle 构建脚本。
            (repo_dir / 'gradlew').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')  # 创建可执行的 Gradle wrapper 文件。
            entry = _make_entry()  # 构造最小测试条目。
            fake_result = subprocess.CompletedProcess(args=['./gradlew'], returncode=0, stdout='BUILD SUCCESSFUL', stderr='')  # 伪造一次成功的 Gradle 测试执行结果。
            with mock.patch('rerun_tool.runner.docker_run', return_value=fake_result) as mocked_run:  # 拦截实际的 docker_run 调用。
                result = runner._run_gradle_test(str(repo_dir), entry, runner.RerunMode.ISOLATED, 30, True, 'demo-image')  # 调用待测的 Gradle 测试逻辑。
            self.assertEqual(result, 'pass')  # 断言成功输出被解析为 pass。
            docker_cmd = mocked_run.call_args.args[2]  # 读取传入容器的命令列表。
            self.assertEqual(docker_cmd[0], './gradlew')  # 断言容器内使用了项目 wrapper。

    def test_run_maven_test_disables_no_match_failures_for_upstream_modules(self):  # 验证 Maven 测试命令会关闭上游模块未匹配测试导致的失败。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            (repo_dir / 'mvnw').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')  # 创建可执行的 Maven wrapper 文件。
            entry = _make_entry(module='module-a')  # 构造带模块名的测试条目。
            fake_result = subprocess.CompletedProcess(  # 伪造一次成功的 Maven 测试执行结果。
                args=['./mvnw'],  # 记录伪造命令参数。
                returncode=0,  # 让测试执行返回成功状态。
                stdout='Tests run: 1, Failures: 0, Errors: 0',  # 提供可被解析为 pass 的 Surefire 输出。
                stderr='',  # 当前测试不需要错误输出。
            )  # 完成伪造结果构造。
            with mock.patch('rerun_tool.runner.docker_run', return_value=fake_result) as mocked_run:  # 拦截实际的 docker_run 调用。
                result = runner._run_maven_test(str(repo_dir), entry, runner.RerunMode.ISOLATED, 30, True, 'demo-image')  # 调用待测的 Maven 测试逻辑。
            self.assertEqual(result, 'pass')  # 断言成功输出被解析为 pass。
            docker_cmd = mocked_run.call_args.args[2]  # 读取传入容器的命令列表。
            self.assertIn('-DfailIfNoTests=false', docker_cmd)  # 断言命令中包含忽略未匹配测试的参数。
            self.assertIn('-Dsurefire.failIfNoSpecifiedTests=false', docker_cmd)  # 断言命令中包含忽略指定测试未命中的参数。
            self.assertIn('-Dmaven.test.failure.ignore=true', docker_cmd)  # 断言命令会保留失败测试输出供工具自行判断。
            self.assertIn('-U', docker_cmd)  # 断言测试阶段同样会强制重新检查此前缓存的缺失依赖。
            self.assertIn('.rerun_tool.maven-settings.xml', ' '.join(docker_cmd))  # 断言测试阶段会显式使用隔离 settings 文件。
            self.assertIn('-Dstyle.color=never', docker_cmd)  # 断言命令会关闭彩色输出以稳定日志解析。

    def test_run_maven_nondex_test_uses_nondex_goal(self):  # 验证 Maven NonDex 执行路径会使用正确的插件目标与参数。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将路径包装为 Path。
            (repo_dir / 'pom.xml').write_text('<project/>', encoding='utf-8')  # 写入最小 pom 以触发 Maven 路径。
            (repo_dir / 'mvnw').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')  # 创建可执行的 Maven wrapper 文件。
            entry = _make_entry(module='module-a')  # 构造带模块名的测试条目。
            fake_result = subprocess.CompletedProcess(args=['./mvnw'], returncode=0, stdout='Tests run: 1, Failures: 0, Errors: 0', stderr='')  # 伪造一次成功的 NonDex 执行结果。
            with mock.patch('rerun_tool.runner.docker_run', return_value=fake_result) as mocked_run:  # 拦截实际的 docker_run 调用。
                result = runner._run_maven_nondex_test(str(repo_dir), entry, 30, True, 'demo-image')  # 调用待测的 NonDex 执行逻辑。
            self.assertEqual(result, 'pass')  # 断言成功输出会被解析为 pass。
            docker_cmd = mocked_run.call_args.args[2]  # 读取传入容器的命令列表。
            self.assertIn('-U', docker_cmd)  # 断言 NonDex 路径同样会强制刷新 Maven 依赖缓存。
            self.assertIn('.rerun_tool.maven-settings.xml', ' '.join(docker_cmd))  # 断言 NonDex 路径同样会显式使用隔离 settings 文件。
            self.assertIn('edu.illinois:nondex-maven-plugin:2.1.7:nondex', docker_cmd)  # 断言命令中使用了正确的 NonDex 插件目标。
            self.assertIn('-DnondexRuns=1', docker_cmd)  # 断言外层每次重跑只触发一次 NonDex 扰动。
            self.assertIn('-pl', docker_cmd)  # 断言多模块项目仍会带上模块参数。

    def test_parse_test_result_treats_build_failure_without_summary_as_error(self):  # 验证没有测试汇总时的 Maven 构建失败会被识别为 error。
        output = (  # 构造一个只有 Maven 构建失败而没有测试摘要的输出。
            '[INFO] BUILD FAILURE\n'  # Maven 构建失败总括。
            '[ERROR] Failed to execute goal org.apache.maven.plugins:maven-enforcer-plugin:3.0.0:enforce\n'  # 典型的插件级构建失败。
        )  # 完成输出构造。
        self.assertEqual(runner._parse_test_result(1, output), 'error')  # 断言此类失败不会被误判为测试 fail。

    def test_parse_test_result_treats_chinese_compilation_error_as_error(self):  # 验证中文编译错误输出会被识别为 error。
        output = (  # 构造一个中文 Maven 编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] 找不到符号\n'  # 中文缺失符号错误位置行。
            '[ERROR]   符号:   变量 JSONAssert\n'  # 中文缺失符号说明行。
        )  # 完成中文输出构造。
        self.assertEqual(runner._parse_test_result(1, output), 'error')  # 断言中文编译失败不会落入返回码兜底的 fail。

    def test_parse_test_result_prefers_executed_summary_over_later_zero_test_summary(self):  # 验证多模块输出中后续 `Tests run: 0` 不会覆盖前面真正执行成功的目标测试结果。
        output = (  # 构造一个先成功执行目标测试、后出现零测试汇总噪声的 Maven 输出。
            '[INFO] Running com.example.TargetTest\n'  # 模拟目标测试开始执行。
            '[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n'  # 模拟目标测试成功执行的汇总行。
            '[INFO] Reactor Summary for Demo:\n'  # 模拟后续进入多模块汇总阶段。
            '[INFO] helper-module .................................... SUCCESS\n'  # 模拟其他模块的成功摘要。
            '[INFO] Tests run: 0, Failures: 0, Errors: 0, Skipped: 0\n'  # 模拟后续模块产生的零测试汇总噪声。
        )  # 完成多模块 Maven 输出构造。
        self.assertEqual(runner._parse_test_result(1, output), 'pass')  # 断言当前解析会优先保留真正执行过的成功测试结果。

    def test_tail_command_output_prefers_relevant_test_marker_window(self):  # 验证 RUN_ERROR 诊断会优先围绕关键测试标记提取输出窗口而不是盲目截日志尾部。
        output = (  # 构造一个前半段包含关键错误、尾部只有无关成功摘要的长输出。
            ('prefix line\n' * 120)  # 先堆叠一段无关前缀以确保输出足够长。
            + 'Tests run: 0, Failures: 0, Errors: 0\n'  # 在中间加入真正解释 RUN_ERROR 的关键信息。
            + 'No tests were executed!\n'  # 再加入更直接的未执行测试提示。
            + ('suffix success line\n' * 120)  # 在尾部堆叠大量无关行，模拟最后只剩 BUILD SUCCESS 摘要。
        )  # 完成长输出构造。
        focused = runner._tail_command_output(output, limit=200)  # 执行输出聚焦逻辑并压缩到较小长度。
        self.assertIn('Tests run: 0', focused)  # 断言结果会优先保留关键测试标记窗口。
        self.assertIn('No tests were executed!', focused)  # 断言未执行测试提示也会被一并保留下来。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
