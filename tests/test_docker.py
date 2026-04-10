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

    def test_should_use_docker_prefers_container_when_version_unknown(self):  # 验证未知版本时会优先选择 Docker 提高可复现性。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建一个没有构建配置的空仓库目录。
            with mock.patch('rerun_tool.docker.is_docker_available', return_value=True):  # 模拟 Docker 可用。
                self.assertTrue(docker.should_use_docker(tmp_dir))  # 断言未知版本时默认走 Docker。

    def test_check_local_jdk_rejects_modern_jdk_for_legacy_source_level(self):  # 验证过新的本地 JDK 不会被误判为兼容 Java 5/6/7 项目。
        fake_version = subprocess.CompletedProcess(args=['java', '-version'], returncode=0, stdout='', stderr='openjdk version "17.0.1"')  # 伪造本地 JDK 17 的版本输出。
        with mock.patch('rerun_tool.docker.subprocess.run', return_value=fake_version):  # 拦截 java -version 调用。
            self.assertFalse(docker.check_local_jdk('1.5'))  # 断言 Java 5 项目不会再被误判为本地可编译。

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


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
