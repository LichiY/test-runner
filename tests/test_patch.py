import tempfile  # 导入临时目录工具用于构造隔离测试仓库。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。

from rerun_tool.data import TestEntry  # 导入数据结构构造最小测试样本。
from rerun_tool.patch import (apply_generated_patch_context, apply_patch, apply_reference_patch_context, find_reference_context_candidates, find_reference_patch_candidates,  # 导入待测的补丁应用、上下文推断与离线参考候选检索函数。
                              fix_missing_imports, fix_unreported_exception_declaration)  # 导入保守源码修复函数。


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

    def test_apply_patch_can_allow_low_similarity_when_method_is_unique(self):  # 验证参考补丁回退在目标文件里只有一个同名方法时可以放宽相似度门槛。
        flaky_code = (  # 构造与真实文件明显不一致的参考代码。
            'public void flakyCase() {\n'
            '    cleanupSharedState();\n'
            '    assertTrue(cache.isEmpty());\n'
            '}\n'
        )  # 完成错误参考代码构造。
        generated_patch = (  # 构造一个只有在参考回退场景下才应该继续尝试的补丁。
            'public void flakyCase() {\n'
            '    int value = 5;\n'
            '    assertEquals(5, value);\n'
            '}\n'
        )  # 完成目标补丁文本构造。
        source = (  # 构造只包含一个目标方法的 Java 文件。
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        int value = 1;\n'
            '        assertEquals(1, value);\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        entry = _make_entry(flaky_code, generated_patch)  # 构造参考补丁回退场景的测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入 Java 测试源文件。
            ok, message = apply_patch(str(test_file), entry, allow_low_similarity_on_unique_match=True)  # 显式启用“唯一方法可放宽相似度”保护。
            patched = test_file.read_text(encoding='utf-8')  # 读取最终文件内容确认补丁已被应用。
        self.assertTrue(ok, message)  # 断言当前唯一方法场景会继续尝试应用补丁。
        self.assertIn('int value = 5;', patched)  # 断言最终文件已经替换成参考补丁内容。

    def test_apply_patch_allows_low_similarity_target_when_method_is_unique(self):  # 验证参考补丁回退在文件内目标方法唯一时可以放宽相似度保护。
        flaky_code = (  # 构造与真实文件明显不一致的参考代码，模拟参考补丁大幅改写方法体的场景。
            'public void flakyCase() {\n'
            '    cleanupSharedState();\n'
            '    assertTrue(cache.isEmpty());\n'
            '}\n'
        )  # 完成错误参考代码构造。
        generated_patch = (  # 构造一个可以安全替换唯一目标方法的方法体。
            'public void flakyCase() {\n'
            '    assertEquals(2, 2);\n'
            '}\n'
        )  # 完成候选补丁文本构造。
        source = (  # 构造只包含一个目标方法的最小 Java 文件。
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        assertEquals(1, 1);\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        entry = _make_entry(flaky_code, generated_patch)  # 构造待应用补丁的测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入 Java 测试源文件。
            ok, message = apply_patch(str(test_file), entry, allow_low_similarity_on_unique_match=True)  # 显式打开“唯一方法可放宽”的参考补丁模式。
            patched = test_file.read_text(encoding='utf-8')  # 读取补丁后的文件内容。
        self.assertTrue(ok, message)  # 断言当前参考补丁模式可以成功应用补丁。
        self.assertIn('assertEquals(2, 2);', patched)  # 断言唯一目标方法已经被替换为候选补丁。

    def test_apply_patch_preserves_original_method_declaration(self):  # 验证当生成补丁篡改方法头时会保留原始声明。
        flaky_code = (  # 构造原始 flaky 方法文本。
            '@Test\n'  # 原始方法带有测试注解。
            'public void flakyCase() throws Exception {\n'  # 原始方法声明包含 throws。
            '    int value = 1;\n'  # 原始方法体中的语句。
            '    assertEquals(1, value);\n'  # 原始方法体中的断言。
            '}\n'  # 原始方法结束。
        )  # 完成参考方法文本构造。
        generated_patch = (  # 构造一个错误地修改了方法头但方法体仍然合法的补丁。
            'public synchronized void flakyCase() {\n'  # 生成补丁错误地修改了修饰符并移除了 throws。
            '    int value = 2;\n'  # 修复后将 value 修改为 2。
            '    assertEquals(2, value);\n'  # 修复后的断言语句。
            '}\n'  # 方法结束。
        )  # 完成补丁文本构造。
        source = (  # 构造包含目标方法的 Java 文件。
            'public class ExampleTest {\n'  # 测试类开始。
            '    @Test\n'  # 原始测试注解。
            '    public void flakyCase() throws Exception {\n'  # 原始方法声明。
            '        int value = 1;\n'  # 原始语句。
            '        assertEquals(1, value);\n'  # 原始断言。
            '    }\n'  # 原始方法结束。
            '}\n'  # 类结束。
        )  # 完成测试源文件构造。
        entry = _make_entry(flaky_code, generated_patch)  # 构造待应用补丁的测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入 Java 测试源文件。
            ok, message = apply_patch(str(test_file), entry)  # 执行补丁应用流程。
            patched = test_file.read_text(encoding='utf-8')  # 读取补丁后的文件内容。
        self.assertTrue(ok, message)  # 断言补丁应用成功。
        self.assertIn('@Test', patched)  # 断言原始测试注解被保留下来。
        self.assertIn('public void flakyCase() throws Exception {', patched)  # 断言原始方法声明没有被补丁错误篡改。
        self.assertIn('int value = 2;', patched)  # 断言方法体仍然应用了新补丁内容。
        self.assertNotIn('public synchronized void flakyCase()', patched)  # 断言错误的方法头没有残留在最终文件中。

    def test_apply_patch_keeps_patch_added_throws_clause(self):  # 验证声明保护不会抹掉补丁为可编译性新增的 throws 子句。
        flaky_code = (  # 构造原始 flaky 方法文本。
            'public void flakyCase() {\n'  # 原始方法声明不带 throws。
            '    callOldApi();\n'  # 原始方法体中的调用语句。
            '}\n'  # 原始方法结束。
        )  # 完成参考方法文本构造。
        generated_patch = (  # 构造一个通过新增 throws 来保持可编译性的补丁。
            'public synchronized void flakyCase() throws Exception {\n'  # 生成补丁新增 throws Exception。
            '    callNewApi();\n'  # 修复后的方法体语句。
            '}\n'  # 方法结束。
        )  # 完成补丁文本构造。
        source = (  # 构造包含目标方法的 Java 文件。
            'public class ExampleTest {\n'  # 测试类开始。
            '    public void flakyCase() {\n'  # 原始方法声明。
            '        callOldApi();\n'  # 原始方法体。
            '    }\n'  # 原始方法结束。
            '}\n'  # 类结束。
        )  # 完成测试源文件构造。
        entry = _make_entry(flaky_code, generated_patch)  # 构造待应用补丁的测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入 Java 测试源文件。
            ok, message = apply_patch(str(test_file), entry)  # 执行补丁应用流程。
            patched = test_file.read_text(encoding='utf-8')  # 读取补丁后的文件内容。
        self.assertTrue(ok, message)  # 断言补丁应用成功。
        self.assertIn('public void flakyCase() throws Exception {', patched)  # 断言补丁新增的 throws 子句被保留下来。
        self.assertIn('callNewApi();', patched)  # 断言方法体仍然应用了补丁内容。

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
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录并允许目录已存在。
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

    def test_fix_missing_imports_supports_chinese_maven_error_with_existing_repo_import_reference(self):  # 验证中文 Maven 输出在仓库内已有 import 线索时也能安全补入第三方类型。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个仓库内已经正确导入 JSONAssert 的参考文件。
            existing_usage_file.parent.mkdir(parents=True)  # 创建参考文件所在目录。
            existing_usage_file.write_text(  # 写入仓库内已有的正确第三方 import 线索。
                'package com.example.tests;\n'  # 声明参考测试类所在包名。
                '\n'  # package 后的空行。
                'import org.skyscreamer.jsonassert.JSONAssert;\n'  # 显式导入 JSONAssert。
                '\n'  # import 区块后的空行。
                'public class ExistingUsage {\n'  # 参考测试类声明行。
                '    Object value = JSONAssert.class;\n'  # 使用 JSONAssert 提供稳定线索。
                '}\n',  # 参考测试类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成参考文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入缺少 JSONAssert import 的测试类。
                'package com.example.tests;\n'  # package 声明行。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        JSONAssert.assertEquals("{}", "{}", false);\n'  # 使用 JSONAssert 但当前未导入。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造中文 Maven 编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] 找不到符号\n'  # 中文错误位置行。
                '[ERROR]   符号:   变量 JSONAssert\n'  # 中文缺失符号行。
                '[ERROR]   位置: 类 com.example.ExampleTest\n'  # 中文错误位置说明行。
            )  # 完成中文编译错误输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言中文错误输出在存在仓库线索时也可以被成功修复。
        self.assertIn('import org.skyscreamer.jsonassert.JSONAssert;', updated)  # 断言第三方类 import 会从仓库既有线索中被安全复用。

    def test_fix_missing_imports_adds_static_import_for_missing_method(self):  # 验证缺失静态方法时会补上高置信度 static import。
        source = (  # 构造一个调用 containsInAnyOrder 但未导入静态方法的最小 Java 文件。
            'package com.example;\n'  # package 声明行。
            '\n'  # package 后的空行。
            'public class ExampleTest {\n'  # 类声明行。
            '    public void flakyCase() {\n'  # 方法签名行。
            '        containsInAnyOrder("a", "b");\n'  # 直接调用缺失的静态方法。
            '    }\n'  # 方法结束行。
            '}\n'  # 类结束行。
        )  # 完成测试源文件构造。
        build_output = (  # 伪造缺失静态方法的 Maven 编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
            '[ERROR]   symbol:   method containsInAnyOrder(java.lang.String,java.lang.String)\n'  # 缺失方法行。
            '[ERROR]   location: class com.example.ExampleTest\n'  # 错误位置说明行。
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 static import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言静态方法修复成功执行。
        self.assertIn('import static org.hamcrest.Matchers.containsInAnyOrder;', updated)  # 断言 static import 被正确插入文件头部。

    def test_fix_missing_imports_qualifies_junit_assert_method_when_assert_class_is_already_imported(self):  # 验证文件已导入 `org.junit.Assert` 时会优先回退到 `Assert.xxx(...)` 限定符形式。
        source = (  # 构造一个已经导入 JUnit Assert、但补丁使用了裸断言方法的最小 Java 文件。
            'package com.example;\n'  # package 声明行。
            '\n'  # package 后的空行。
            'import org.junit.Assert;\n'  # 显式导入 JUnit Assert 类。
            '\n'  # import 区块后的空行。
            'public class ExampleTest {\n'  # 类声明行。
            '    public void flakyCase() {\n'  # 方法签名行。
            '        assertTrue(true);\n'  # 直接调用缺少限定符的 JUnit 断言方法。
            '    }\n'  # 方法结束行。
            '}\n'  # 类结束行。
        )  # 完成测试源文件构造。
        build_output = (  # 伪造缺失 JUnit 断言方法的 Maven 编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[7,9] cannot find symbol\n'  # 错误位置行。
            '[ERROR]   symbol:   method assertTrue(boolean)\n'  # 缺失方法行。
            '[ERROR]   location: class com.example.ExampleTest\n'  # 错误位置说明行。
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动方法限定符修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言限定符修复成功执行。
        self.assertIn('Assert.assertTrue(true);', updated)  # 断言裸断言方法已被安全改写为 `Assert.assertTrue(...)`。
        self.assertNotIn('import static org.junit.Assert.assertTrue;', updated)  # 断言当前场景优先保持与文件现有风格一致，而不是再额外注入 static import。

    def test_fix_missing_imports_uses_dom_context_for_node_symbol(self):  # 验证仅在明显的 DOM/XML 上下文中才会将缺失的 `Node` 解析为 `org.w3c.dom.Node`。
        source = (  # 构造一个已经明显处于 DOM/XML 语境中的最小 Java 文件。
            'package com.example;\n'  # package 声明行。
            '\n'  # package 后的空行。
            'import javax.xml.parsers.DocumentBuilderFactory;\n'  # 已有 DOM/XML 相关导入。
            'import org.w3c.dom.NamedNodeMap;\n'  # 已有 DOM 相关导入。
            'import org.xml.sax.InputSource;\n'  # 已有 XML 解析相关导入。
            '\n'  # import 区块后的空行。
            'public class ExampleTest {\n'  # 类声明行。
            '    public void flakyCase() {\n'  # 方法签名行。
            '        Node node = null;\n'  # 直接使用缺少 import 的 Node 类型。
            '    }\n'  # 方法结束行。
            '}\n'  # 类结束行。
        )  # 完成测试源文件构造。
        build_output = (  # 伪造缺失 Node 类型的 Maven 编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[9,9] cannot find symbol\n'  # 错误位置行。
            '[ERROR]   symbol:   class Node\n'  # 缺失类名行。
            '[ERROR]   location: class com.example.ExampleTest\n'  # 错误位置说明行。
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言当前上下文足够明确时可以安全完成修复。
        self.assertIn('import org.w3c.dom.Node;', updated)  # 断言已基于 DOM 上下文补入正确的 Node import。

    def test_fix_missing_imports_can_correct_symbol_case_from_repo_import_reference(self):  # 验证仓库内已有 import 线索时会安全地修正类名大小写并补齐 import。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            helper_file = repo_dir / 'src' / 'main' / 'java' / 'com' / 'example' / 'lib' / 'JSONPath.java'  # 构造仓库内真实类名为 JSONPath 的辅助类路径。
            helper_file.parent.mkdir(parents=True)  # 创建辅助类所在目录。
            helper_file.write_text(  # 写入一个最小可识别的 Java 类。
                'package com.example.lib;\n'  # 声明辅助类所在包名。
                '\n'  # 包声明后的空行。
                'public class JSONPath {\n'  # 写入最小类定义。
                '}\n',  # 结束最小类定义。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成辅助类源码写入。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个已经正确导入 JSONPath 的参考测试文件。
            existing_usage_file.parent.mkdir(parents=True, exist_ok=True)  # 确保参考文件所在目录存在。
            existing_usage_file.write_text(  # 写入仓库内已有的正确 import 线索。
                'package com.example.tests;\n'  # 声明参考测试类所在包名。
                '\n'  # package 后的空行。
                'import com.example.lib.JSONPath;\n'  # 显式导入正确大小写的 JSONPath 类型。
                '\n'  # import 区块后的空行。
                'public class ExistingUsage {\n'  # 参考测试类声明行。
                '    JSONPath value;\n'  # 使用已经正确导入的 JSONPath 类型。
                '}\n',  # 参考测试类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成参考测试文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录并允许目录已存在。
            test_file.write_text(  # 写入错误地使用了 JsonPath 大小写的测试类。
                'package com.example.tests;\n'  # 声明测试类所在包名。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        JsonPath value = null;\n'  # 使用了错误大小写的类名。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
                '[ERROR]   symbol:   class JsonPath\n'  # 缺失类名行。
                '[ERROR]   location: class com.example.tests.ExampleTest\n'  # 错误位置说明行。
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动符号大小写修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertTrue(ok, message)  # 断言仓库内已有 import 线索可以被成功复用。
        self.assertIn('import com.example.lib.JSONPath;', updated)  # 断言已补入正确大小写的 import。
        self.assertIn('JSONPath value = null;', updated)  # 断言源码中的错误符号名也被一并修正。

    def test_fix_missing_imports_does_not_replace_lowercase_local_variable_with_type_name(self):  # 验证自动修复不会把局部变量名误改成同名类型。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个仓库内已经导入 List 的参考文件。
            existing_usage_file.parent.mkdir(parents=True)  # 创建参考文件所在目录。
            existing_usage_file.write_text(  # 写入已有的 `java.util.List` import 线索。
                'package com.example.tests;\n'
                '\n'
                'import java.util.List;\n'
                '\n'
                'public class ExistingUsage {\n'
                '    List<String> values;\n'
                '}\n',
                encoding='utf-8',
            )  # 完成参考文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入一个把局部变量 `list` 当作符号缺失的测试类。
                'package com.example.tests;\n'
                '\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        list.stream();\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] cannot find symbol\n'
                '[ERROR]   symbol:   variable list\n'
                '[ERROR]   location: class com.example.tests.ExampleTest\n'
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertFalse(ok)  # 断言当前场景会被安全拒绝，而不是做错误替换。
        self.assertIn('no safe import or symbol fixes available', message.lower())  # 断言失败原因来自保守修复策略。
        self.assertIn('list.stream();', updated)  # 断言局部变量名不会被误改成 `List`。
        self.assertNotIn('List.stream();', updated)  # 断言错误替换不会发生。

    def test_fix_missing_imports_prefers_known_jsonpath_import_over_case_only_project_source(self):  # 验证已知第三方类映射会优先于仅靠文件名大小写接近的项目类。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            helper_file = repo_dir / 'src' / 'main' / 'java' / 'com' / 'example' / 'lib' / 'JSONPath.java'  # 构造仓库内一个仅大小写接近的项目类路径。
            helper_file.parent.mkdir(parents=True)  # 创建辅助类所在目录。
            helper_file.write_text(  # 写入一个最小可识别的 Java 类。
                'package com.example.lib;\n'  # 声明辅助类所在包名。
                '\n'  # 包声明后的空行。
                'public class JSONPath {\n'  # 写入最小类定义。
                '}\n',  # 结束最小类定义。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成辅助类源码写入。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个仓库内已经正确导入 JsonPath 的参考文件。
            existing_usage_file.parent.mkdir(parents=True, exist_ok=True)  # 创建参考文件所在目录。
            existing_usage_file.write_text(  # 写入仓库内已有的第三方 JsonPath import 线索。
                'package com.example.tests;\n'  # 声明参考测试类所在包名。
                '\n'  # package 后的空行。
                'import com.jayway.jsonpath.JsonPath;\n'  # 显式导入第三方 JsonPath。
                '\n'  # import 区块后的空行。
                'public class ExistingUsage {\n'  # 参考测试类声明行。
                '    Object value = JsonPath.class;\n'  # 使用 JsonPath 提供稳定线索。
                '}\n',  # 参考测试类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成参考测试文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录并允许目录已存在。
            test_file.write_text(  # 写入缺少 JsonPath import 的测试类。
                'package com.example.tests;\n'  # 声明测试类所在包名。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        JsonPath value = null;\n'  # 使用了缺少 import 的 JsonPath 类型。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
                '[ERROR]   symbol:   class JsonPath\n'  # 缺失类名行。
                '[ERROR]   location: class com.example.tests.ExampleTest\n'  # 错误位置说明行。
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertTrue(ok, message)  # 断言高置信度第三方映射会触发一次安全修复。
        self.assertIn('import com.jayway.jsonpath.JsonPath;', updated)  # 断言最终选择的是已知第三方 JsonPath import。
        self.assertIn('JsonPath value = null;', updated)  # 断言源码中的符号名保持为原始的 JsonPath。
        self.assertNotIn('JSONPath value = null;', updated)  # 断言不会仅凭项目内大小写接近的类就误改为 JSONPath。

    def test_fix_missing_imports_can_resolve_third_party_symbol_from_repo_existing_import(self):  # 验证仓库内已有第三方 import 线索时可以安全补入同一依赖类型。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个已经正确导入第三方类型的参考测试文件。
            existing_usage_file.parent.mkdir(parents=True)  # 创建参考文件所在目录。
            existing_usage_file.write_text(  # 写入仓库内已有的正确第三方 import 线索。
                'package com.example.tests;\n'  # 声明参考测试类所在包名。
                '\n'  # package 后的空行。
                'import org.json.JSONException;\n'  # 显式导入第三方 JSONException 类型。
                '\n'  # import 区块后的空行。
                'public class ExistingUsage {\n'  # 参考测试类声明行。
                '    JSONException error;\n'  # 使用已经正确导入的第三方类型。
                '}\n',  # 参考测试类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成参考测试文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入缺少 JSONException import 的测试类。
                'package com.example.tests;\n'  # 声明测试类所在包名。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        JSONException error = null;\n'  # 使用缺少 import 的第三方类型。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
                '[ERROR]   symbol:   class JSONException\n'  # 缺失类名行。
                '[ERROR]   location: class com.example.tests.ExampleTest\n'  # 错误位置说明行。
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertTrue(ok, message)  # 断言仓库内已有 import 线索可以被成功复用。
        self.assertIn('import org.json.JSONException;', updated)  # 断言已补入仓库中已有的第三方 import。

    def test_fix_missing_imports_can_resolve_static_import_from_repo_existing_import(self):  # 验证仓库内已有 static import 线索时可以安全补入同一静态方法。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个已经正确导入静态方法的参考测试文件。
            existing_usage_file.parent.mkdir(parents=True)  # 创建参考文件所在目录。
            existing_usage_file.write_text(  # 写入仓库内已有的正确 static import 线索。
                'package com.example.tests;\n'  # 声明参考测试类所在包名。
                '\n'  # package 后的空行。
                'import static org.assertj.core.groups.Tuple.tuple;\n'  # 显式导入正确的 tuple 静态方法。
                '\n'  # import 区块后的空行。
                'public class ExistingUsage {\n'  # 参考测试类声明行。
                '    Object value = tuple("name", "value");\n'  # 使用已经正确导入的静态方法。
                '}\n',  # 参考测试类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成参考测试文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入缺少 tuple static import 的测试类。
                'package com.example.tests;\n'  # 声明测试类所在包名。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        tuple("name", "value");\n'  # 直接调用缺少 static import 的方法。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
                '[ERROR]   symbol:   method tuple(java.lang.String,java.lang.String)\n'  # 缺失方法行。
                '[ERROR]   location: class com.example.tests.ExampleTest\n'  # 错误位置说明行。
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 static import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertTrue(ok, message)  # 断言仓库内已有 static import 线索可以被成功复用。
        self.assertIn('import static org.assertj.core.groups.Tuple.tuple;', updated)  # 断言已补入仓库中已有的 static import。

    def test_fix_missing_imports_prefers_assertj_static_import_for_fluent_assert_that(self):  # 验证 `assertThat(...).startsWith()` 这类链式断言会优先补 AssertJ 的 static import。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于创建目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            existing_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExistingUsage.java'  # 构造一个仓库内已有 AssertJ Assertions import 的参考文件。
            existing_usage_file.parent.mkdir(parents=True)  # 创建参考文件所在目录。
            existing_usage_file.write_text(  # 写入仓库内已有的 AssertJ 类导入线索。
                'package com.example.tests;\n'  # 声明参考测试类所在包名。
                '\n'  # package 后的空行。
                'import org.assertj.core.api.Assertions;\n'  # 显式导入 AssertJ Assertions 类。
                '\n'  # import 区块后的空行。
                'public class ExistingUsage {\n'  # 参考测试类声明行。
                '    Object value = Assertions.assertThat("demo");\n'  # 使用 Assertions 作为限定符，提供稳定的仓库内线索。
                '}\n',  # 参考测试类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成参考文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入一个使用链式 AssertJ 风格断言但缺少 static import 的测试类。
                'package com.example.tests;\n'  # 声明测试类所在包名。
                '\n'  # package 后的空行。
                'public class ExampleTest {\n'  # 类声明行。
                '    public void flakyCase() {\n'  # 方法签名行。
                '        String actual = "services/collector/raw";\n'  # 准备一个简单的字符串变量供链式断言使用。
                '        assertThat(actual).startsWith("services");\n'  # 使用典型的 AssertJ 链式断言但当前没有导入 assertThat。
                '    }\n'  # 方法结束行。
                '}\n',  # 类结束行。
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成测试文件写入。
            build_output = (  # 伪造缺少 `assertThat` 方法的 Maven 编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[6,9] cannot find symbol\n'  # 错误位置行。
                '[ERROR]   symbol:   method assertThat(java.lang.String)\n'  # 缺失方法行。
                '[ERROR]   location: class com.example.tests.ExampleTest\n'  # 错误位置说明行。
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 static import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言当前修复场景可以被成功识别和处理。
        self.assertIn('import static org.assertj.core.api.Assertions.assertThat;', updated)  # 断言最终补入的是 AssertJ 的 static import 而不是 JUnit Assert。
        self.assertNotIn('Assert.assertThat', updated)  # 断言源码中不会被错误改写为 JUnit Assert 的限定符调用。

    def test_fix_missing_imports_prefers_assertj_static_import_when_repo_depends_on_assertj(self):  # 验证即便仓库里同时存在 JUnit Jupiter Assertions，只要断言形态明显是 AssertJ 且 pom 依赖了 assertj-core，也会补入正确的 static import。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text(  # 写入一个只包含 assertj-core 依赖的最小 pom。
                '<project><dependencies><dependency><groupId>org.assertj</groupId><artifactId>assertj-core</artifactId></dependency></dependencies></project>',
                encoding='utf-8',
            )  # 完成最小 pom 写入。
            junit_usage_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'JupiterUsage.java'  # 构造一个仓库内已有 JUnit Jupiter Assertions 导入的干扰文件。
            junit_usage_file.parent.mkdir(parents=True)  # 创建干扰文件所在目录。
            junit_usage_file.write_text(  # 写入 Jupiter Assertions 导入，模拟与 AssertJ 并存的真实仓库。
                'package com.example.tests;\n\n'
                'import org.junit.jupiter.api.Assertions;\n\n'
                'public class JupiterUsage {\n'
                '    Object value = Assertions.class;\n'
                '}\n',
                encoding='utf-8',
            )  # 完成干扰文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入一个使用 Druid 同类 AssertJ 链式断言但缺少 static import 的测试类。
                'package com.example.tests;\n\n'
                'import java.util.Arrays;\n'
                'import java.util.LinkedHashSet;\n'
                'import java.util.Set;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        Set<String> actual = new LinkedHashSet<>(Arrays.asList("id", "name"));\n'
                '        Set<String> expected = new LinkedHashSet<>(Arrays.asList("id", "name"));\n'
                '        assertThat(actual).containsExactlyInAnyOrderElementsOf(expected);\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            build_output = (  # 伪造缺少 `assertThat` 方法的 Maven 编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[9,9] cannot find symbol\n'
                '[ERROR]   symbol:   method assertThat(java.util.Set<java.lang.String>)\n'
                '[ERROR]   location: class com.example.tests.ExampleTest\n'
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 static import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言当前修复场景可以被成功识别和处理。
        self.assertIn('import static org.assertj.core.api.Assertions.assertThat;', updated)  # 断言最终补入的是 AssertJ 的 static import。
        self.assertNotIn('org.junit.Assert.assertThat', updated)  # 断言不会错误回退到 JUnit 风格的 assertThat。

    def test_fix_missing_imports_can_resolve_gson_json_parser_from_context(self):  # 验证 `JsonParser` 在 Gson 语境中会被保守地解析到 `com.google.gson.JsonParser`。
        source = (  # 构造一个明显使用 Gson 语义但缺少 JsonParser import 的最小 Java 文件。
            'package com.example;\n\n'
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        JsonParser.parseString("{\\"name\\":\\"demo\\"}").getAsJsonObject();\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        build_output = (  # 伪造 Maven 中文编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] 找不到符号\n'
            '[ERROR]   符号:   变量 JsonParser\n'
            '[ERROR]   位置: 类 com.example.ExampleTest\n'
        )  # 完成编译错误输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            repo_dir = Path(tmp_dir)  # 包装临时目录路径。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            test_file = repo_dir / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言当前上下文足够明确时可以成功修复。
        self.assertIn('import com.google.gson.JsonParser;', updated)  # 断言最终补入的是 Gson 的 JsonParser 导入。

    def test_fix_missing_imports_does_not_add_jsonunit_static_import_without_repo_evidence(self):  # 验证对于 `assertThatJson` 这类高风险 helper，不会在缺少仓库线索时盲目补三方 static import。
        source = (  # 构造一个直接调用 `assertThatJson` 但仓库内没有任何 json-unit 线索的最小 Java 文件。
            'package com.example;\n'  # package 声明行。
            '\n'  # package 后的空行。
            'public class ExampleTest {\n'  # 类声明行。
            '    public void flakyCase() {\n'  # 方法签名行。
            '        assertThatJson("{\\"a\\":1}");\n'  # 使用高风险 helper 但当前既无导入也无仓库线索。
            '    }\n'  # 方法结束行。
            '}\n'  # 类结束行。
        )  # 完成测试源文件构造。
        build_output = (  # 伪造缺少 `assertThatJson` 方法的编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] cannot find symbol\n'  # 错误位置行。
            '[ERROR]   symbol:   method assertThatJson(java.lang.String)\n'  # 缺失方法行。
            '[ERROR]   location: class com.example.ExampleTest\n'  # 错误位置说明行。
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertFalse(ok)  # 断言当前高风险 helper 不会被盲目修复。
        self.assertIn('no safe import', message.lower())  # 断言失败原因来自“没有安全修复方案”。
        self.assertNotIn('net.javacrumbs.jsonunit.assertj.JsonAssertions.assertThatJson', updated)  # 断言不会盲目写入 json-unit 的 static import。

    def test_find_reference_context_candidates_only_keeps_matching_generated_patch(self):  # 验证离线参考分析只会保留与当前 generated_patch 足够接近的成功上下文。
        entry = _make_entry('public void flakyCase() {}', 'public void flakyCase() {\n    assertEquals(1, 1);\n}')  # 构造一个最小测试条目，并让当前被评估补丁等于安全候选。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时参考补丁根目录。
            reference_root = Path(tmp_dir)  # 将字符串路径包装成 Path 便于构造目录结构。
            risky_patch = reference_root / 'gpt' / 'gpt1' / 'GoodPatches' / entry.project_name / entry.original_sha / 'module' / f'{entry.test_class}.{entry.test_method}' / '1.patch'  # 构造一个需要额外 pom 依赖的候选路径。
            risky_patch.parent.mkdir(parents=True)  # 创建风险候选所在目录。
            risky_patch.write_text(  # 写入一个依赖额外 pom 的高风险候选。
                'Patch:\n\n'
                'test_code:\n'
                'public void flakyCase() {\n'
                '    assertThatJson("{}");\n'
                '}\n\n'
                'import:\n'
                'import static net.javacrumbs.jsonunit.assertj.JsonAssertions.assertThatJson;\n\n'
                'pom:\n'
                '<dependency>\n'
                '  <groupId>net.javacrumbs</groupId>\n'
                '</dependency>\n',
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成风险候选写入。
            safe_patch = reference_root / 'gpt' / 'gpt1' / 'all_rounds' / entry.project_name / entry.original_sha / 'module' / f'{entry.test_class}.{entry.test_method}' / '2.patch'  # 构造一个与当前 generated_patch 相匹配的候选路径。
            safe_patch.parent.mkdir(parents=True, exist_ok=True)  # 创建安全候选所在目录。
            safe_patch.write_text(  # 写入一个无需额外依赖的简单候选。
                'ROUND 1:\n\n'
                'Before stitching:\n\n'
                'test_code:\n\n'
                'public void flakyCase() {\n'
                '    assertEquals(1, 1);\n'
                '}\n\n'
                'import:\n\n'
                '[]\n\n'
                'pom:\n\n'
                'None\n',
                encoding='utf-8',  # 使用 UTF-8 编码写入文件。
            )  # 完成安全候选写入。
            candidates = find_reference_context_candidates(entry, reference_root=str(reference_root), similarity_threshold=0.85)  # 离线分析时只保留和当前 generated_patch 足够相似的上下文候选。
        self.assertEqual(len(candidates), 1)  # 断言只有与当前补丁匹配的候选会被离线归因保留。
        self.assertIn('assertEquals(1, 1);', candidates[0].test_code)  # 断言最终保留下来的就是与当前 generated_patch 相匹配的成功候选。

    def test_find_reference_patch_candidates_merges_context_for_duplicate_code(self):  # 验证相同 test_code 的多个参考来源会合并 import 与 pom，而不是被去重时丢掉上下文。
        entry = _make_entry('public void flakyCase() {}', 'public void flakyCase() {}')  # 构造一个最小测试条目，复用其中的项目、提交和测试键。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时参考补丁根目录。
            reference_root = Path(tmp_dir)  # 将根目录包装成 Path 便于创建结构。
            plain_patch = reference_root / 'gpt' / 'gpt1' / 'all_rounds' / entry.project_name / entry.original_sha / 'module' / f'{entry.test_class}.{entry.test_method}' / '1.patch'  # 构造只有 test_code 的普通候选。
            plain_patch.parent.mkdir(parents=True)  # 创建普通候选目录。
            plain_patch.write_text(  # 写入一个不带 import 与 pom 的简单候选。
                'Patch:\n\n'
                'test_code:\n'
                'public void flakyCase() {\n'
                '    JsonPath.read("{}");\n'
                '}\n\n'
                'import:\n'
                '[]\n\n'
                'pom:\n'
                'None\n',
                encoding='utf-8',
            )  # 完成普通候选写入。
            rich_patch = reference_root / 'gpt' / 'gpt2' / 'GoodPatches' / entry.project_name / entry.original_sha / 'module' / f'{entry.test_class}.{entry.test_method}' / '2.patch'  # 构造同代码但带完整上下文的候选。
            rich_patch.parent.mkdir(parents=True, exist_ok=True)  # 创建富上下文候选目录。
            rich_patch.write_text(  # 写入和上面相同代码，但额外附带 import 与 pom。
                'Patch:\n\n'
                'test_code:\n'
                'public void flakyCase() {\n'
                '    JsonPath.read("{}");\n'
                '}\n\n'
                'import:\n'
                "['import com.jayway.jsonpath.JsonPath;']\n\n"
                'pom:\n'
                '<dependency>\n'
                '  <groupId>com.jayway.jsonpath</groupId>\n'
                '  <artifactId>json-path</artifactId>\n'
                '</dependency>\n',
                encoding='utf-8',
            )  # 完成富上下文候选写入。
            candidates = find_reference_patch_candidates(entry, reference_root=str(reference_root))  # 执行参考补丁检索。
        self.assertEqual(len(candidates), 1)  # 断言相同代码最终会被折叠成一个候选。
        self.assertIn('import com.jayway.jsonpath.JsonPath;', candidates[0].imports)  # 断言合并后的候选仍然保留了 import 上下文。
        self.assertIn('json-path', candidates[0].pom_snippet)  # 断言合并后的候选也保留了 pom 依赖上下文。

    def test_find_reference_patch_candidates_only_reads_reference_patch_tree(self):  # 验证离线参考候选只来自参考补丁目录本身，不混入 ground truth 或其他数据集代码。
        entry = _make_entry('public void flakyCase() {}', 'public void flakyCase() { assertEquals(0, 0); }')  # 构造一个最小测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离目录存放临时参考源。
            reference_root = Path(tmp_dir) / 'patches'  # 构造临时参考补丁根目录。
            reference_root.mkdir(parents=True)  # 创建空目录，确保当前测试只依赖我们手写的数据。
            patch_path = reference_root / 'gpt' / 'gpt1' / 'GoodPatches' / entry.project_name / entry.original_sha / 'module' / f'{entry.test_class}.{entry.test_method}' / '1.patch'  # 构造一个真正来自参考补丁库的候选。
            patch_path.parent.mkdir(parents=True, exist_ok=True)  # 创建参考补丁目录。
            patch_path.write_text(  # 写入一个最小成功参考补丁。
                'Patch:\n\n'
                'test_code:\n'
                'public void flakyCase() {\n'
                '    assertEquals(2, 2);\n'
                '}\n\n'
                'import:\n'
                '[]\n\n'
                'pom:\n'
                'None\n',
                encoding='utf-8',
            )  # 完成参考补丁写入。
            candidates = find_reference_patch_candidates(entry, reference_root=str(reference_root))  # 执行候选检索。
        self.assertEqual(len(candidates), 1)  # 断言离线分析只会返回参考补丁目录中的候选。
        self.assertIn('GoodPatches', candidates[0].source_path)  # 断言返回的候选来自参考补丁库目录本身。

    def test_apply_generated_patch_context_infers_imports_and_dependency_from_generated_patch(self):  # 验证真实运行路径只靠原始 generated_patch 自身也能补出 import 与 pom 上下文。
        entry = _make_entry('public void flakyCase() {}', 'public void flakyCase() {\n    DocumentContext parsed = JsonPath.parse("{}");\n}\n')  # 构造一个正文里显式使用 json-path API 的最小补丁。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个还没有 json-path import 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        DocumentContext parsed = JsonPath.parse("{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 只根据原始 generated_patch 自身推断上下文。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取更新后的 pom。
        self.assertTrue(ok, message)  # 断言上下文推断成功。
        self.assertIn('import com.jayway.jsonpath.JsonPath;', updated_test)  # 断言会补出 JsonPath import。
        self.assertIn('import com.jayway.jsonpath.DocumentContext;', updated_test)  # 断言会补出 DocumentContext import。
        self.assertIn('<artifactId>json-path</artifactId>', updated_pom)  # 断言会同步补入 json-path 依赖。

    def test_apply_reference_patch_context_can_add_imports_and_dependency(self):  # 验证参考补丁回退会同步把 import 与 pom 依赖落到工作区。
        entry = _make_entry('public void flakyCase() {}', 'public void flakyCase() {}')  # 构造一个最小测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入最小 Java 测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        JsonPath.read("{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            candidate = type('Candidate', (), {  # 构造一个最小参考补丁候选对象。
                'imports': ('import com.jayway.jsonpath.JsonPath;',),
                'pom_snippet': '<dependency>\n  <groupId>com.jayway.jsonpath</groupId>\n  <artifactId>json-path</artifactId>\n</dependency>',
            })()  # 当前测试只关心上下文注入，不依赖完整 dataclass。
            ok, message = apply_reference_patch_context(str(repo_dir), entry, str(test_file), candidate)  # 执行参考补丁上下文注入。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取更新后的 pom。
        self.assertTrue(ok, message)  # 断言上下文注入成功。
        self.assertIn('import com.jayway.jsonpath.JsonPath;', updated_test)  # 断言参考补丁 import 已被写入测试文件。
        self.assertIn('<artifactId>json-path</artifactId>', updated_pom)  # 断言参考补丁依赖已被写入 pom。

    def test_apply_reference_patch_context_can_infer_imports_and_dependency_from_test_code(self):  # 验证即使参考补丁缺少显式 import/pom，工具也能从补丁代码正文推断上下文。
        entry = _make_entry('public void flakyCase() {}', 'public void flakyCase() {}')  # 构造一个最小测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入最小 Java 测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        JsonPath.parse("{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            candidate = type('Candidate', (), {  # 构造一个缺少显式上下文的参考补丁候选对象。
                'test_code': 'public void flakyCase() {\n    DocumentContext parsed = JsonPath.parse("{}");\n}\n',
                'imports': (),
                'pom_snippet': 'None',
            })()  # 当前测试专门验证正文推断逻辑。
            ok, message = apply_reference_patch_context(str(repo_dir), entry, str(test_file), candidate)  # 执行参考补丁上下文注入。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取更新后的 pom。
        self.assertTrue(ok, message)  # 断言上下文注入成功。
        self.assertIn('import com.jayway.jsonpath.JsonPath;', updated_test)  # 断言工具会从补丁正文推断出 JsonPath import。
        self.assertIn('import com.jayway.jsonpath.DocumentContext;', updated_test)  # 断言工具会从补丁正文推断出 DocumentContext import。
        self.assertIn('<artifactId>json-path</artifactId>', updated_pom)  # 断言工具会同步补入 json-path 依赖。

    def test_apply_reference_patch_context_can_infer_dependency_from_original_flaky_signature(self):  # 验证当参考候选保留原方法声明里的异常类型时，工具会从原始 flaky 方法补回 import 与依赖。
        entry = _make_entry(  # 构造一个原始方法声明中包含 JSONException 的最小测试条目。
            'public void flakyCase() throws JSONException {\n'
            '    assertEquals(expected, actual);\n'
            '}\n',
            'public void flakyCase() {\n'
            '    assertEquals(expected, actual);\n'
            '}\n',
        )  # 当前测试专门覆盖 pulsar 一类“声明保留但候选正文没显式写异常”的场景。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入最小 Java 测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() throws JSONException {\n'
                '        assertEquals(expected, actual);\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            candidate = type('Candidate', (), {  # 构造一个正文里没有 JSONException，但会复用原始声明的参考候选。
                'test_code': 'public void flakyCase() {\n    assertEquals(expected, actual);\n}\n',
                'imports': (),
                'pom_snippet': 'None',
            })()  # 当前测试只关心原始方法声明带来的上下文推断。
            ok, message = apply_reference_patch_context(str(repo_dir), entry, str(test_file), candidate)  # 执行参考补丁上下文注入。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取更新后的 pom。
        self.assertTrue(ok, message)  # 断言上下文注入成功。
        self.assertIn('import org.json.JSONException;', updated_test)  # 断言工具会从原始方法声明补出 JSONException import。
        self.assertIn('<artifactId>json</artifactId>', updated_pom)  # 断言工具会同步补入 org.json 依赖。

    def test_fix_unreported_exception_declaration_appends_checked_exception(self):  # 验证构建日志里的 checked exception 会被补到目标方法声明中。
        source = (  # 构造一个最小 Java 测试类。
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        parser.parse("{}");\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        build_output = (  # 伪造中文 javac 的 checked exception 错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] 未报告的异常错误net.minidev.json.parser.ParseException; 必须对其进行捕获或声明以便抛出\n'
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_unreported_exception_declaration(str(test_file), 'flakyCase', build_output)  # 执行 checked exception 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言修复成功。
        self.assertIn('throws net.minidev.json.parser.ParseException', updated)  # 断言方法声明已追加正确的异常全限定名。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
