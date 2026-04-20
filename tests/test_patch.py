import tempfile  # 导入临时目录工具用于构造隔离测试仓库。
import unittest  # 导入标准库测试框架。
from pathlib import Path  # 导入路径工具简化测试文件创建。
from unittest import mock  # 导入 mock 以便隔离 fixed_sha Git 读取逻辑。

from rerun_tool.data import TestEntry  # 导入数据结构构造最小测试样本。
from rerun_tool.patch import (ReferencePatchCandidate, apply_generated_patch_context, apply_patch, apply_reference_patch_context,  # 导入待测的补丁应用与上下文推断函数。
                              backport_fixed_sha_test_helpers, fix_missing_imports, fix_related_test_imports,  # 导入 fixed_sha helper 回溯与保守源码修复函数。
                              fix_unreported_exception_declaration, _resolve_contextual_symbol_reference,
                              _resolve_missing_method_reference)  # 导入保守源码修复函数与少量上下文解析 helper。
from rerun_tool.reference_analysis import find_reference_context_candidates, find_reference_patch_candidates  # 导入仅供离线分析使用的参考候选检索函数。


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

    def test_resolve_missing_method_reference_prefers_mockito_at_least_once(self):  # 验证 Mockito verify 场景的 `atLeastOnce()` 会被解析到正确的 static import。
        source = (
            'package com.example;\n'
            '\n'
            'import static org.mockito.Mockito.verify;\n'
            '\n'
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        verify(mock, atLeastOnce()).close();\n'
            '    }\n'
            '}\n'
        )  # 构造一个缺少 `atLeastOnce` static import 的最小测试文件。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            resolved_import = _resolve_missing_method_reference(str(test_file), 'atLeastOnce', source)  # 执行静态方法来源解析。
        self.assertEqual(resolved_import, 'org.mockito.Mockito.atLeastOnce')  # 断言当前会稳定绑定到 Mockito。

    def test_resolve_contextual_symbol_reference_prefers_commons_collection_utils_for_is_equal_collection(self):  # 验证 `CollectionUtils.isEqualCollection(...)` 会优先绑定到 commons-collections4。
        source = (
            'package com.example;\n'
            '\n'
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        CollectionUtils.isEqualCollection(left, right);\n'
            '    }\n'
            '}\n'
        )  # 构造一个只保留歧义调用点的最小测试文件。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            resolved_symbol = _resolve_contextual_symbol_reference(str(test_file), 'CollectionUtils')  # 执行带上下文的符号解析。
        self.assertEqual(resolved_symbol, ('CollectionUtils', 'org.apache.commons.collections4.CollectionUtils'))  # 断言当前会优先解析到真正拥有 `isEqualCollection` 的 commons-collections4。

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

    def test_fix_missing_imports_prefers_contextual_jsonpath_over_project_jsonpath_case_repair(self):  # 验证当前文件已经明显处于 jayway json-path 语境时，不会再把 `JsonPath` 误修成仓库内的 `JSONPath`。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            helper_file = repo_dir / 'src' / 'main' / 'java' / 'com' / 'example' / 'lib' / 'JSONPath.java'  # 构造一个仅大小写接近的项目内类。
            helper_file.parent.mkdir(parents=True)  # 创建辅助类所在目录。
            helper_file.write_text(  # 写入最小可识别的仓库内 JSONPath 类。
                'package com.example.lib;\n'
                '\n'
                'public class JSONPath {}\n',
                encoding='utf-8',
            )  # 完成辅助类源码写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入一个已经明显处于 jayway json-path 语境中的测试类。
                'package com.example.tests;\n'
                '\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        ReadContext context = JsonPath.parse("{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            build_output = (  # 伪造当前测试文件缺失 `JsonPath` 的编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[5,31] cannot find symbol\n'
                '[ERROR]   symbol:   class JsonPath\n'
                '[ERROR]   location: class com.example.tests.ExampleTest\n'
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的测试文件内容。
        self.assertTrue(ok, message)  # 断言当前文件语境足够明确时修复成功。
        self.assertIn('import com.jayway.jsonpath.JsonPath;', updated)  # 断言仍然会补入 jayway 的 `JsonPath`。
        self.assertIn('ReadContext context = JsonPath.parse("{}");', updated)  # 断言源码中的类型名不会被误改成 `JSONPath`。
        self.assertNotIn('JSONPath.parse("{}")', updated)  # 断言不会再发生跨库大小写误修。

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

    def test_fix_missing_imports_can_detect_assertj_assert_that_with_nested_parentheses(self):  # 验证 `assertThat(foo.bar())` 这类带嵌套括号的链式断言仍会补入 AssertJ static import。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text(  # 写入一个只包含 assertj-core 依赖的最小 pom。
                '<project><dependencies><dependency><groupId>org.assertj</groupId><artifactId>assertj-core</artifactId></dependency></dependencies></project>',
                encoding='utf-8',
            )  # 完成最小 pom 写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入一个使用 cloud-slang 同类链式断言但缺少 static import 的测试类。
                'package com.example.tests;\n\n'
                'import java.util.LinkedHashMap;\n'
                'import java.util.Map;\n'
                'import java.util.Set;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        Map<String, String> values = new LinkedHashMap<>();\n'
                '        Set<String> expected = values.keySet();\n'
                '        assertThat(values.keySet()).containsExactlyInAnyOrderElementsOf(expected);\n'
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
        self.assertIn('import static org.assertj.core.api.Assertions.assertThat;', updated)  # 断言嵌套括号不会再阻断 AssertJ static import 推断。

    def test_fix_missing_imports_can_resolve_assertj_entry_helper(self):  # 验证 `entry(...)` 会在仓库已经依赖 AssertJ 时补入正确的 static import。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时仓库目录。
            repo_dir = Path(tmp_dir)  # 将字符串路径包装为 Path 便于构造目录树。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text(  # 写入一个只包含 assertj-core 依赖的最小 pom。
                '<project><dependencies><dependency><groupId>org.assertj</groupId><artifactId>assertj-core</artifactId></dependency></dependencies></project>',
                encoding='utf-8',
            )  # 完成最小 pom 写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'tests' / 'ExampleTest.java'  # 构造待修复测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件所在目录。
            test_file.write_text(  # 写入一个使用 `entry(...)` 但缺少 static import 的测试类。
                'package com.example.tests;\n\n'
                'import java.util.Map;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertThat(Map.of("id", 1)).containsOnly(entry("id", 1));\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            build_output = (  # 伪造缺少 `entry` 方法的 Maven 编译错误输出。
                '[ERROR] /workspace/src/test/java/com/example/tests/ExampleTest.java:[6,9] cannot find symbol\n'
                '[ERROR]   symbol:   method entry(java.lang.String,int)\n'
                '[ERROR]   location: class com.example.tests.ExampleTest\n'
            )  # 完成编译输出构造。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 static import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言当前修复场景可以被成功识别和处理。
        self.assertIn('import static org.assertj.core.api.Assertions.entry;', updated)  # 断言最终补入的是 AssertJ 的 entry helper。

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

    def test_fix_missing_imports_does_not_rewrite_jsonobject_to_gson_jsonobject(self):  # 验证 `JSONObject->JsonObject` 这类带缩写语义差异的替换会被拦住。
        source = (  # 构造一个缺少 `JSONObject` 导入的最小 Java 文件。
            'package com.example;\n\n'
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        JSONObject value = new JSONObject("{}");\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        build_output = (  # 伪造缺少 `JSONObject` 类的编译错误输出。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] cannot find symbol\n'
            '[ERROR]   symbol:   class JSONObject\n'
            '[ERROR]   location: class com.example.ExampleTest\n'
        )  # 完成编译错误输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离的临时目录。
            repo_dir = Path(tmp_dir)  # 包装临时目录路径。
            (repo_dir / '.git').mkdir()  # 创建空的 .git 目录作为仓库根标记。
            helper_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExistingUsage.java'  # 构造一个仓库内已有 Gson `JsonObject` 导入的参考文件。
            helper_file.parent.mkdir(parents=True)  # 创建参考文件所在目录。
            helper_file.write_text(  # 写入仓库里会误导大小写修正的导入线索。
                'package com.example;\n\n'
                'import com.google.gson.JsonObject;\n\n'
                'public class ExistingUsage {\n'
                '    JsonObject value;\n'
                '}\n',
                encoding='utf-8',
            )  # 完成参考文件写入。
            test_file = repo_dir / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_missing_imports(str(test_file), build_output)  # 执行自动 import 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 当前场景现在可以安全补成 `org.json.JSONObject`，不需要再放弃修复。
        self.assertIn('import org.json.JSONObject;', updated)  # 断言会优先补入正确的 `org.json` 导入。
        self.assertIn('JSONObject value = new JSONObject("{}");', updated)  # 断言工具不会再把 `JSONObject` 错误改成 `JsonObject`。
        self.assertNotIn('JsonObject value = new JsonObject("{}");', updated)  # 断言危险的 Gson 大小写替换不会发生。

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

    def test_apply_generated_patch_context_can_infer_guava_imports_and_dependency(self):  # 验证 generated_patch 出现 Guava 集合工厂类时会补入 import 和 pom 依赖。
        entry = _make_entry(  # 构造一个明显依赖 Guava 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    ImmutableList<String> values = ImmutableList.of("a", "b");\n'
            '    assertEquals(2, values.size());\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个还没有 Guava import 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        ImmutableList<String> values = ImmutableList.of("a", "b");\n'
                '        assertEquals(2, values.size());\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取更新后的 pom。
        self.assertTrue(ok, message)  # 断言 Guava 上下文增强成功。
        self.assertIn('import com.google.common.collect.ImmutableList;', updated_test)  # 断言补入了 Guava 类型 import。
        self.assertIn('<artifactId>guava</artifactId>', updated_pom)  # 断言补入了 Guava 依赖。

    def test_apply_generated_patch_context_can_infer_typesafe_config_imports_and_dependency(self):  # 验证 Config 只有在 typesafe 语境明确时才会补入 import 和依赖。
        entry = _make_entry(  # 构造一个同时使用 Config 和 ConfigFactory 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    Config parsed = ConfigFactory.parseString("demo.enabled=true");\n'
            '    assertNotNull(parsed);\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个还没有 typesafe-config import 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        Config parsed = ConfigFactory.parseString("demo.enabled=true");\n'
                '        assertNotNull(parsed);\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取更新后的 pom。
        self.assertTrue(ok, message)  # 断言 typesafe-config 上下文增强成功。
        self.assertIn('import com.typesafe.config.Config;', updated_test)  # 断言 `Config` 会在 typesafe 语境里被正确导入。
        self.assertIn('import com.typesafe.config.ConfigFactory;', updated_test)  # 断言 `ConfigFactory` 也会一并补齐。
        self.assertIn('<artifactId>config</artifactId>', updated_pom)  # 断言补入了 typesafe-config 依赖。

    def test_apply_generated_patch_context_prefers_jsonunit_option_when_ignoring_array_order_is_used(self):  # 验证 `IGNORING_ARRAY_ORDER` 会绑定到 json-unit 的 Option，而不是 json-path 的 Option。
        entry = _make_entry(  # 构造一个明显属于 json-unit 语境的 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    assertThatJson("[]").when(Option.IGNORING_ARRAY_ORDER);\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个还没有 json-unit import 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertThatJson("[]").when(Option.IGNORING_ARRAY_ORDER);\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
        self.assertTrue(ok, message)  # 断言 json-unit 上下文增强成功。
        self.assertIn('import net.javacrumbs.jsonunit.core.Option;', updated_test)  # 断言补入的是 json-unit 的 Option。
        self.assertNotIn('import com.jayway.jsonpath.Option;', updated_test)  # 断言不会误补成 json-path 的 Option。

    def test_apply_generated_patch_context_can_infer_assert_that_and_catch_exception_context(self):  # 验证 generated_patch 直接出现 assertThat/when/then/caughtException 时，会补齐对应 static import 与依赖。
        entry = _make_entry(  # 构造一个同时使用 catch-exception 和 AssertJ BDD 断言的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    when(() -> service.call());\n'
            '    then(caughtException()).isNotNull();\n'
            '    assertThat("demo").startsWith("de");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            witness = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'WitnessTest.java'  # 构造一个提供 AssertJ 线索的仓库内测试文件。
            witness.parent.mkdir(parents=True)  # 创建 witness 文件目录。
            witness.write_text(  # 写入一个已经使用 AssertJ assertThat 的最小测试类。
                'package com.example;\n\n'
                'import static org.assertj.core.api.Assertions.assertThat;\n\n'
                'public class WitnessTest {\n'
                '    public void witness() {\n'
                '        assertThat("demo").startsWith("de");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成 witness 文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.write_text(  # 写入一个尚未补齐上下文的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    private Service service;\n'
                '    public void flakyCase() {\n'
                '        when(() -> service.call());\n'
                '        then(caughtException()).isNotNull();\n'
                '        assertThat("demo").startsWith("de");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取增强后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取增强后的 pom。
        self.assertTrue(ok, message)  # 断言上下文增强成功。
        self.assertIn('import static com.googlecode.catchexception.CatchException.when;', updated_test)  # 断言补入 catch-exception 的 `when` 静态导入。
        self.assertIn('import static com.googlecode.catchexception.CatchException.caughtException;', updated_test)  # 断言补入 catch-exception 的 `caughtException` 静态导入。
        self.assertIn('import static org.assertj.core.api.Assertions.then;', updated_test)  # 断言补入 AssertJ BDD 风格的 `then` 静态导入。
        self.assertIn('import static org.assertj.core.api.Assertions.assertThat;', updated_test)  # 断言补入 `assertThat` 静态导入。
        self.assertIn('<artifactId>catch-exception</artifactId>', updated_pom)  # 断言补入了 catch-exception 依赖。
        self.assertIn('<artifactId>assertj-core</artifactId>', updated_pom)  # 断言补入了 AssertJ 依赖。

    def test_apply_generated_patch_context_promotes_guava_to_compile_scope_when_main_source_uses_it(self):  # 验证当主源码本身也依赖 Guava 时，工具会把 guava 依赖升级为 compile scope。
        entry = _make_entry(  # 构造一个会在 generated_patch 中用到 Guava 集合工厂的最小条目。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    ImmutableList<String> values = ImmutableList.of("a");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            main_file = repo_dir / 'src' / 'main' / 'java' / 'com' / 'example' / 'App.java'  # 构造一个明确使用 Guava 的 main source 文件。
            main_file.parent.mkdir(parents=True)  # 创建主源码目录。
            main_file.write_text(  # 写入一个包含 `com.google.common` 包引用的最小主源码文件。
                'package com.example;\n\n'
                'import com.google.common.collect.ImmutableList;\n\n'
                'public class App {\n'
                '    ImmutableList<String> values = ImmutableList.of("a");\n'
                '}\n',
                encoding='utf-8',
            )  # 完成主源码文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.parent.mkdir(parents=True, exist_ok=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个缺少 Guava import 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        ImmutableList<String> values = ImmutableList.of("a");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取增强后的 pom。
        self.assertTrue(ok, message)  # 断言上下文增强成功。
        self.assertIn('<artifactId>guava</artifactId>', updated_pom)  # 断言补入了 Guava 依赖。
        self.assertIn('<scope>compile</scope>', updated_pom)  # 断言 Guava 已经被升级为 compile scope。

    def test_apply_reference_patch_context_dedupes_conflicting_simple_name_imports(self):  # 验证同一批次里出现两个同名不同库的 import 时，只会保留与代码语义一致的那一个。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个明显属于 Gson 语境的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        JsonParser.parseString("{}").getAsJsonObject();\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            candidate = ReferencePatchCandidate(  # 构造一个同时带入 Gson 和 Jackson `JsonParser` import 的上下文候选。
                source_path='fixed_sha:src/test/java/com/example/ExampleTest.java',
                test_code='JsonParser.parseString("{}").getAsJsonObject();',
                imports=('import com.google.gson.JsonParser;', 'import com.fasterxml.jackson.core.JsonParser;'),
            )  # 完成冲突上下文候选构造。
            entry = _make_entry('public void flakyCase() {}\n', 'public void flakyCase() {}\n')  # 构造最小测试条目。
            ok, message = apply_reference_patch_context(str(repo_dir), entry, str(test_file), candidate)  # 执行 import 上下文应用。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取更新后的测试文件。
        self.assertTrue(ok, message)  # 断言上下文应用成功。
        self.assertIn('import com.google.gson.JsonParser;', updated_test)  # 断言最终保留了 Gson 的 `JsonParser` 导入。
        self.assertNotIn('import com.fasterxml.jackson.core.JsonParser;', updated_test)  # 断言冲突的 Jackson `JsonParser` 不会再被插入。

    def test_backport_fixed_sha_test_helpers_can_append_helper_and_context(self):  # 验证 fixed_sha 中的测试 helper 会被迁回 original_sha，并同步补入依赖上下文。
        entry = _make_entry(  # 构造一个会调用 fixed_sha helper 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    assertJsonEqualsNonStrict("{}", "{}");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入 original_sha 工作区里的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertJsonEqualsNonStrict("{}", "{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            fixed_sha_content = (  # 伪造 fixed_sha 同路径文件内容，其中已经包含真正的 helper 方法和所需 import。
                'package com.example;\n\n'
                'import org.skyscreamer.jsonassert.JSONAssert;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertJsonEqualsNonStrict("{}", "{}");\n'
                '    }\n\n'
                '    private void assertJsonEqualsNonStrict(String expected, String actual) throws Exception {\n'
                '        JSONAssert.assertEquals(expected, actual, false);\n'
                '    }\n'
                '}\n'
            )  # 完成 fixed_sha 文件内容构造。
            build_output = (  # 伪造当前测试文件缺失 helper 方法的编译错误。
                '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] cannot find symbol\n'
                '[ERROR]   symbol:   method assertJsonEqualsNonStrict(java.lang.String,java.lang.String)\n'
                '[ERROR]   location: class com.example.ExampleTest\n'
            )  # 完成编译输出构造。
            with mock.patch('rerun_tool.patch.ensure_revision_available', return_value=(True, 'ready')), mock.patch('rerun_tool.patch.list_files_at_revision', return_value=(True, ['src/test/java/com/example/ExampleTest.java'])), mock.patch('rerun_tool.patch.read_file_at_revision', return_value=(True, fixed_sha_content)):  # 拦截 fixed_sha Git 读取逻辑，只验证 helper 迁移流程本身。
                ok, message = backport_fixed_sha_test_helpers(str(repo_dir), entry, str(test_file), build_output)  # 执行 fixed_sha helper 回溯。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取迁回 helper 后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取同步更新后的 pom。
        self.assertTrue(ok, message)  # 断言 fixed_sha helper 回溯成功。
        self.assertIn('private void assertJsonEqualsNonStrict', updated_test)  # 断言 helper 方法已经被追加回当前测试类。
        self.assertIn('import org.skyscreamer.jsonassert.JSONAssert;', updated_test)  # 断言 helper 依赖的 import 也被一并补回。
        self.assertIn('<artifactId>jsonassert</artifactId>', updated_pom)  # 断言 helper 需要的测试依赖被同步补入 pom。
        self.assertIn('Backported fixed_sha context', message)  # 断言返回消息会保留 fixed_sha 上下文来源摘要。

    def test_backport_fixed_sha_test_helpers_can_append_field_and_filter_imports(self):  # 验证 fixed_sha 字段回补只会迁回当前字段真正依赖的 import，不会整文件 import 全搬回来。
        entry = _make_entry(  # 构造一个会调用 fixed_sha 常量字段的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    JSON_MAPPER.writeValueAsString("demo");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入 original_sha 工作区里的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        JSON_MAPPER.writeValueAsString("demo");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            fixed_sha_content = (  # 伪造 fixed_sha 同路径文件内容，其中包含字段定义和多个无关 import。
                'package com.example;\n\n'
                'import com.fasterxml.jackson.databind.ObjectMapper;\n'
                'import com.example.unused.UnusedType;\n\n'
                'public class ExampleTest {\n'
                '    private static final ObjectMapper JSON_MAPPER = new ObjectMapper();\n\n'
                '    public void flakyCase() {\n'
                '        JSON_MAPPER.writeValueAsString("demo");\n'
                '    }\n'
                '}\n'
            )  # 完成 fixed_sha 文件内容构造。
            build_output = (  # 伪造当前测试文件缺失字段的编译错误。
                '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,9] cannot find symbol\n'
                '[ERROR]   symbol:   variable JSON_MAPPER\n'
                '[ERROR]   location: class com.example.ExampleTest\n'
            )  # 完成编译输出构造。
            with mock.patch('rerun_tool.patch.ensure_revision_available', return_value=(True, 'ready')), mock.patch('rerun_tool.patch.list_files_at_revision', return_value=(True, ['src/test/java/com/example/ExampleTest.java'])), mock.patch('rerun_tool.patch.read_file_at_revision', return_value=(True, fixed_sha_content)):  # 拦截 fixed_sha Git 读取逻辑，只验证字段迁移流程本身。
                ok, message = backport_fixed_sha_test_helpers(str(repo_dir), entry, str(test_file), build_output)  # 执行 fixed_sha 字段回溯。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取迁回字段后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取同步更新后的 pom。
        self.assertTrue(ok, message)  # 断言 fixed_sha 字段回溯成功。
        self.assertIn('private static final ObjectMapper JSON_MAPPER', updated_test)  # 断言字段定义已经被追加回当前测试类。
        self.assertIn('import com.fasterxml.jackson.databind.ObjectMapper;', updated_test)  # 断言字段真正依赖的 import 会被补回。
        self.assertNotIn('import com.example.unused.UnusedType;', updated_test)  # 断言无关 import 不会被整文件搬回。
        self.assertIn('<artifactId>jackson-databind</artifactId>', updated_pom)  # 断言字段依赖的测试依赖也会同步补入 pom。

    def test_backport_fixed_sha_test_helpers_can_reuse_fixed_sha_imports_for_missing_classes(self):  # 验证当目标测试文件缺少类导入时，会优先复用 fixed_sha 同文件里的 import。
        entry = _make_entry(  # 构造一个会直接使用 `Sets.newSet(...)` 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    assertEquals(Sets.newSet("a"), value);\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入 original_sha 工作区里的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertEquals(Sets.newSet("a"), value);\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            fixed_sha_content = (  # 伪造 fixed_sha 同路径文件内容，其中已经包含真正可编译的 import。
                'package com.example;\n\n'
                'import org.mockito.internal.util.collections.Sets;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertEquals(Sets.newSet("a"), value);\n'
                '    }\n'
                '}\n'
            )  # 完成 fixed_sha 文件内容构造。
            build_output = (  # 伪造当前测试文件缺失类导入的编译错误。
                '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[5,22] cannot find symbol\n'
                '[ERROR]   symbol:   class Sets\n'
                '[ERROR]   location: class com.example.ExampleTest\n'
            )  # 完成编译输出构造。
            with mock.patch('rerun_tool.patch.ensure_revision_available', return_value=(True, 'ready')), mock.patch('rerun_tool.patch.list_files_at_revision', return_value=(True, ['src/test/java/com/example/ExampleTest.java'])), mock.patch('rerun_tool.patch.read_file_at_revision', return_value=(True, fixed_sha_content)):  # 拦截 fixed_sha Git 读取逻辑，只验证 import 回补流程本身。
                ok, message = backport_fixed_sha_test_helpers(str(repo_dir), entry, str(test_file), build_output)  # 执行 fixed_sha import 回溯。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取回补 import 后的测试文件。
        self.assertTrue(ok, message)  # 断言 fixed_sha import 回补成功。
        self.assertIn('import org.mockito.internal.util.collections.Sets;', updated_test)  # 断言 fixed_sha 同文件里的 import 会被复用到 original_sha 工作区。

    def test_backport_fixed_sha_test_helpers_supports_plain_test_root_layout(self):  # 验证 fixed_sha helper 搜索也兼容老仓库常见的 `test/...` 目录布局。
        entry = _make_entry(  # 构造一个会调用 fixed_sha helper 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    assertJsonEqualsNonStrict("{}", "{}");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'test' / 'com' / 'example' / 'ExampleTest.java'  # 构造老式 `test/...` 布局下的测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入 original_sha 工作区里的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertJsonEqualsNonStrict("{}", "{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            fixed_sha_content = (  # 伪造 fixed_sha 同路径文件内容，其中已经包含真正的 helper 方法和所需 import。
                'package com.example;\n\n'
                'import org.skyscreamer.jsonassert.JSONAssert;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        assertJsonEqualsNonStrict("{}", "{}");\n'
                '    }\n\n'
                '    private void assertJsonEqualsNonStrict(String expected, String actual) throws Exception {\n'
                '        JSONAssert.assertEquals(expected, actual, false);\n'
                '    }\n'
                '}\n'
            )  # 完成 fixed_sha 文件内容构造。
            build_output = (  # 伪造当前测试文件缺失 helper 方法的编译错误。
                '[ERROR] /workspace/test/com/example/ExampleTest.java:[5,9] cannot find symbol\n'
                '[ERROR]   symbol:   method assertJsonEqualsNonStrict(java.lang.String,java.lang.String)\n'
                '[ERROR]   location: class com.example.ExampleTest\n'
            )  # 完成编译输出构造。
            with mock.patch('rerun_tool.patch.ensure_revision_available', return_value=(True, 'ready')), mock.patch('rerun_tool.patch.list_files_at_revision', return_value=(True, ['test/com/example/ExampleTest.java'])), mock.patch('rerun_tool.patch.read_file_at_revision', return_value=(True, fixed_sha_content)):  # 拦截 fixed_sha Git 读取逻辑，只验证老式测试目录下的 helper 迁移流程。
                ok, message = backport_fixed_sha_test_helpers(str(repo_dir), entry, str(test_file), build_output)  # 执行 fixed_sha helper 回溯。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取迁回 helper 后的测试文件。
        self.assertTrue(ok, message)  # 断言老式 `test/...` 目录布局下也可以成功回补 helper。
        self.assertIn('private void assertJsonEqualsNonStrict', updated_test)  # 断言 helper 方法已经被追加回当前测试类。

    def test_backport_fixed_sha_test_helpers_routes_qualified_static_helper_to_owner_file(self):  # 验证 `JSONSchemaTest.assertJSONEqual(...)` 这类限定静态调用会把 helper 回补到 owner 类，而不是当前目标测试类。
        entry = _make_entry(  # 构造一个会调用 sibling test helper 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    JSONSchemaTest.assertJSONEqual("{}", "{}");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            target_test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'KeyValueSchemaInfoTest.java'  # 构造当前目标测试文件路径。
            owner_test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'JSONSchemaTest.java'  # 构造 helper 真正所属的 sibling 测试文件路径。
            owner_test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            target_test_file.write_text(  # 写入当前目标测试文件。
                'package com.example;\n\n'
                'public class KeyValueSchemaInfoTest {\n'
                '    public void flakyCase() {\n'
                '        JSONSchemaTest.assertJSONEqual("{}", "{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            owner_test_file.write_text(  # 写入当前 original_sha 下还没有 helper 的 sibling 测试文件。
                'package com.example;\n\n'
                'public class JSONSchemaTest {\n'
                '}\n',
                encoding='utf-8',
            )  # 完成 owner 测试文件写入。
            fixed_sha_owner_content = (  # 伪造 fixed_sha 的 sibling 测试文件内容，其中已经包含 helper 定义和所需 import。
                'package com.example;\n\n'
                'import org.skyscreamer.jsonassert.JSONAssert;\n\n'
                'public class JSONSchemaTest {\n'
                '    public static void assertJSONEqual(String expected, String actual) throws Exception {\n'
                '        JSONAssert.assertEquals(expected, actual, false);\n'
                '    }\n'
                '}\n'
            )  # 完成 fixed_sha owner 文件内容构造。
            build_output = (  # 伪造当前目标测试文件缺失 sibling helper 的编译错误。
                '[ERROR] /workspace/src/test/java/com/example/KeyValueSchemaInfoTest.java:[5,9] cannot find symbol\n'
                '[ERROR]   symbol:   method assertJSONEqual(java.lang.String,java.lang.String)\n'
                '[ERROR]   location: class com.example.JSONSchemaTest\n'
            )  # 完成编译输出构造。
            def _read_file_side_effect(_repo_dir, _fixed_sha, relative_path, timeout=120):  # 根据 fixed_sha 查询路径返回不同的文件内容。
                if relative_path.endswith('JSONSchemaTest.java'):  # 只有 owner 文件中存在真正的 helper。
                    return True, fixed_sha_owner_content
                return True, 'package com.example;\npublic class KeyValueSchemaInfoTest {}\n'
            with mock.patch('rerun_tool.patch.ensure_revision_available', return_value=(True, 'ready')), mock.patch('rerun_tool.patch.list_files_at_revision', return_value=(True, ['src/test/java/com/example/KeyValueSchemaInfoTest.java', 'src/test/java/com/example/JSONSchemaTest.java'])), mock.patch('rerun_tool.patch.read_file_at_revision', side_effect=_read_file_side_effect):  # 拦截 fixed_sha Git 读取逻辑，只验证限定静态 helper 路由流程。
                ok, message = backport_fixed_sha_test_helpers(str(repo_dir), entry, str(target_test_file), build_output)  # 执行 fixed_sha helper 回溯。
            updated_target_test = target_test_file.read_text(encoding='utf-8')  # 读取目标测试文件。
            updated_owner_test = owner_test_file.read_text(encoding='utf-8')  # 读取真正持有 helper 的 sibling 测试文件。
        self.assertTrue(ok, message)  # 断言限定静态 helper 回溯成功。
        self.assertNotIn('public static void assertJSONEqual', updated_target_test)  # 断言 helper 方法定义不会被错误地追加到当前目标测试类。
        self.assertIn('public static void assertJSONEqual', updated_owner_test)  # 断言 helper 被正确追加到 owner 类。
        self.assertIn('import org.skyscreamer.jsonassert.JSONAssert;', updated_owner_test)  # 断言 owner 文件也会同步补齐 helper 所需的 import。

    def test_backport_fixed_sha_test_helpers_can_append_into_file_with_text_block(self):  # 验证类成员插入会正确跳过 Java text block，不会把 helper 插到错误的大括号层级。
        entry = _make_entry(  # 构造一个会调用 fixed_sha helper 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    helper();\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个内部包含 Java text block 的目标测试文件。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        String payload = """\n'
                '            {\n'
                '              "id": 1\n'
                '            }\n'
                '            """;\n'
                '        helper();\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            fixed_sha_content = (  # 伪造 fixed_sha 同路径文件内容，其中已经包含真正的 helper 方法。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        helper();\n'
                '    }\n\n'
                '    private void helper() {\n'
                '    }\n'
                '}\n'
            )  # 完成 fixed_sha 文件内容构造。
            build_output = (  # 伪造当前测试文件缺失 helper 方法的编译错误。
                '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[10,9] cannot find symbol\n'
                '[ERROR]   symbol:   method helper()\n'
                '[ERROR]   location: class com.example.ExampleTest\n'
            )  # 完成编译输出构造。
            with mock.patch('rerun_tool.patch.ensure_revision_available', return_value=(True, 'ready')), mock.patch('rerun_tool.patch.list_files_at_revision', return_value=(True, ['src/test/java/com/example/ExampleTest.java'])), mock.patch('rerun_tool.patch.read_file_at_revision', return_value=(True, fixed_sha_content)):  # 拦截 fixed_sha Git 读取逻辑，只验证 text block 场景下的成员插入。
                ok, message = backport_fixed_sha_test_helpers(str(repo_dir), entry, str(test_file), build_output)  # 执行 fixed_sha helper 回溯。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取迁回 helper 后的测试文件。
        self.assertTrue(ok, message)  # 断言 helper 回溯成功。
        self.assertRegex(updated_test, r'private void helper\(\)\s*\{\s*\}\s*\}\s*$')  # 断言 helper 最终落在顶层类闭合大括号之前，而不是 text block 内部。

    def test_fix_related_test_imports_can_repair_non_target_test_files(self):  # 验证 test-compile 连带失败的同模块非目标测试文件也会被保守修复。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            target_test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            related_test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ConfigUtilTest.java'  # 构造被连带卡住的非目标测试文件。
            witness_test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'WitnessTest.java'  # 构造提供 shaded import 线索的 witness 文件。
            witness_test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            target_test_file.write_text(  # 写入目标测试文件。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {}\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            related_test_file.write_text(  # 写入缺少 shaded ConfigFactory import 的非目标测试文件。
                'package com.example;\n\n'
                'public class ConfigUtilTest {\n'
                '    public void flakyCase() {\n'
                '        ConfigFactory.parseString("demo.enabled=true");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成非目标测试文件写入。
            witness_test_file.write_text(  # 写入一个仓库内已存在的 shaded import 线索文件。
                'package com.example;\n\n'
                'import org.apache.seatunnel.shade.com.typesafe.config.ConfigFactory;\n\n'
                'public class WitnessTest {\n'
                '    public void witness() {\n'
                '        ConfigFactory.parseString("demo.enabled=true");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成 witness 文件写入。
            build_output = (  # 伪造当前模块里非目标测试文件缺失 import 的编译错误。
                f'[ERROR] {related_test_file}:[4,9] cannot find symbol\n'
                '[ERROR]   symbol:   variable ConfigFactory\n'
                '[ERROR]   location: class com.example.ConfigUtilTest\n'
            )  # 完成编译输出构造。
            ok, message = fix_related_test_imports(str(repo_dir), str(target_test_file), build_output)  # 执行非目标测试 import 修复。
            updated_related_test = related_test_file.read_text(encoding='utf-8')  # 读取修复后的非目标测试文件。
        self.assertTrue(ok, message)  # 断言非目标测试修复成功。
        self.assertIn('import org.apache.seatunnel.shade.com.typesafe.config.ConfigFactory;', updated_related_test)  # 断言工具会把仓库里已有的 shaded import 线索补到非目标测试文件。

    def test_apply_generated_patch_context_skips_conflicting_simple_name_import(self):  # 验证 generated_patch 上下文增强不会再把同名不同库的 import 强行插进当前文件。
        entry = _make_entry(  # 构造一个会使用 TypeReference 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    TypeReference<String> value = null;\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个已经显式导入 fastjson TypeReference 的测试类。
                'package com.example;\n\n'
                'import com.alibaba.fastjson.TypeReference;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        TypeReference<String> value = null;\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取增强后的测试文件。
        self.assertTrue(ok, message)  # 断言上下文增强流程本身可以正常结束。
        self.assertIn('import com.alibaba.fastjson.TypeReference;', updated_test)  # 断言当前文件已有 import 会被保留。
        self.assertNotIn('import com.fasterxml.jackson.core.type.TypeReference;', updated_test)  # 断言不会再插入同名但不同库的冲突 import。

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

    def test_fix_unreported_exception_declaration_prefers_method_covering_error_line(self):  # 验证当构建日志给出具体报错行时，会优先修改真正出错的方法而不是请求里的测试方法名。
        source = (  # 构造一个包含两个方法的最小 Java 测试类。
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        helper();\n'
            '    }\n'
            '\n'
            '    private void helper() {\n'
            '        parser.parse("{}");\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        build_output = (  # 伪造真正报错发生在 helper() 方法体里的 checked exception 日志。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[7,9] 未报告的异常错误org.json.JSONException; 必须对其进行捕获或声明以便抛出\n'
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_unreported_exception_declaration(str(test_file), 'flakyCase', build_output)  # 即使请求方法名是 flakyCase，也应该优先修 helper()。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言修复成功。
        self.assertIn('private void helper() throws org.json.JSONException', updated)  # 断言真正出错的方法声明已追加异常。
        self.assertNotIn('public void flakyCase() throws org.json.JSONException', updated)  # 断言不会把 throws 错加到未报错的方法上。

    def test_fix_unreported_exception_declaration_does_not_drift_to_adjacent_method_on_nearby_line_number(self):  # 验证当编译器行号轻微漂移到相邻方法附近时，不会把 throws 错加到下一个方法上。
        source = (  # 构造一个包含两个紧邻方法的最小 Java 测试类。
            'public class ExampleTest {\n'
            '    public void flakyCase() {\n'
            '        parser.parse("{}");\n'
            '    }\n'
            '\n'
            '    public void nextCase() {\n'
            '    }\n'
            '}\n'
        )  # 完成测试源文件构造。
        build_output = (  # 伪造编译器行号已经漂移到相邻方法声明附近的 checked exception 日志。
            '[ERROR] /workspace/src/test/java/com/example/ExampleTest.java:[6,9] 未报告的异常错误org.json.JSONException; 必须对其进行捕获或声明以便抛出\n'
        )  # 完成编译输出构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离临时目录。
            test_file = Path(tmp_dir) / 'ExampleTest.java'  # 构造测试文件路径。
            test_file.write_text(source, encoding='utf-8')  # 写入原始 Java 文件。
            ok, message = fix_unreported_exception_declaration(str(test_file), 'flakyCase', build_output)  # 执行 checked exception 修复。
            updated = test_file.read_text(encoding='utf-8')  # 读取修复后的文件内容。
        self.assertTrue(ok, message)  # 断言修复成功。
        self.assertIn('public void flakyCase() throws org.json.JSONException', updated)  # 断言 throws 仍然加在目标测试方法上。
        self.assertNotIn('public void nextCase() throws org.json.JSONException', updated)  # 断言不会再误修相邻方法。

    def test_apply_generated_patch_context_does_not_treat_qualified_when_as_catch_exception(self):  # 验证 `mocked.when(...)` 这类成员调用不会再误触发 catch-exception 依赖。
        entry = _make_entry(  # 构造一个只包含 Mockito 风格 `.when(...)` 的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    mocked.when(Service::call).thenReturn("demo");\n'
            '    assertThat("demo").startsWith("de");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            witness = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'WitnessTest.java'  # 构造一个提供 AssertJ 线索的仓库内测试文件。
            witness.parent.mkdir(parents=True)  # 创建 witness 文件目录。
            witness.write_text(  # 写入一个已经使用 AssertJ assertThat 的最小测试类。
                'package com.example;\n\n'
                'import static org.assertj.core.api.Assertions.assertThat;\n\n'
                'public class WitnessTest {\n'
                '    public void witness() {\n'
                '        assertThat("demo").startsWith("de");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成 witness 文件写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.write_text(  # 写入一个只会触发 Mockito 成员调用 `.when(...)` 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    private MockedStatic<Service> mocked;\n'
                '    public void flakyCase() {\n'
                '        mocked.when(Service::call).thenReturn("demo");\n'
                '        assertThat("demo").startsWith("de");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取增强后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取增强后的 pom。
        self.assertTrue(ok, message)  # 断言上下文增强可以正常结束。
        self.assertIn('import static org.assertj.core.api.Assertions.assertThat;', updated_test)  # 断言仍然会补入真正需要的 AssertJ `assertThat`。
        self.assertNotIn('CatchException.when', updated_test)  # 断言不会再把 Mockito 的 `.when(...)` 误判成 catch-exception static import。
        self.assertNotIn('<artifactId>catch-exception</artifactId>', updated_pom)  # 断言 pom 里不会再误加 catch-exception 依赖。

    def test_apply_generated_patch_context_prefers_mockito_when_static_import(self):  # 验证裸的 `when(...).thenReturn(...)` 会被识别成 Mockito，而不是 catch-exception。
        entry = _make_entry(  # 构造一个只包含 Mockito 风格裸 `when(...)` 调用的最小 generated_patch。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    when(service.call()).thenReturn("demo");\n'
            '    assertThat("demo").startsWith("de");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个使用裸 Mockito `when(...)` 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    private Service service;\n'
                '    public void flakyCase() {\n'
                '        when(service.call()).thenReturn("demo");\n'
                '        assertThat("demo").startsWith("de");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取增强后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取增强后的 pom。
        self.assertTrue(ok, message)  # 断言上下文增强可以正常结束。
        self.assertIn('import static org.mockito.Mockito.when;', updated_test)  # 断言裸 `when(...)` 会被识别为 Mockito 的静态导入。
        self.assertNotIn('CatchException.when', updated_test)  # 断言不会再误绑到 catch-exception。
        self.assertNotIn('<artifactId>catch-exception</artifactId>', updated_pom)  # 断言 pom 里不会误加 catch-exception。

    def test_apply_generated_patch_context_infers_org_json_and_keeps_dependency_top_level(self):  # 验证 `JSONObject` 会绑定到 org.json，且新增依赖写在 project 顶层而不是 plugin 依赖区。
        entry = _make_entry(  # 构造一个会在 generated_patch 中使用 `JSONObject` 的最小条目。
            'public void flakyCase() {}\n',
            'public void flakyCase() {\n'
            '    JSONObject actual = new JSONObject("{}");\n'
            '}\n',
        )  # 完成最小测试条目构造。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text(  # 写入一个只包含 plugin 依赖、还没有顶层 dependencies 的最小 pom。
                '<project>\n'
                '    <build>\n'
                '        <plugins>\n'
                '            <plugin>\n'
                '                <artifactId>maven-compiler-plugin</artifactId>\n'
                '                <dependencies>\n'
                '                    <dependency>\n'
                '                        <groupId>org.codehaus.plexus</groupId>\n'
                '                        <artifactId>plexus-compiler-javac</artifactId>\n'
                '                    </dependency>\n'
                '                </dependencies>\n'
                '            </plugin>\n'
                '        </plugins>\n'
                '    </build>\n'
                '</project>\n',
                encoding='utf-8',
            )  # 完成最小 pom 写入。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个还没有 `JSONObject` import 的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '        JSONObject actual = new JSONObject("{}");\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            ok, message = apply_generated_patch_context(str(repo_dir), entry, str(test_file))  # 执行 generated_patch 上下文增强。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取增强后的测试文件。
            updated_pom = (repo_dir / 'pom.xml').read_text(encoding='utf-8')  # 读取增强后的 pom。
        self.assertTrue(ok, message)  # 断言上下文增强成功。
        self.assertIn('import org.json.JSONObject;', updated_test)  # 断言补入的是 org.json 的 `JSONObject` 导入。
        self.assertNotIn('org.codehaus.jettison.json.JSONObject', updated_test)  # 断言不会误绑到 jettison。
        self.assertIn('<artifactId>json</artifactId>', updated_pom)  # 断言 pom 顶层补入了 org.json 依赖。
        self.assertLess(updated_pom.index('<artifactId>json</artifactId>'), updated_pom.index('<build>'))  # 断言新增依赖块位于 build 之前，也就是 project 顶层。

    def test_apply_reference_patch_context_prefers_helper_code_context_for_json_parser(self):  # 验证 fixed_sha helper 正文里的旧式 Gson `new JsonParser().parse(...)` 语义也能被正确识别。
        entry = _make_entry('public void flakyCase() {}\n', 'public void flakyCase() {}\n')  # 构造最小测试条目。
        with tempfile.TemporaryDirectory() as tmp_dir:  # 创建隔离仓库目录。
            repo_dir = Path(tmp_dir)  # 包装仓库路径便于创建文件。
            (repo_dir / '.git').mkdir()  # 创建空 .git 目录作为仓库根标记。
            (repo_dir / 'pom.xml').write_text('<project>\n</project>\n', encoding='utf-8')  # 写入最小 pom。
            test_file = repo_dir / 'src' / 'test' / 'java' / 'com' / 'example' / 'ExampleTest.java'  # 构造目标测试文件路径。
            test_file.parent.mkdir(parents=True)  # 创建测试文件目录。
            test_file.write_text(  # 写入一个当前文件尚未带入 `JsonParser` 上下文的最小测试类。
                'package com.example;\n\n'
                'public class ExampleTest {\n'
                '    public void flakyCase() {\n'
                '    }\n'
                '}\n',
                encoding='utf-8',
            )  # 完成目标测试文件写入。
            candidate = ReferencePatchCandidate(  # 构造一个明显属于 Gson 语义的 helper 候选。
                source_path='fixed_sha:src/test/java/com/example/ExampleTest.java',
                test_code='private void assertJsonStringEquals(String left, String right) {\n'
                '    JsonParser parser = new JsonParser();\n'
                '    JsonObject actual = parser.parse(left).getAsJsonObject();\n'
                '    assertEquals(actual, parser.parse(right).getAsJsonObject());\n'
                '}\n',
            )  # 完成 helper 候选构造。
            ok, message = apply_reference_patch_context(str(repo_dir), entry, str(test_file), candidate)  # 执行参考上下文应用。
            updated_test = test_file.read_text(encoding='utf-8')  # 读取增强后的测试文件。
        self.assertTrue(ok, message)  # 断言上下文应用成功。
        self.assertIn('import com.google.gson.JsonParser;', updated_test)  # 断言会按 helper 正文语义补入 Gson 的 `JsonParser`。
        self.assertIn('import com.google.gson.JsonObject;', updated_test)  # 断言也会补入 Gson 的 `JsonObject`。


if __name__ == '__main__':  # 允许单文件直接运行测试。
    unittest.main()  # 执行当前文件中的全部测试用例。
