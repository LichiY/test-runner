import tempfile  # 导入临时目录工具用于构造隔离测试仓库。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。

from rerun_tool.data import TestEntry  # 导入数据结构构造最小测试样本。
from rerun_tool.patch import apply_patch, fix_missing_imports  # 导入待测的补丁应用与 import 修复函数。


def _make_entry(flaky_code: str, generated_patch: str) -> TestEntry:  # 根据测试场景创建最小数据样本。
    return TestEntry(  # 返回只填充必要字段的测试条目。
        index=0,  # 伪造 CSV 行号。
        repo_url='https://example.com/repo.git',  # 伪造仓库地址。
        repo_owner='example',  # 伪造仓库 owner。
        project_name='demo',  # 伪造项目名。
        original_sha='a' * 40,  # 提供长度正确的伪造提交号。
        fixed_sha='b' * 40,  # 提供长度正确的伪造修复提交号。
        module='.',  # 当前测试只关心单模块仓库。
        full_test_name='com.example.ExampleTest.flakyCase',  # 提供可解析的测试方法名。
        pr_link='',  # 当前测试不依赖 PR 链接。
        flaky_code=flaky_code,  # 写入测试场景使用的原 flaky 方法文本。
        fixed_code='',  # 当前测试不依赖 fixed_code。
        diff='',  # 当前测试不依赖 diff。
        generated_patch=generated_patch,  # 写入测试场景使用的目标补丁文本。
        is_correct='1',  # 伪造标签字段。
        source_file='',  # 当前测试不依赖 source_file。
    )  # 完成最小测试条目构造。


class PatchBehaviorTests(unittest.TestCase):  # 测试补丁定位与保护逻辑。
    def test_apply_patch_uses_reference_code_to_choose_right_method(self):  # 验证多候选同名方法时会选择最像 flaky 代码的那个方法。
        flaky_code = (  # 构造外层目标方法作为参考代码。
            'public void flakyCase() {\n'  # 方法签名行。
            '    int value = 1;\n'  # 目标方法中的特征语句。
            '    assertEquals(1, value);\n'  # 目标方法中的断言语句。
            '}'  # 方法结束行。
        )  # 完成参考方法文本构造。
        generated_patch = (  # 构造要贴入的修复补丁文本。
            'public void flakyCase() {\n'  # 方法签名行。
            '    int value = 2;\n'  # 修复后将 value 修改为 2。
            '    assertEquals(2, value);\n'  # 修复后的断言语句。
            '}'  # 方法结束行。
        )  # 完成目标补丁文本构造。
        source = (  # 构造包含两个同名方法的 Java 文件。
            'public class ExampleTest {\n'  # 外层测试类开始。
            '    public void flakyCase() {\n'  # 外层方法签名。
            '        int value = 1;\n'  # 外层方法的特征语句。
            '        assertEquals(1, value);\n'  # 外层方法的断言语句。
            '    }\n'  # 外层方法结束。
            '\n'  # 添加空行增强可读性。
            '    static class Nested {\n'  # 嵌套类开始。
            '        @Test\n'  # 故意让错误候选更像测试方法。
            '        public void flakyCase() {\n'  # 内层同名方法签名。
            '            int value = 99;\n'  # 内层方法的不同特征语句。
            '            assertEquals(99, value);\n'  # 内层方法的不同断言语句。
            '        }\n'  # 内层方法结束。
            '    }\n'  # 嵌套类结束。
            '}\n'  # 外层测试类结束。
        )  # 完成测试源文件构造。
        entry = _make_entry(flaky_code, generated_patch)  # 构造待应用补丁的测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入 Java 测试源文件。
            ok, message = apply_patch(str(test_file), entry)  # 执行补丁应用流程。
            patched = test_file.read_text(encoding='utf-8')  # 读取补丁后的文件内容。
        self.assertTrue(ok, message)  # 断言补丁应用成功。
        self.assertIn('int value = 2;', patched)  # 断言外层目标方法被替换为补丁内容。
        self.assertIn('assertEquals(99, value);', patched)  # 断言内层同名方法保持不变。

    def test_apply_patch_rejects_low_similarity_target(self):  # 验证当目标方法与 flaky 代码不匹配时会主动拒绝补丁。
        flaky_code = (  # 构造与真实文件明显不一致的参考代码。
            'public void flakyCase() {\n'  # 方法签名行。
            '    cleanupSharedState();\n'  # 使用完全不同的方法调用。
            '    assertTrue(cache.isEmpty());\n'  # 使用完全不同的断言结构。
            '}'  # 方法结束行。
        )  # 完成错误参考代码构造。
        generated_patch = (  # 构造形式上合法但不该被应用的补丁文本。
            'public void flakyCase() {\n'  # 方法签名行。
            '    int value = 5;\n'  # 任意替换内容。
            '    assertEquals(5, value);\n'  # 任意断言内容。
            '}'  # 方法结束行。
        )  # 完成目标补丁文本构造。
        source = (  # 构造真实文件中的方法内容。
            'public class ExampleTest {\n'  # 测试类开始。
            '    public void flakyCase() {\n'  # 方法签名行。
            '        int value = 1;\n'  # 实际文件中的特征语句。
            '        assertEquals(1, value);\n'  # 实际文件中的断言语句。
            '    }\n'  # 方法结束行。
            '}\n'  # 测试类结束。
        )  # 完成测试源文件构造。
        entry = _make_entry(flaky_code, generated_patch)  # 构造与真实文件不一致的测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入 Java 测试源文件。
            ok, message = apply_patch(str(test_file), entry)  # 执行补丁应用流程。
            current = test_file.read_text(encoding='utf-8')  # 读取最终文件内容确认未被错误修改。
        self.assertFalse(ok)  # 断言补丁应用被安全拒绝。
        self.assertIn('Target method mismatch', message)  # 断言失败原因来自相似度保护逻辑。
        self.assertIn('int value = 1;', current)  # 断言原始文件内容仍然保持不变。

    def test_fix_missing_imports_adds_safe_java_util_import(self):  # 验证可以根据编译错误自动补充高置信度的 java.util import。
        source = (  # 构造一个缺少 Arrays import 的最小 Java 文件。
            'package com.example;\n'  # package 声明行。
            '\n'  # package 后的空行。
            'import junit.framework.TestCase;\n'  # 现有 import 行。
            '\n'  # import 区块后的空行。
            'public class ExampleTest extends TestCase {\n'  # 类声明行。
            '    public void flakyCase() {\n'  # 方法签名行。
            '        assertEquals(1, Arrays.asList(1).size());\n'  # 使用 Arrays 但当前未导入。
            '    }\n'  # 方法结束行。
            '}\n'  # 类结束行。
        )  # 完成测试源文件构造。
        build_output = (  # 伪造 Maven 编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[7,25] cannot find symbol\n'  # 错误位置行。
            '[ERROR]   symbol:   variable Arrays\n'  # 缺失符号行。
            '[ERROR]   location: class com.example.ExampleTest\n'  # 错误位置说明行。
        )  # 完成编译错误输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言自动修复成功执行。
        self.assertIn('import java.util.Arrays;', updated)  # 断言缺失的 Arrays import 已被添加。
        self.assertIn('import junit.framework.TestCase;', updated)  # 断言原有 import 没有被破坏。

    def test_fix_missing_imports_can_resolve_unique_project_class(self):  # 验证可以在仓库源码中定位唯一类定义并补充 import。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            helper_file = repo_dir / 'src' / 'main' / 'java' / 'com' / 'example' / 'lib' / 'TypeReference.java'  # 构造仓库内唯一类的源码路径。
            helper_file.parent.mkdir(parents=True)  # 创建辅助类所在目录。
            helper_file.write_text(  # 写入一个最小可识别的 Java 类。
                'package com.example.lib;\n'  # 声明辅助类所在包名。
                '\n'  # 包声明后的空行。
                'public class TypeReference {}\n',  # 写入最小类定义。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成辅助类源码写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入缺少 TypeReference import 的测试类。
                'package com.example.tests;\n'  # 声明测试类所在包名。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        TypeReference value = null;\n'  # 使用仓库内唯一类但当前未导入。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
                '[ERROR]   symbol:   class TypeReference\n'  # 缺失类名行。
                '[ERROR]   location: class com.example.tests.ExampleTest\n'  # 错误位置说明行。
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertTrue(ok, message)  # 断言仓库内唯一类可以被成功解析并导入。
        self.assertIn('import com.example.lib.TypeReference;', updated)  # 断言已补入正确的项目内类 import。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
