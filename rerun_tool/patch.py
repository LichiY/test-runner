"""Patch application logic for flaky tests.

Uses a robust method-name-based approach:
1. Find the test file by class name
2. Locate the target method by method name + regex
3. Extract the full method using brace counting
4. Replace it with the generated patch
5. Verify the replacement was correct
"""

import difflib  # 使用文本相似度帮助选择最可能的目标文件和方法。
import logging
import os
import re
import shutil
import ast  # 用于宽松解析参考补丁 import 段里出现的 Python 列表文本。
from dataclasses import dataclass  # 用于承载参考补丁候选信息。
from typing import Dict, List, Optional, Tuple

from .data import TestEntry
from .repo import ensure_revision_available, list_files_at_revision, read_file_at_revision

logger = logging.getLogger(__name__)

JAVA_IMPORT_CANDIDATES = {  # 仅为标准库和高确定性的基础类型提供无仓库上下文也可安全启用的 import 推断表。
    'ArrayList': 'java.util.ArrayList',  # ArrayList 属于 java.util。
    'Arrays': 'java.util.Arrays',  # Arrays 属于 java.util。
    'Collection': 'java.util.Collection',  # Collection 属于 java.util。
    'Collections': 'java.util.Collections',  # Collections 属于 java.util。
    'Comparator': 'java.util.Comparator',  # Comparator 属于 java.util。
    'Date': 'java.util.Date',  # Date 属于 java.util。
    'HashMap': 'java.util.HashMap',  # HashMap 属于 java.util。
    'HashSet': 'java.util.HashSet',  # HashSet 属于 java.util。
    'Iterator': 'java.util.Iterator',  # Iterator 属于 java.util。
    'LinkedHashMap': 'java.util.LinkedHashMap',  # LinkedHashMap 属于 java.util。
    'LinkedHashSet': 'java.util.LinkedHashSet',  # LinkedHashSet 属于 java.util。
    'LinkedList': 'java.util.LinkedList',  # LinkedList 属于 java.util。
    'List': 'java.util.List',  # List 属于 java.util。
    'Map': 'java.util.Map',  # Map 属于 java.util。
    'Objects': 'java.util.Objects',  # Objects 属于 java.util。
    'Optional': 'java.util.Optional',  # Optional 属于 java.util。
    'ParseException': 'java.text.ParseException',  # ParseException 常见于 java.text。
    'Queue': 'java.util.Queue',  # Queue 属于 java.util。
    'Set': 'java.util.Set',  # Set 属于 java.util。
    'SortedMap': 'java.util.SortedMap',  # SortedMap 属于 java.util。
    'SortedSet': 'java.util.SortedSet',  # SortedSet 属于 java.util。
    'TreeMap': 'java.util.TreeMap',  # TreeMap 属于 java.util。
    'TreeSet': 'java.util.TreeSet',  # TreeSet 属于 java.util。
}

JAVA_STATIC_IMPORT_CANDIDATES = {  # 为常见的静态断言与匹配器方法提供保守的 static import 推断表。
    'containsString': 'org.hamcrest.Matchers.containsString',  # containsString 常见于 Hamcrest 匹配器。
    'containsInAnyOrder': 'org.hamcrest.Matchers.containsInAnyOrder',  # containsInAnyOrder 常见于 Hamcrest 匹配器。
    'caughtException': 'com.googlecode.catchexception.CatchException.caughtException',  # caughtException 来自 catch-exception。
    'then': 'org.assertj.core.api.Assertions.then',  # then 常见于 AssertJ 的 BDD 风格断言。
    'tuple': 'org.assertj.core.groups.Tuple.tuple',  # tuple 常见于 AssertJ 分组断言。
    'when': 'com.googlecode.catchexception.CatchException.when',  # when 来自 catch-exception。
}  # 当前仅收录高置信度且歧义较低的方法级修复候选。

JUNIT_ASSERT_METHODS = {  # 为可安全回退到 `Assert.xxx(...)` 形式的 JUnit 断言方法建立白名单。
    'assertArrayEquals',  # JUnit 常见数组断言方法。
    'assertEquals',  # JUnit 常见相等断言方法。
    'assertFalse',  # JUnit 常见布尔断言方法。
    'assertNotNull',  # JUnit 常见非空断言方法。
    'assertNull',  # JUnit 常见空值断言方法。
    'assertTrue',  # JUnit 常见布尔断言方法。
}  # 仅收录在 JUnit `Assert` 中稳定存在的方法，避免误把未知 helper 绑定到错误断言库。

REFERENCE_CODE_IMPORT_INFERENCE = {  # 这些导入推断规则来自离线参考补丁分析，运行时也会复用于原始 generated_patch 的上下文补全。
    'Assertions': 'import org.assertj.core.api.Assertions;',
    'CollectionUtils': 'import org.apache.commons.collections4.CollectionUtils;',
    'TypeReference': 'import com.fasterxml.jackson.core.type.TypeReference;',
    'JsonPath': 'import com.jayway.jsonpath.JsonPath;',
    'DocumentContext': 'import com.jayway.jsonpath.DocumentContext;',
    'ReadContext': 'import com.jayway.jsonpath.ReadContext;',
    'Configuration': 'import com.jayway.jsonpath.Configuration;',
    'JSONAssert': 'import org.skyscreamer.jsonassert.JSONAssert;',
    'JSONCompareMode': 'import org.skyscreamer.jsonassert.JSONCompareMode;',
    'JsonObject': 'import com.google.gson.JsonObject;',
    'JsonParser': 'import com.google.gson.JsonParser;',
    'JsonElement': 'import com.google.gson.JsonElement;',
    'JSONObject': 'import org.json.JSONObject;',
    'JSONArray': 'import org.json.JSONArray;',
    'JsonNode': 'import com.fasterxml.jackson.databind.JsonNode;',
    'ObjectMapper': 'import com.fasterxml.jackson.databind.ObjectMapper;',
    'JsonMappingException': 'import com.fasterxml.jackson.databind.JsonMappingException;',
    'StdDateFormat': 'import com.fasterxml.jackson.databind.util.StdDateFormat;',
    'JsonProcessingException': 'import com.fasterxml.jackson.core.JsonProcessingException;',
    'XmlMapper': 'import com.fasterxml.jackson.dataformat.xml.XmlMapper;',
    'ImmutableList': 'import com.google.common.collect.ImmutableList;',
    'ImmutableMap': 'import com.google.common.collect.ImmutableMap;',
    'ImmutableSet': 'import com.google.common.collect.ImmutableSet;',
    'MockMvc': 'import org.springframework.test.web.servlet.MockMvc;',
    'MockMvcRequestBuilders': 'import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;',
    'ConfigFactory': 'import com.typesafe.config.ConfigFactory;',
    'ConfigResolveOptions': 'import com.typesafe.config.ConfigResolveOptions;',
    'JSONException': 'import org.json.JSONException;',
}  # 这些符号在当前失败集里高频出现，并且基本都对应稳定的第三方类。

REFERENCE_CODE_STATIC_IMPORT_INFERENCE = {  # 这些静态导入规则来自离线成功样本，只有正文明确使用 helper 时才会补入。
    'assertThatJson': 'import static net.javacrumbs.jsonunit.assertj.JsonAssertions.assertThatJson;',
    'caughtException': 'import static com.googlecode.catchexception.CatchException.caughtException;',
    'then': 'import static org.assertj.core.api.Assertions.then;',
    'when': 'import static com.googlecode.catchexception.CatchException.when;',
}  # 当前只补一个高频且来源明确的 helper，避免重新走回盲目修补。

REFERENCE_DEPENDENCY_SNIPPETS = {  # 这些依赖片段来自离线成功样本归纳，运行时只会按当前补丁正文的使用痕迹补入。
    'assertj-core': (
        '<dependency>\n'
        '  <groupId>org.assertj</groupId>\n'
        '  <artifactId>assertj-core</artifactId>\n'
        '  <version>3.24.2</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'commons-collections4': (
        '<dependency>\n'
        '  <groupId>org.apache.commons</groupId>\n'
        '  <artifactId>commons-collections4</artifactId>\n'
        '  <version>4.4</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'json-path': (
        '<dependency>\n'
        '  <groupId>com.jayway.jsonpath</groupId>\n'
        '  <artifactId>json-path</artifactId>\n'
        '  <version>2.8.0</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'jsonassert': (
        '<dependency>\n'
        '  <groupId>org.skyscreamer</groupId>\n'
        '  <artifactId>jsonassert</artifactId>\n'
        '  <version>1.5.1</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'gson': (
        '<dependency>\n'
        '  <groupId>com.google.code.gson</groupId>\n'
        '  <artifactId>gson</artifactId>\n'
        '  <version>2.10.1</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'jackson-databind': (
        '<dependency>\n'
        '  <groupId>com.fasterxml.jackson.core</groupId>\n'
        '  <artifactId>jackson-databind</artifactId>\n'
        '  <version>2.13.0</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'json': (
        '<dependency>\n'
        '  <groupId>org.json</groupId>\n'
        '  <artifactId>json</artifactId>\n'
        '  <version>20240303</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'json-unit-assertj': (
        '<dependency>\n'
        '  <groupId>net.javacrumbs.json-unit</groupId>\n'
        '  <artifactId>json-unit-assertj</artifactId>\n'
        '  <version>2.38.0</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'guava': (
        '<dependency>\n'
        '  <groupId>com.google.guava</groupId>\n'
        '  <artifactId>guava</artifactId>\n'
        '  <version>31.1-jre</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'spring-test': (
        '<dependency>\n'
        '  <groupId>org.springframework</groupId>\n'
        '  <artifactId>spring-test</artifactId>\n'
        '  <version>5.3.27</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'jackson-dataformat-xml': (
        '<dependency>\n'
        '  <groupId>com.fasterxml.jackson.dataformat</groupId>\n'
        '  <artifactId>jackson-dataformat-xml</artifactId>\n'
        '  <version>2.13.0</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'typesafe-config': (
        '<dependency>\n'
        '  <groupId>com.typesafe</groupId>\n'
        '  <artifactId>config</artifactId>\n'
        '  <version>1.4.2</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'catch-exception': (
        '<dependency>\n'
        '  <groupId>com.googlecode.catch-exception</groupId>\n'
        '  <artifactId>catch-exception</artifactId>\n'
        '  <version>1.4.6</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'jettison': (
        '<dependency>\n'
        '  <groupId>org.codehaus.jettison</groupId>\n'
        '  <artifactId>jettison</artifactId>\n'
        '  <version>1.5.4</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
    'hadoop-common': (
        '<dependency>\n'
        '  <groupId>org.apache.hadoop</groupId>\n'
        '  <artifactId>hadoop-common</artifactId>\n'
        '  <version>3.3.6</version>\n'
        '  <scope>test</scope>\n'
        '</dependency>'
    ),
}  # 只覆盖当前失败集里明确出现的几个测试依赖。

REFERENCE_PROJECT_HELPER_MARKERS = (  # 这些 helper 在 v5 失败集中反复证明“看起来像正确修复，但在原始 SHA 上并不存在”。
    'assertJsonEqualsNonStrict',
    'assertJsonStringEquals',
    'assertJSONEqual',
)  # 排序时需要显式把它们放后，避免比可编译的标准 API 候选更早被尝试。

ASSERTJ_ASSERT_THAT_METHODS = (  # 用于识别 AssertJ `assertThat(...)` 链式断言的高频方法。
    'isEqualTo',
    'contains',
    'startsWith',
    'endsWith',
    'hasSize',
    'containsExactly',
    'containsExactlyInAnyOrderElementsOf',
    'containsOnly',
    'containsEntry',
    'containsKey',
    'containsValue',
    'isTrue',
    'isFalse',
)  # 仅收录当前失败集中真实出现过或同族的 AssertJ 链式方法。

FIXED_SHA_HELPER_SEARCH_DIRS = ('src/test/java', 'src/test/groovy', 'src/test/scala')  # fixed_sha helper 回溯只搜索测试侧源码，避免把业务代码迁回 original_sha。


@dataclass(frozen=True)  # 用结构化对象承载“成功样本中抽取出的上下文信号”，运行时和离线分析都可复用。
class ReferencePatchCandidate:
    source_path: str  # 记录当前上下文信号来自哪里，比如参考补丁文件或 synthetic generated_patch。
    test_code: str  # 保存可用于推断 import、dependency 和 throws 的方法代码片段。
    imports: Tuple[str, ...] = ()  # 保留样本里显式声明的 import 片段，便于后续诊断。
    pom_snippet: str = ''  # 保留样本里显式声明的 pom 依赖提示，便于排序和排查。
    destination_rel_path: str = ''  # fixed_sha 成员回补时记录应写回 original_sha 工作区的目标文件路径。


@dataclass(frozen=True)  # 用结构化对象承载“当前构建输出里缺失的方法引用”。
class MissingMethodReference:
    method_name: str  # 保存缺失的方法名。
    owner_class_name: str = ''  # 保存编译器 location 指向的实际类名，用于区分限定静态调用。
    error_file_path: str = ''  # 保存错误发生的文件路径，便于后续诊断和回补目标决策。


def backup_file(file_path: str) -> bool:  # 为将要被修改的文件创建一次性基线备份。
    if not file_path or not os.path.isfile(file_path):  # 文件不存在时无法创建备份。
        return False  # 返回失败供上层决定是否继续。
    backup_path = file_path + '.bak'  # 统一沿用 `.bak` 后缀以便复用现有恢复逻辑。
    if os.path.isfile(backup_path):  # 已有备份时直接复用，避免把原始基线覆盖成中间状态。
        return True  # 当前文件已经具备可恢复基线。
    shutil.copy2(file_path, backup_path)  # 首次修改前保存原始文件。
    return True  # 返回成功。


def find_test_file(repo_dir: str, entry: TestEntry) -> Optional[str]:
    """Find the test Java file in the repository.

    Uses multiple search strategies with increasing scope.

    Args:
        repo_dir: Path to the repository root.
        entry: The test entry containing class info.

    Returns:
        Absolute path to the test file, or None if not found.
    """
    class_path = entry.class_path  # e.g., com/foo/Bar.java
    simple_filename = os.path.basename(class_path)

    # Determine module-specific search root
    if entry.module and entry.module != '.':
        module_root = os.path.join(repo_dir, entry.module)
    else:
        module_root = repo_dir

    candidates = []  # 收集所有候选文件后统一打分，避免过早返回错误文件。

    # Strategy 1: Standard test source directories (fastest)
    for base_dir in [module_root, repo_dir]:
        for src_dir in ['src/test/java', 'src/test/groovy', 'src/test/scala',
                        'src/test', 'test/java', 'test']:
            candidate = os.path.join(base_dir, src_dir, class_path)
            if os.path.isfile(candidate):
                candidates.append(candidate)  # 标准目录命中的候选也先加入列表参与后续排序。

    # Strategy 2: Walk directories, match by full class path suffix
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules', '.idea'}
    for base_dir in [module_root, repo_dir]:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            if simple_filename in files:
                full_path = os.path.join(root, simple_filename)
                if full_path.replace(os.sep, '/').endswith(class_path):
                    candidates.append(full_path)  # 记录类路径后缀匹配的候选文件。

    # Strategy 3: Match by filename only (last resort, may find wrong file)
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if simple_filename in files:
            full_path = os.path.join(root, simple_filename)
            candidates.append(full_path)  # 文件名兜底候选权重最低，交由评分函数决定。

    best_candidate = _pick_best_test_file(candidates, entry)  # 使用方法名和 flaky 代码对候选文件排序。
    if best_candidate is not None:
        logger.info(f"Selected test file: {best_candidate}")  # 记录最终选择的文件路径。
        return best_candidate  # 返回打分最高的候选文件。

    logger.error(f"Test file not found: {class_path}")
    return None


def apply_patch(test_file: str, entry: TestEntry, allow_low_similarity_on_unique_match: bool = False) -> Tuple[bool, str]:
    """Apply the generated patch to the test file.

    Primary strategy: locate method by name with brace counting,
    then replace the entire method body.

    Creates a backup before modifying.

    Args:
        test_file: Path to the test Java file.
        entry: The test entry containing flaky_code and generated_patch.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not entry.generated_patch or not entry.generated_patch.strip():
        return False, "Empty generated_patch"

    try:
        with open(test_file, 'r', encoding='utf-8') as f:
            original_content = f.read()

        backup_path = test_file + '.bak'  # 回退路径统一和 backup_file/restore_backup 保持一致。
        if not backup_file(test_file):  # 在首次修改前固定保存原始文件，避免后续回退链条丢失基线。
            return False, f"Failed to create backup for {os.path.basename(test_file)}"

        method_name = entry.test_method
        lines = original_content.splitlines()
        method_candidates = _find_method_declaration_candidates(lines, method_name)  # 单独记录同名方法候选数，供参考补丁回退时决定是否放宽目标相似度保护。

        # Step 1: Find the method declaration line
        method_start = _find_method_declaration(lines, method_name, entry.flaky_code)  # 用原 flaky 代码辅助锁定正确的方法节点。
        if method_start is None:
            return False, f"Method '{method_name}' not found in {os.path.basename(test_file)}"

        # Step 2: Find method end using brace counting
        method_end = _find_method_end(lines, method_start)
        if method_end is None:
            return False, f"Could not find closing brace for method '{method_name}'"

        original_method = '\n'.join(lines[method_start:method_end + 1])  # 提取当前文件中的目标方法源码用于相似度校验。
        reference_similarity = _method_similarity(original_method, entry.flaky_code)  # 比较当前方法与数据集中 flaky 方法的一致性。
        if entry.flaky_code and reference_similarity < 0.60:  # 相似度过低说明大概率定位错了方法。
            if allow_low_similarity_on_unique_match and len(method_candidates) == 1:  # 参考补丁回退里只要文件和方法唯一，继续强卡 0.60 会把大量正确候选误拒绝。
                logger.info(f"Bypassing low-similarity target guard for unique method '{method_name}' (similarity={reference_similarity:.2f})")  # 记录当前是在唯一方法场景下放宽保护。
            else:  # 其余场景仍然保留原有的强保护，避免误贴到错误方法上。
                return False, f"Target method mismatch (similarity={reference_similarity:.2f})"  # 主动失败以避免误贴补丁。

        # Step 3: Detect the file's indentation for this method
        method_line = lines[method_start]
        file_indent = len(method_line) - len(method_line.lstrip())
        file_indent_str = method_line[:file_indent]

        # Step 4: Normalize the patch text before indentation handling
        patch_text = entry.generated_patch.strip()  # 去掉首尾空白以便后续统一处理补丁文本。
        patch_text, declaration_preserved = _preserve_original_declaration(original_method, patch_text)  # 如果生成补丁擅自修改方法头，则回退到原始方法声明。
        if declaration_preserved:  # 只有真的发生方法头回退时才记录日志。
            logger.info(f"Patch declaration adjusted to preserve original signature for {method_name}")  # 记录本次签名保护已经生效。
        patch_lines = patch_text.splitlines()  # 将处理后的补丁重新拆分为按行列表。

        patch_base_indent = _detect_base_indent(patch_lines)

        # Step 5: Re-indent the patch to match the file
        reindented_patch = _reindent_patch(patch_lines, patch_base_indent, file_indent_str)

        # Step 6: Verify the patch looks like a valid method replacement
        #   - First line should contain the method name
        #   - Last non-empty line should end with '}'
        patch_first = reindented_patch[0].strip() if reindented_patch else ''
        if method_name not in patch_first and 'void' not in patch_first:
            logger.warning(f"Patch first line doesn't match method: {patch_first[:80]}")

        # Step 7: Write the patched file
        # Keep everything before the method declaration and after the method end
        new_lines = lines[:method_start] + reindented_patch + lines[method_end + 1:]

        patched_content = '\n'.join(new_lines)
        # Preserve original trailing newline
        if original_content.endswith('\n') and not patched_content.endswith('\n'):
            patched_content += '\n'

        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(patched_content)

        # Step 8: Verify the patch was applied correctly
        ok, msg = _verify_patch_applied(test_file, method_name, patch_text)  # 使用实际写入文件的补丁文本再次验证结构和内容。
        if not ok:
            # Restore backup
            logger.warning(f"Patch verification failed: {msg}. Restoring backup.")
            shutil.copy2(backup_path, test_file)
            return False, f"Patch verification failed: {msg}"

        logger.info(f"Patch applied successfully to {method_name} "
                     f"(replaced lines {method_start + 1}-{method_end + 1})")
        return True, "OK"

    except Exception as e:
        # Restore backup on any error
        if os.path.exists(test_file + '.bak'):
            shutil.copy2(test_file + '.bak', test_file)
        return False, f"Exception during patch: {e}"


def restore_backup(test_file: str) -> bool:
    """Restore the test file from its backup."""
    backup_path = test_file + '.bak'
    if os.path.exists(backup_path):
        shutil.copy2(backup_path, test_file)
        return True
    return False


def _normalize_reference_import_lines(import_lines: Tuple[str, ...]) -> Tuple[str, ...]:  # 统一解析参考补丁 import 段里的多种落盘格式。
    normalized_lines = [line.strip() for line in import_lines if line and line.strip() and line.strip() != '[]']  # 先去掉空行和空列表占位。
    if not normalized_lines:  # 没有有效 import 文本时直接返回空元组。
        return ()  # 表示当前候选没有额外 import 上下文。
    import_blob = '\n'.join(normalized_lines).strip()  # 将整个 import 段拼成一个字符串便于统一解析。
    parsed_imports: List[str] = []  # 收集最终归一化后的 import 语句。
    if import_blob.startswith('[') and import_blob.endswith(']'):  # 某些 patch 文件会把 import 列表直接序列化成 Python 列表字符串。
        try:  # 优先按 Python 字面量解析，避免手写切分破坏带逗号的 import 文本。
            literal_value = ast.literal_eval(import_blob)  # 将 `['import ...;']` 解析成真实列表。
        except Exception:  # 列表文本不合法时回退到逐行清洗。
            literal_value = None
        if isinstance(literal_value, (list, tuple)):  # 仅接受列表或元组，避免解析出其他类型。
            for item in literal_value:  # 顺序读取列表中的每一项。
                item_text = str(item).strip()  # 统一规整成字符串。
                if item_text.startswith('import '):  # 只保留真正的 import 语句。
                    parsed_imports.append(item_text)
            if parsed_imports:  # 只要成功解析出 import，就直接返回，避免再走后面的兜底逻辑。
                return tuple(dict.fromkeys(parsed_imports))  # 去重并保持顺序稳定。
    for line in normalized_lines:  # 普通多行格式和解析失败场景都走这里。
        cleaned_line = line.strip().rstrip(',')  # 去掉列表文本可能残留的尾逗号。
        if (cleaned_line.startswith("'") and cleaned_line.endswith("'")) or (cleaned_line.startswith('"') and cleaned_line.endswith('"')):  # 单行列表元素有时还会残留引号。
            cleaned_line = cleaned_line[1:-1].strip()  # 去掉包裹引号并再次规整空白。
        if cleaned_line.startswith('import '):  # 仅保留真正的 import 语句。
            parsed_imports.append(cleaned_line)
    return tuple(dict.fromkeys(parsed_imports))  # 去重并保持首次出现顺序。


def _candidate_uses_risky_json_helpers(test_code: str) -> bool:  # 识别那些依赖额外 JSON helper 或三方库的高风险候选。
    risky_markers = ('assertJsonEqualsNonStrict', 'assertJsonStringEquals', 'assertJSONEqual(', 'assertJsonEquals(', 'assertJsonArrayEquals', 'assertJsonObjectEquals', 'assertThatJson(')  # 这些标记要么像幻觉 helper，要么会额外引入 json-unit 这类更重的上下文。
    return any(marker in (test_code or '') for marker in risky_markers)  # 只有命中明确的幻觉 helper 时才把候选后移。

def _candidate_has_explicit_reference_context(candidate: ReferencePatchCandidate) -> bool:  # 判断候选是否显式给出了 import 或 pom 上下文。
    if tuple(getattr(candidate, 'imports', ()) or ()):  # 只要明确给出了 import 段，就说明这条成功样本提供了可直接复用的上下文。
        return True
    normalized_pom = (getattr(candidate, 'pom_snippet', '') or '').strip()  # 读取并规整 pom 片段。
    return bool(normalized_pom and normalized_pom != 'None' and '<dependency>' in normalized_pom)  # 只有真正声明了 dependency 才算显式上下文。


def _collect_reference_import_lines(candidate: ReferencePatchCandidate, entry: Optional[TestEntry] = None, test_file: str = '') -> List[str]:  # 汇总参考补丁显式给出的 import 与可从候选代码和原始方法签名推断出的 import。
    explicit_imports = _normalize_reference_import_lines(tuple(getattr(candidate, 'imports', ()) or ()))  # 先统一解析候选显式 import 段。
    supplemental_flaky_code = getattr(entry, 'flaky_code', '') if entry and getattr(candidate, 'test_code', '').strip() else ''  # 仅在当前候选真的携带代码片段时才借助原始 flaky 方法补足声明上下文，避免 import-only 候选被额外带偏。
    inferred_imports = _infer_reference_import_lines(test_file, getattr(candidate, 'test_code', ''), supplemental_flaky_code)  # 再结合当前文件和原始 flaky 方法签名推断补丁正文里没有显式出现的异常和类型。
    merged_imports = list(dict.fromkeys(explicit_imports + inferred_imports))  # 先按出现顺序去重，后续再按简单类名解决冲突。
    return _dedupe_import_lines_by_simple_name(merged_imports, test_file, getattr(candidate, 'test_code', '') + '\n' + supplemental_flaky_code)  # 避免一次性带入多个同名不同库的 import。


def _infer_reference_import_lines(test_file: str, *code_fragments: str) -> Tuple[str, ...]:  # 根据参考补丁正文、当前测试文件和原始方法签名共同推断高频第三方 import 与 static import。
    code = '\n'.join(fragment for fragment in code_fragments if fragment)  # 把候选代码和原始 flaky 方法拼成统一信号串，兼顾“保留原声明”后的异常类型。
    inferred_imports: List[str] = []  # 保存按顺序推断出的 import 语句。
    for symbol, import_line in REFERENCE_CODE_IMPORT_INFERENCE.items():  # 逐个检查常见第三方类型符号。
        if re.search(rf'\b{re.escape(symbol)}\b', code):  # 只有代码正文明确出现该符号时才推断 import。
            inferred_imports.append(_resolve_reference_symbol_import_line(test_file, symbol, import_line, code))  # 优先复用当前仓库和当前候选代码自身的线索，避免盲目绑到错误库。
    for symbol in ('Sets', 'Lists', 'Maps', 'Config', 'Option'):  # 这几个符号歧义高，必须走带上下文的解析逻辑。
        if re.search(rf'\b{re.escape(symbol)}\b', code):  # 只有正文真的用到了当前符号时才继续解析。
            resolved_import = _resolve_reference_symbol_import_line(test_file, symbol, '', code)  # 先让已有仓库线索和当前候选代码上下文决定导入路径。
            if resolved_import:  # 只有成功解析出明确导入路径时才补 import。
                inferred_imports.append(resolved_import)  # 记录当前上下文解析出的导入。
    for helper_name, import_line in REFERENCE_CODE_STATIC_IMPORT_INFERENCE.items():  # 再检查高频 helper 的静态导入。
        if _reference_code_uses_static_helper(code, helper_name):  # 仅在代码里真实出现裸 helper 调用时才补对应 static import。
            inferred_imports.append(import_line)  # 记录当前推断出的 static import。
    if re.search(r'(?<![A-Za-z0-9_$.])assertThat\s*\(', code):  # generated_patch 里直接出现裸 `assertThat(...)` 时也要尝试补静态导入。
        import_path = _resolve_missing_method_reference(test_file, 'assertThat') if test_file else None  # 继续沿用已有的 AssertJ/Hamcrest 选择逻辑。
        if import_path:  # 只有仓库证据足够明确时才追加 static import。
            inferred_imports.append(f'import static {import_path};')  # 记录当前推断出的 `assertThat` 静态导入。
    return tuple(import_line for import_line in dict.fromkeys(inferred_imports) if import_line)  # 去重后过滤空值，保持顺序稳定。


def _has_unqualified_helper_call(code: str, helper_name: str) -> bool:  # 判断代码里是否真的出现了裸 helper 调用，而不是 `mocked.when(...)` 这类限定成员调用。
    return bool(re.search(rf'(?<![A-Za-z0-9_$.]){re.escape(helper_name)}\s*\(', code or ''))  # 只接受前面不是标识符、点号或美元符的裸调用。


def _reference_code_uses_static_helper(code: str, helper_name: str) -> bool:  # 统一判断参考代码里是否真实使用了某个 static helper。
    if helper_name in {'when', 'caughtException', 'then', 'assertThatJson'}:  # 这几类 helper 只有裸调用时才应该补 static import。
        return _has_unqualified_helper_call(code, helper_name)  # 过滤掉 `.when(...)`、`.then(...)` 这类成员调用假阳性。
    return bool(re.search(rf'\b{re.escape(helper_name)}\s*\(', code or ''))  # 其余 helper 继续沿用原来的词边界匹配。


def _collect_reference_dependency_snippets(candidate: ReferencePatchCandidate, entry: Optional[TestEntry] = None, import_lines: Tuple[str, ...] = (), repo_dir: str = '', test_file: str = '') -> List[str]:  # 汇总参考补丁显式给出的 dependency 片段与可从候选代码和原始方法签名推断出的测试依赖。
    explicit_snippets = _extract_dependency_snippets(getattr(candidate, 'pom_snippet', ''))  # 先提取候选显式声明的所有 dependency 块。
    supplemental_flaky_code = getattr(entry, 'flaky_code', '') if entry and getattr(candidate, 'test_code', '').strip() else ''  # import-only 候选不再借助 flaky_code 推断额外依赖，避免把无关上下文再次带回。
    inferred_snippets = _infer_reference_dependency_snippets(  # 再根据代码正文和 import 线索推断缺失依赖。
        getattr(candidate, 'test_code', ''),
        tuple(import_lines or tuple(getattr(candidate, 'imports', ()) or ())),  # 优先使用已经按当前文件上下文收敛后的 import 线索，避免被源文件中无关 import 带偏。
        getattr(candidate, 'pom_snippet', ''),
        supplemental_flaky_code,
        repo_dir=repo_dir,
        test_file=test_file,
    )
    merged_snippets: List[str] = []  # 通过依赖坐标做稳定去重。
    seen_keys = set()  # 记录已经收集过的依赖坐标。
    for snippet in explicit_snippets + inferred_snippets:  # 保持“显式优先、推断补足”的顺序。
        dependency_key = _dependency_coordinate_key(snippet) or _normalize_xml_for_match(snippet)  # 优先用 groupId:artifactId 去重，再回退到规范化 XML。
        if dependency_key in seen_keys:  # 相同依赖无需重复保留。
            continue  # 跳过重复 dependency 片段。
        seen_keys.add(dependency_key)  # 记录当前 dependency 已收集。
        merged_snippets.append(snippet.strip())  # 保存当前 dependency 片段。
    return merged_snippets  # 返回最终汇总后的 dependency 列表。


def _extract_dependency_snippets(pom_snippet: str) -> List[str]:  # 从参考补丁的 pom 段中提取全部 dependency XML 片段。
    return [match.group(1).strip() for match in re.finditer(r'(<dependency>[\s\S]*?</dependency>)', pom_snippet or '', re.IGNORECASE)]  # 按出现顺序返回所有命中的 dependency 块。


def _infer_reference_dependency_snippets(test_code: str, import_lines: Tuple[str, ...], pom_snippet: str, supplemental_code: str = '', repo_dir: str = '', test_file: str = '') -> List[str]:  # 根据参考补丁正文、原始方法签名与 import 线索推断常见测试依赖。
    signals = '\n'.join(filter(None, [test_code or '', supplemental_code or '', '\n'.join(import_lines or ()), pom_snippet or '']))  # 把候选代码、原始方法签名和显式上下文一起拼成统一信号串。
    inferred_dependencies: List[str] = []  # 保存按顺序推断出的 dependency 片段。
    dependency_markers = {  # 当前失败集中高频出现的第三方 JSON 相关依赖标记。
        'assertj-core': ('Assertions.', 'org.assertj.core.api.Assertions', 'assertThat('),
        'commons-collections4': ('CollectionUtils', 'org.apache.commons.collections4'),
        'json-path': ('JsonPath', 'DocumentContext', 'ReadContext', 'Configuration', 'Option', 'com.jayway.jsonpath'),
        'jsonassert': ('JSONAssert', 'JSONCompareMode', 'org.skyscreamer.jsonassert'),
        'gson': ('JsonObject', 'JsonParser', 'JsonElement', 'com.google.gson'),
        'jackson-databind': ('JsonNode', 'ObjectMapper', 'JsonProcessingException', 'com.fasterxml.jackson'),
        'json': ('JSONException', 'JSONObject', 'JSONArray', 'org.json.'),
        'json-unit-assertj': ('assertThatJson', 'net.javacrumbs.jsonunit'),
        'guava': ('ImmutableList', 'ImmutableMap', 'ImmutableSet', 'Lists.newArrayList', 'Sets.newHashSet', 'Maps.newHashMap', 'com.google.common.collect'),
        'spring-test': ('MockMvcRequestBuilders', 'MockMvc', 'org.springframework.test'),
        'jackson-dataformat-xml': ('XmlMapper', 'com.fasterxml.jackson.dataformat.xml'),
        'typesafe-config': ('ConfigFactory', 'ConfigResolveOptions', 'com.typesafe.config'),
        'catch-exception': ('com.googlecode.catchexception',),
        'jettison': ('org.codehaus.jettison.json',),
        'hadoop-common': ('SequenceFile.Writer', 'org.apache.hadoop.io', 'SequenceFile$Writer'),
    }  # 用少量高置信度 marker 覆盖当前 v3 剩余失败里最常见的依赖缺口。
    for dependency_key, markers in dependency_markers.items():  # 逐类检查是否命中依赖信号。
        if _signals_require_dependency(dependency_key, signals, markers):  # 只在当前补丁正文真的需要该依赖时才追加。
            inferred_dependencies.append(_dependency_snippet_for_context(dependency_key, repo_dir=repo_dir, test_file=test_file, signals=signals))  # 结合 main/test 源码使用范围决定最终依赖 scope。
    return inferred_dependencies  # 返回推断出的 dependency 片段列表。


def _signals_require_dependency(dependency_key: str, signals: str, markers: Tuple[str, ...]) -> bool:  # 在依赖推断阶段对高噪声依赖做更保守的命中判断。
    normalized_signals = signals or ''  # 统一把空值规整成空串，便于后续复用。
    if dependency_key == 'catch-exception':  # 只有真正出现 catch-exception 的裸 helper 调用时才补这个依赖。
        return 'com.googlecode.catchexception' in normalized_signals or _has_unqualified_helper_call(normalized_signals, 'when') or _has_unqualified_helper_call(normalized_signals, 'caughtException')
    if dependency_key == 'jettison':  # jettison 只在补丁或当前文件显式出现其包名时才补，避免被 `JSONObject`/`JSONException` 误触发。
        return 'org.codehaus.jettison.json' in normalized_signals
    return any(marker in normalized_signals for marker in markers)  # 其余依赖继续沿用原来的 marker 命中逻辑。


def _dedupe_import_lines_by_simple_name(import_lines: List[str], test_file: str, code_fragment: str = '') -> List[str]:  # 在一次批量 import 注入前先解决“同名不同库”的冲突导入。
    grouped_imports: Dict[str, List[str]] = {}  # 按简单类名聚合同一批导入。
    passthrough_imports: List[str] = []  # 保存 static import 与无法解析简单类名的导入。
    for import_line in import_lines:  # 顺序处理当前批次导入。
        simple_name = _import_simple_name(import_line)  # 读取普通 import 对应的简单类名。
        if not simple_name:  # static import 或非标准导入不参与 single-type-import 去重。
            if import_line not in passthrough_imports:  # 保持顺序去重。
                passthrough_imports.append(import_line)  # 直接原样保留。
            continue  # 进入下一条导入。
        grouped_imports.setdefault(simple_name, [])  # 初始化当前简单类名对应的候选列表。
        if import_line not in grouped_imports[simple_name]:  # 保持组内顺序并去重。
            grouped_imports[simple_name].append(import_line)  # 记录当前候选导入。
    resolved_imports: List[str] = []  # 保存最终收敛后的普通 import。
    for simple_name, candidates in grouped_imports.items():  # 逐组解决同名导入冲突。
        if len(candidates) == 1:  # 没有冲突时直接保留。
            resolved_imports.append(candidates[0])  # 记录唯一候选。
            continue  # 继续处理下一组。
        chosen_import = _choose_conflicting_import_line(test_file, code_fragment, simple_name, candidates)  # 结合仓库和当前代码上下文挑选最可信的导入。
        if chosen_import and chosen_import not in resolved_imports:  # 只有成功选出候选时才保留。
            resolved_imports.append(chosen_import)  # 记录当前冲突组的最终导入。
    return resolved_imports + passthrough_imports  # 保持普通 import 在前、static import 在后的稳定输出顺序。


def _choose_conflicting_import_line(test_file: str, code_fragment: str, simple_name: str, candidates: List[str]) -> str:  # 在多个同名不同库的导入之间选择最可信的那个。
    resolved_reference = _resolve_missing_symbol_reference(test_file, simple_name) if test_file else None  # 优先尊重当前文件和仓库里已存在的稳定导入证据。
    if resolved_reference and resolved_reference[1]:  # 仓库已经给出了唯一真实导入时直接采用。
        target_import = f'import {resolved_reference[1]};'  # 将解析出的路径转成标准 import 行。
        if target_import in candidates:  # 只有当前冲突组里确实包含该导入时才采用。
            return target_import  # 返回仓库证据支持的唯一导入。
    normalized_code = (code_fragment or '').lower()  # 统一转小写，便于根据代码语义做二次判断。
    if simple_name == 'JsonParser':  # 当前失败集里最常见的同名冲突是 Gson 与 Jackson 的 `JsonParser`。
        gson_markers = ('jsonobject', 'jsonelement', 'getasjson', 'parsestring(', 'new jsonparser(', '.parse(')  # 这些调用更像 Gson 语义。
        jackson_markers = ('jsonfactory', 'jsontoken', 'deserializationcontext', 'serializerprovider')  # 这些调用更像 Jackson core 语义。
        if any(marker in normalized_code for marker in gson_markers):  # 代码正文明显更像 Gson 时优先保留 Gson 导入。
            for candidate in candidates:  # 顺序寻找 Gson 导入。
                if 'com.google.gson.JsonParser' in candidate:  # 命中 Gson 导入时直接返回。
                    return candidate
        if any(marker in normalized_code for marker in jackson_markers):  # 代码正文明显更像 Jackson core 时优先保留 Jackson 导入。
            for candidate in candidates:  # 顺序寻找 Jackson 导入。
                if 'com.fasterxml.jackson.core.JsonParser' in candidate:  # 命中 Jackson core 导入时直接返回。
                    return candidate
    return candidates[0]  # 其余场景保持保守，沿用当前顺序中的第一个候选。


def _dependency_snippet_for_context(dependency_key: str, repo_dir: str = '', test_file: str = '', signals: str = '') -> str:  # 基于当前仓库上下文和源码使用范围为依赖片段选择合适 scope。
    base_snippet = REFERENCE_DEPENDENCY_SNIPPETS[dependency_key]  # 先读取当前依赖的标准片段模板。
    desired_scope = _infer_dependency_scope(repo_dir=repo_dir, test_file=test_file, dependency_key=dependency_key, signals=signals)  # 再判断当前依赖应该使用 test 还是 compile scope。
    return _replace_dependency_scope(base_snippet, desired_scope)  # 返回调整过 scope 的最终 dependency 片段。


def _infer_dependency_scope(repo_dir: str, test_file: str, dependency_key: str, signals: str = '') -> str:  # 判断当前依赖应以 test 还是 compile scope 注入到目标模块 pom。
    compile_scope_keys = {'guava'}  # 当前 v8 里真正暴露出 main source compile 受 scope 影响的高频依赖。
    if dependency_key not in compile_scope_keys:  # 其余依赖默认仍保持 test scope，避免扩大影响面。
        return 'test'
    package_markers = {  # 将依赖键映射到可在 main source 中检索的稳定包名前缀。
        'guava': ('com.google.common.',),
    }
    markers = package_markers.get(dependency_key, ())  # 读取当前依赖对应的包标记。
    if _repo_main_source_uses_markers(repo_dir, markers, exclude_test_file=test_file):  # 只要主源码本身也依赖该包，就应该升级为 compile scope。
        return 'compile'
    return 'test'


def _repo_main_source_uses_markers(repo_dir: str, markers: Tuple[str, ...], exclude_test_file: str = '') -> bool:  # 扫描仓库 main source，判断当前依赖是否已经被主源码真实使用。
    if not repo_dir or not markers:  # 缺少仓库根目录或检索标记时无法继续。
        return False  # 返回假以保持调用方逻辑简单。
    normalized_exclude = os.path.abspath(exclude_test_file) if exclude_test_file else ''  # 避免误扫当前测试文件。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物与无关目录。
    for root, dirs, files in os.walk(repo_dir):  # 遍历当前仓库目录树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 就地裁剪无关目录以减少扫描开销。
        if f'{os.sep}src{os.sep}main{os.sep}' not in root:  # 只扫描 main source 目录。
            continue  # 跳过测试源码和其他无关目录。
        for filename in files:  # 遍历当前目录下的文件。
            if not filename.endswith('.java'):  # 当前只需要检查 Java 源码引用。
                continue  # 非 Java 文件无需处理。
            file_path = os.path.abspath(os.path.join(root, filename))  # 拼出当前源文件绝对路径。
            if normalized_exclude and file_path == normalized_exclude:  # 当前测试文件不应该参与 main source 判定。
                continue  # 跳过当前文件。
            try:  # 单个文件读取失败时直接跳过即可。
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取源码。
                    content = f.read()  # 读取完整文件文本用于包名前缀检索。
            except Exception:  # 某个文件读取失败不影响整体扫描。
                continue  # 继续扫描其他文件。
            if any(marker in content for marker in markers):  # 主源码中命中包名前缀时说明 compile scope 才是正确选择。
                return True  # 立即返回真，避免继续无谓扫描。
    return False  # 扫描完仍未命中时说明 test scope 已足够。


def _replace_dependency_scope(dependency_snippet: str, scope: str) -> str:  # 将标准 dependency 片段中的 scope 调整为指定值。
    normalized_scope = (scope or 'test').strip() or 'test'  # 空值统一回退到 test scope。
    if re.search(r'<scope>\s*.+?\s*</scope>', dependency_snippet or '', re.IGNORECASE):  # 片段中已有 scope 时直接替换。
        return re.sub(r'<scope>\s*.+?\s*</scope>', f'<scope>{normalized_scope}</scope>', dependency_snippet, flags=re.IGNORECASE)  # 保留其余 XML 结构不变。
    return dependency_snippet  # 没有 scope 标签时保持原样，避免擅自改写非常规片段。


def apply_reference_patch_context(repo_dir: str, entry: TestEntry, test_file: str, candidate: ReferencePatchCandidate) -> Tuple[bool, str]:  # 将“已知成功代码片段里显式或可推断的上下文”同步应用到工作区。
    change_messages = []  # 收集本次上下文补齐的摘要说明，便于写入失败诊断。
    import_lines = _collect_reference_import_lines(candidate, entry, test_file)  # 汇总显式 import 与从参考补丁正文、原始方法声明推断出的 import。
    if import_lines:  # 只要存在 import 上下文就尝试同步到目标测试文件。
        if not backup_file(test_file):  # 目标测试文件缺少备份时无法安全回退。
            return False, f"Failed to backup test file before applying reference imports: {os.path.basename(test_file)}"
        imported, import_msg = apply_import_context(test_file, import_lines)  # 将候选 import 合并到当前测试文件。
        if not imported:  # import 应用失败时直接返回，让上层继续尝试其他候选。
            return False, import_msg
        if import_msg:  # 只有真正发生变化时才记录摘要。
            change_messages.append(import_msg)
    dependency_snippets = _collect_reference_dependency_snippets(candidate, entry, tuple(import_lines), repo_dir=repo_dir, test_file=test_file)  # 汇总显式与推断出的 dependency 片段。
    if dependency_snippets:  # 只要存在依赖上下文就尝试同步到目标模块 pom。
        pom_file = find_module_pom(repo_dir, getattr(entry, 'module', '.'))  # 优先定位当前目标模块的 pom。
        if not pom_file:  # 找不到 pom 时无法继续注入依赖。
            return False, "Reference patch requires additional dependency but target pom.xml was not found"
        if not backup_file(pom_file):  # 确保 pom 也具备可恢复基线。
            return False, f"Failed to backup pom.xml before applying reference dependency: {pom_file}"
        dependency_messages = []  # 收集每个 dependency 注入的结果摘要。
        for dependency_snippet in dependency_snippets:  # 顺序应用每个需要的 dependency 片段。
            pom_applied, pom_msg = apply_dependency_snippet_to_pom(pom_file, dependency_snippet)  # 将单个 dependency 片段插入当前模块 pom。
            if not pom_applied:  # 任一依赖注入失败都说明当前候选上下文不完整。
                return False, pom_msg
            if pom_msg:  # 只有真正发生变化时才记录摘要。
                dependency_messages.append(pom_msg)
        if dependency_messages:  # 至少有一个 dependency 被成功补入时记录到总摘要。
            change_messages.extend(dependency_messages)
    return True, '; '.join(change_messages) if change_messages else "Reference patch context already satisfied"


def apply_generated_patch_context(repo_dir: str, entry: TestEntry, test_file: str) -> Tuple[bool, str]:  # 根据当前被评估的 generated_patch 本身推断 import 与 pom 上下文，不依赖任何外部参考补丁库。
    generated_patch = getattr(entry, 'generated_patch', '').strip()  # 读取当前案例真正要被评估的生成补丁。
    if not generated_patch:  # 没有生成补丁时就没有可推断的上下文。
        return False, 'Generated patch is empty, no context to infer'
    synthetic_candidate = ReferencePatchCandidate(source_path='generated_patch', test_code=generated_patch)  # 用当前补丁正文构造一个仅供上下文推断的临时候选。
    context_ok, context_msg = apply_reference_patch_context(repo_dir, entry, test_file, synthetic_candidate)  # 复用已经验证过的 import/pom 注入逻辑。
    if not context_msg:  # 没有额外说明时直接返回原结果。
        return context_ok, context_msg
    return context_ok, context_msg.replace('Reference patch context', 'Generated patch context')  # 把底层通用 helper 的消息改写成当前真实语义。


def backport_fixed_sha_test_helpers(repo_dir: str, entry: TestEntry, test_file: str, build_output: str, git_timeout: int = 300, git_retries: int = 1) -> Tuple[bool, str]:  # 当 generated_patch 依赖 fixed_sha 才引入的测试 helper、field 或 import 时，把最小必需上下文迁回 original_sha 工作区。
    missing_method_refs = _extract_missing_method_references(build_output, test_file)  # 同时保留方法名和编译器 location 指向的实际类名，便于处理限定静态调用。
    missing_variables = _extract_missing_variables(build_output, test_file)  # 继续提取当前目标测试文件缺失的字段或常量，覆盖 JSON_MAPPER 一类场景。
    missing_classes = _extract_missing_classes(build_output, test_file)  # 最后提取当前目标测试文件缺失的类名，用于 fixed_sha 同文件 import 回补。
    if not missing_method_refs and not missing_variables and not missing_classes:  # 没有任何可从 fixed_sha 学习的上下文时无需继续。
        return False, 'No reusable fixed_sha context found in build output'
    fixed_sha = getattr(entry, 'fixed_sha', '').strip()  # 读取当前样本可追溯的修复提交号。
    if not fixed_sha:  # 没有 fixed_sha 时无法回溯 helper 定义。
        return False, 'No fixed_sha available for helper backport'
    revision_ok, revision_msg = ensure_revision_available(repo_dir, fixed_sha, timeout=git_timeout, max_retries=git_retries)  # 确保 fixed_sha 已经在本地对象库可读。
    if not revision_ok:  # revision 仍然不可读时无法继续回溯 helper。
        return False, f'Fixed-SHA helper lookup failed: {revision_msg}'
    target_rel_path = os.path.relpath(test_file, repo_dir).replace(os.sep, '/')  # 计算目标测试文件相对仓库根目录的路径，便于去 fixed_sha 中读取同路径文件。
    file_contents: Dict[str, str] = {}  # 按绝对路径缓存已读取的目标文件内容，支持同一轮把成员分别迁回多个测试文件。
    backported_candidates: List[Tuple[ReferencePatchCandidate, str]] = []  # 收集这次真正迁回来的候选以及对应的目标文件，上下文注入阶段会继续复用。
    backport_messages = []  # 收集每个回补动作的来源摘要，最终写回错误消息头部。
    for method_ref in missing_method_refs:  # 顺序处理当前编译错误里暴露出的每个缺失 helper。
        helper_candidate = _find_fixed_sha_helper_candidate(repo_dir, entry, fixed_sha, target_rel_path, method_ref.method_name, git_timeout, preferred_owner_class=method_ref.owner_class_name)  # 去 fixed_sha 中寻找最可信的 helper 定义来源。
        if helper_candidate is None:  # fixed_sha 里也没找到时，把机会留给后续真正的失败诊断。
            continue  # 当前 helper 无法回溯，继续处理下一个。
        target_member_file = _resolve_backport_target_file(repo_dir, test_file, helper_candidate, target_rel_path, method_ref.owner_class_name)  # 根据调用形态和 fixed_sha 来源文件决定 original_sha 中真正该写回哪个测试文件。
        current_content, read_error = _read_backport_target_content(target_member_file, file_contents)  # 读取并缓存当前目标文件内容。
        if read_error:  # 目标文件无法读取时只能跳过当前候选。
            continue
        if _file_contains_method_definition(current_content, method_ref.method_name):  # 当前工作区里如果已经有了这个 helper，就不重复追加。
            continue  # 跳过已满足的方法定义。
        if target_member_file not in file_contents and not backup_file(target_member_file):  # 第一次修改该文件前先保存原始基线。
            return False, f"Failed to backup file before fixed_sha helper backport: {os.path.basename(target_member_file)}"
        current_content, inserted = _append_helper_method_to_file(current_content, helper_candidate.test_code, method_ref.method_name)  # 把 helper 定义追加回真正的目标类末尾。
        if not inserted:  # 追加失败或当前方法已经存在时跳过。
            continue  # 不把这个候选计入真正的 backport。
        file_contents[target_member_file] = current_content  # 缓存更新后的目标文件内容，后续统一落盘。
        backported_candidates.append((helper_candidate, target_member_file))  # 记录这次真的迁回来的 helper 候选及其目标文件。
        backport_messages.append(f"{method_ref.method_name}<-{helper_candidate.source_path}")  # 保存方法名与来源路径，便于最终诊断。
    for variable_name in missing_variables:  # 顺序处理当前目标测试文件缺失的字段或常量。
        current_content, read_error = _read_backport_target_content(test_file, file_contents)  # 字段仍然只回补到目标测试文件本身。
        if read_error:  # 无法读取目标测试文件时直接终止。
            return False, read_error
        if _file_contains_field_definition(current_content, variable_name):  # 当前工作区里如果已经有了同名字段，就不重复追加。
            continue  # 跳过已满足的字段定义。
        field_candidate = _find_fixed_sha_field_candidate(repo_dir, entry, fixed_sha, target_rel_path, variable_name, git_timeout)  # 去 fixed_sha 中寻找最可信的字段定义来源。
        if field_candidate is None:  # fixed_sha 里找不到同名字段时继续让后续真实诊断处理。
            continue  # 当前字段无法回溯，继续处理下一个。
        if test_file not in file_contents and not backup_file(test_file):  # 第一次修改目标测试文件前先保存原始基线。
            return False, f"Failed to backup file before fixed_sha field backport: {os.path.basename(test_file)}"
        current_content, inserted = _append_class_member_to_file(current_content, field_candidate.test_code)  # 把字段定义追加回当前测试类末尾。
        if not inserted:  # 追加失败或当前字段已经存在时跳过。
            continue  # 不把这个候选计入真正的 backport。
        file_contents[test_file] = current_content  # 缓存更新后的目标测试文件内容。
        backported_candidates.append((field_candidate, test_file))  # 记录这次真的迁回来的字段候选及其目标文件。
        backport_messages.append(f"{variable_name}<-{field_candidate.source_path}")  # 保存字段名与来源路径，便于最终诊断。
    import_candidate = _find_fixed_sha_import_candidate(repo_dir, fixed_sha, target_rel_path, missing_classes, git_timeout)  # 从 fixed_sha 同文件里挑出当前真正缺失的类导入。
    if import_candidate is not None:  # 只有 fixed_sha 同文件明确给出了所需导入时才继续。
        backported_candidates.append((import_candidate, test_file))  # 记录这次真的迁回来的 import 候选。
        imported_names = [name for name in missing_classes if any(_import_simple_name(line) == name for line in import_candidate.imports)]  # 只记录当前真正命中的导入名称。
        if imported_names:  # 只有确实命中了缺失类名时才写回消息。
            backport_messages.append(f"imports:{','.join(imported_names)}<-{import_candidate.source_path}")  # 标记这是一次 fixed_sha import 回补，而不是成员迁移。
    if not backported_candidates:  # 没有任何 helper、field 或 import 被真正迁回时直接返回。
        return False, 'No reusable fixed_sha context found'
    for file_path, content in file_contents.items():  # 将本轮真正发生变化的文件统一落回工作区。
        with open(file_path, 'w', encoding='utf-8') as f:  # 以 UTF-8 写回目标文件。
            f.write(content)  # 写入新的测试文件内容。
    context_messages = []  # 收集 helper 自身需要的 import 和 pom 上下文补齐记录。
    for helper_candidate, target_context_file in backported_candidates:  # 顺序把每个 helper、field 或 import 候选依赖的 import 和 dependency 一起补回对应工作区文件。
        context_ok, context_msg = apply_reference_patch_context(repo_dir, entry, target_context_file, helper_candidate)  # 复用统一的 import/pom 注入逻辑，不引入新的“替代补丁”语义。
        if not context_ok:  # import 或 pom 注入失败时直接上报。
            return False, context_msg
        if context_msg and context_msg != 'Reference patch context already satisfied':  # 只有真的发生变化时才保留消息。
            context_messages.append(context_msg.replace('Reference patch context', 'Fixed-SHA helper context'))  # 把底层消息前缀改成当前语义。
    message_parts = [f"Backported fixed_sha context: {', '.join(backport_messages)}"]  # 先写 fixed_sha 上下文来源摘要。
    if context_messages:  # helper 额外触发了 import 或 pom 补齐时继续追加。
        message_parts.extend(context_messages)  # 保留依赖上下文变化信息，便于最终复盘。
    return True, '; '.join(message_parts)  # 返回本轮 helper 回溯成功摘要。

def _read_backport_target_content(file_path: str, file_contents: Dict[str, str]) -> Tuple[str, str]:  # 读取 fixed_sha 回补目标文件内容，并复用本轮内存缓存。
    if file_path in file_contents:  # 当前文件本轮已经被读取或修改过时直接复用缓存。
        return file_contents[file_path], ''  # 返回缓存内容。
    try:  # 首次读取该文件时从工作区磁盘加载。
        with open(file_path, 'r', encoding='utf-8') as f:  # 以 UTF-8 读取工作区目标文件。
            return f.read(), ''  # 返回原始文件内容。
    except Exception as e:  # 某些目标文件在 original_sha 中不存在时返回明确错误。
        return '', f'Failed to read fixed_sha backport target file: {e}'


def _resolve_backport_target_file(repo_dir: str, test_file: str, candidate: ReferencePatchCandidate, target_rel_path: str, owner_class_name: str = '') -> str:  # 根据 fixed_sha 候选来源和缺失方法的 owner 类决定 original_sha 里真正该修改哪个文件。
    candidate_rel_path = (getattr(candidate, 'destination_rel_path', '') or _relative_path_from_fixed_sha_source(candidate.source_path)).strip()  # 优先使用候选显式给出的目标路径。
    if candidate_rel_path:  # fixed_sha 来源路径可用时优先直接映射回 original_sha。
        return os.path.join(repo_dir, candidate_rel_path.replace('/', os.sep))  # 返回对应的工作区目标文件绝对路径。
    if owner_class_name and owner_class_name != os.path.splitext(os.path.basename(target_rel_path))[0]:  # 编译器 location 明确指向了另一个类时，也尝试按同包同名文件推断。
        guessed_rel_path = '/'.join(target_rel_path.split('/')[:-1] + [f'{owner_class_name}.java'])  # 保持原包路径，仅替换目标类文件名。
        guessed_abs_path = os.path.join(repo_dir, guessed_rel_path.replace('/', os.sep))  # 拼出对应工作区目标文件路径。
        if os.path.isfile(guessed_abs_path):  # 只有 original_sha 中真实存在该文件时才采用。
            return guessed_abs_path  # 返回限定静态 helper 真正应该写回的目标文件。
    return test_file  # 其余场景默认仍然回补到当前目标测试文件。


def _relative_path_from_fixed_sha_source(source_path: str) -> str:  # 从 `fixed_sha:path/to/File.java` 形式的来源字符串中提取相对路径。
    if not source_path or not source_path.startswith('fixed_sha:'):  # 非 fixed_sha 候选没有可直接映射的相对路径。
        return ''  # 返回空串让调用方继续走其他推断。
    return source_path.split(':', 1)[1].strip()  # 返回 fixed_sha 候选文件的相对路径部分。


def _find_fixed_sha_helper_candidate(repo_dir: str, entry: TestEntry, fixed_sha: str, target_rel_path: str, method_name: str, git_timeout: int, preferred_owner_class: str = '') -> Optional[ReferencePatchCandidate]:  # 按“目标类 -> 同包 -> 同模块测试树 -> 全仓测试树”的顺序在 fixed_sha 里找最可信的 helper 定义。
    visited_paths = set()  # 记录已经扫描过的文件，避免同一文件被多个前缀重复返回。
    for relative_path in _iter_fixed_sha_helper_candidate_paths(repo_dir, entry, fixed_sha, target_rel_path, git_timeout):  # 顺序遍历优先级已排好的 fixed_sha 候选文件。
        normalized_path = relative_path.strip()  # 统一去掉可能残留的空白。
        if not normalized_path or normalized_path in visited_paths:  # 空路径或重复路径都没有继续价值。
            continue  # 跳过当前候选。
        if preferred_owner_class and os.path.basename(normalized_path) != f'{preferred_owner_class}.java' and normalized_path == target_rel_path:  # 限定静态调用优先避免再次回到当前测试文件本身。
            pass  # 保留当前候选继续参与排序，只是不在这里提前过滤。
        visited_paths.add(normalized_path)  # 记录当前文件已经扫描过。
        file_ok, file_content = read_file_at_revision(repo_dir, fixed_sha, normalized_path, timeout=git_timeout)  # 读取 fixed_sha 下当前候选文件的完整内容。
        if not file_ok or not file_content or f'{method_name}(' not in file_content:  # 文件读取失败或根本不含目标方法名时无需继续解析。
            continue  # 跳过当前候选文件。
        helper_method = _extract_non_test_method_from_content(file_content, method_name)  # 从当前文件里抽取真正的 helper 方法定义。
        if not helper_method:  # 没有找到可安全迁回的 helper 定义时继续试下一个文件。
            continue  # 跳到下一候选。
        return _build_fixed_sha_member_candidate(f'fixed_sha:{normalized_path}', file_content, helper_method, destination_rel_path=normalized_path)  # 返回当前最可信的 fixed_sha helper 候选，并只保留 snippet 真正需要的 import。
    return None  # 全部候选路径都找不到可用 helper 时返回空值。


def _find_fixed_sha_field_candidate(repo_dir: str, entry: TestEntry, fixed_sha: str, target_rel_path: str, field_name: str, git_timeout: int) -> Optional[ReferencePatchCandidate]:  # 按“同文件 -> 同目录 -> 同模块测试树”的顺序在 fixed_sha 里找最可信的字段或常量定义。
    visited_paths = set()  # 记录已经扫描过的文件，避免同一文件被多个前缀重复返回。
    for relative_path in _iter_fixed_sha_helper_candidate_paths(repo_dir, entry, fixed_sha, target_rel_path, git_timeout):  # 复用与 helper 相同的固定搜索顺序。
        normalized_path = relative_path.strip()  # 统一去掉可能残留的空白。
        if not normalized_path or normalized_path in visited_paths:  # 空路径或重复路径都没有继续价值。
            continue  # 跳过当前候选。
        visited_paths.add(normalized_path)  # 记录当前文件已经扫描过。
        file_ok, file_content = read_file_at_revision(repo_dir, fixed_sha, normalized_path, timeout=git_timeout)  # 读取 fixed_sha 下当前候选文件的完整内容。
        if not file_ok or not file_content or field_name not in file_content:  # 文件读取失败或根本不含目标字段名时无需继续解析。
            continue  # 跳过当前候选文件。
        field_definition = _extract_top_level_field_from_content(file_content, field_name)  # 从当前文件里抽取真正的字段定义。
        if not field_definition:  # 没有找到可安全迁回的字段定义时继续试下一个文件。
            continue  # 跳到下一候选。
        return _build_fixed_sha_member_candidate(f'fixed_sha:{normalized_path}', file_content, field_definition, destination_rel_path=target_rel_path)  # 返回当前最可信的 fixed_sha 字段候选，并只保留 snippet 真正需要的 import。
    return None  # 全部候选路径都找不到可用字段时返回空值。


def _find_fixed_sha_import_candidate(repo_dir: str, fixed_sha: str, target_rel_path: str, missing_classes: List[str], git_timeout: int) -> Optional[ReferencePatchCandidate]:  # 从 fixed_sha 同文件里提取当前目标测试真正缺失的类导入。
    if not missing_classes:  # 没有缺失类名时无需继续。
        return None  # 返回空值表示当前轮不需要 import 回补。
    file_ok, file_content = read_file_at_revision(repo_dir, fixed_sha, target_rel_path, timeout=git_timeout)  # 优先读取 fixed_sha 下同一路径的测试文件。
    if not file_ok or not file_content:  # 同文件读取失败时当前轮直接放弃 import 回补。
        return None  # 返回空值让其余诊断继续。
    import_lines = tuple(  # 只保留 fixed_sha 同文件里真正匹配当前缺失类名的 import。
        line for line in re.findall(r'^\s*import\s+.+?;\s*$', file_content, re.MULTILINE)
        if _import_simple_name(line) in set(missing_classes)
    )
    if not import_lines:  # fixed_sha 同文件里也没有这些缺失类名的显式导入时无需继续。
        return None  # 返回空值表示当前轮没有学到新的 import 上下文。
    return ReferencePatchCandidate(source_path=f'fixed_sha:{target_rel_path}', test_code='', imports=import_lines, destination_rel_path=target_rel_path)  # 返回一个仅承载 import 的候选，后续复用统一上下文注入逻辑。

def _build_fixed_sha_member_candidate(source_path: str, file_content: str, member_code: str, destination_rel_path: str = '') -> ReferencePatchCandidate:  # 根据 fixed_sha 源文件和待迁回成员构造只保留最小上下文的候选。
    import_lines = _filter_relevant_import_lines(tuple(re.findall(r'^\s*import\s+.+?;\s*$', file_content, re.MULTILINE)), member_code)  # 只保留当前成员真正引用到的 import，避免 cloudstack 一类整文件 import 误迁回。
    return ReferencePatchCandidate(source_path=source_path, test_code=member_code, imports=import_lines, destination_rel_path=destination_rel_path)  # 返回裁剪过 import 的成员候选。


def _iter_fixed_sha_helper_candidate_paths(repo_dir: str, entry: TestEntry, fixed_sha: str, target_rel_path: str, git_timeout: int):  # 生成 fixed_sha helper 检索时的候选文件顺序。
    files_ok, files_or_message = list_files_at_revision(repo_dir, fixed_sha, '', timeout=git_timeout)  # 直接列出当前 revision 下的全部文件，再由本地排序逻辑统一决定优先级。
    if not files_ok:  # 列树失败时直接结束，让上层看到“未找到 helper”。
        return  # 当前 revision 无法列树时不再继续。
    target_dir = os.path.dirname(target_rel_path).replace(os.sep, '/')  # 当前目标测试文件所在目录。
    target_filename = os.path.basename(target_rel_path)  # 当前目标测试文件名。
    module_prefix = '' if not getattr(entry, 'module', '') or entry.module == '.' else entry.module.strip('/').replace(os.sep, '/')  # 统一规整模块前缀。
    candidate_paths: List[Tuple[Tuple[int, int, int, str], str]] = []  # 保存排序键与候选文件路径。
    for relative_path in files_or_message:  # 遍历 fixed_sha 下的所有文件。
        normalized_path = relative_path.strip().replace(os.sep, '/')  # 统一路径分隔符。
        if not normalized_path.endswith('.java'):  # helper 回退当前只处理 Java 测试源码。
            continue  # 过滤掉无关文件。
        if not any(f'/{test_dir}/' in f'/{normalized_path}' for test_dir in FIXED_SHA_HELPER_SEARCH_DIRS):  # 只保留测试源码树下的文件。
            continue  # 跳过 main source 与其他无关文件。
        exact_target = 0 if normalized_path == target_rel_path else 1  # 目标测试文件本身优先级最高。
        same_dir = 0 if target_dir and normalized_path.startswith(f'{target_dir}/') else 1  # 同包其他测试文件排在后面。
        same_module = 0 if module_prefix and normalized_path.startswith(f'{module_prefix}/') else 1  # 同模块测试树优先于仓库其他模块。
        nested_bonus = normalized_path.count(f'/{target_filename}')  # 同名测试文件在嵌套模块里也应优先靠前。
        sort_key = (exact_target, same_dir, same_module, f'{nested_bonus}:{normalized_path}')  # 保持“目标文件 -> 同包 -> 同模块 -> 其他测试树”的稳定顺序。
        candidate_paths.append((sort_key, normalized_path))  # 记录当前候选路径及其排序键。
    for _, normalized_path in sorted(candidate_paths, key=lambda item: item[0]):  # 按排序键稳定输出候选文件。
        yield normalized_path  # 把当前候选文件路径交给上层逐个解析。


def _extract_non_test_method_from_content(content: str, method_name: str) -> str:  # 从 fixed_sha 文件内容里抽出真正的 helper 方法定义，并尽量避开测试方法本身。
    lines = (content or '').splitlines()  # 按行拆分文件内容，复用现有方法定位与大括号匹配逻辑。
    candidates = _find_method_declaration_candidates(lines, method_name)  # 先收集所有同名方法声明位置。
    for method_start in candidates:  # 顺序尝试每个同名方法。
        method_end = _find_method_end(lines, method_start)  # 找到当前方法的结束行，便于提取完整源码。
        if method_end is None:  # 结构损坏的方法定义不应被迁回。
            continue  # 尝试下一个候选。
        prefix_start = _extend_method_start_to_annotations(lines, method_start)  # 把紧邻方法头的注解与注释一起保留下来。
        method_text = '\n'.join(lines[prefix_start:method_end + 1])  # 提取完整方法源码。
        if '@Test' in method_text or '@ParameterizedTest' in method_text:  # 只迁回 helper，不迁回另一个测试方法。
            continue  # 继续寻找真正的 helper 定义。
        return method_text  # 返回当前找到的 helper 方法。
    return ''  # 未找到可安全迁回的 helper 时返回空串。


def _extend_method_start_to_annotations(lines: List[str], method_start: int) -> int:  # 把方法前紧邻的注解与注释一起纳入 helper 迁移范围。
    prefix_start = method_start  # 默认只从方法声明行开始。
    while prefix_start > 0:  # 向上回溯直到遇到非注解/非注释行。
        previous_line = lines[prefix_start - 1].strip()  # 读取上一行并去掉空白。
        if not previous_line:  # 空行说明注解块已经结束。
            break  # 停止继续向上回溯。
        if previous_line.startswith('@') or previous_line.startswith('*') or previous_line.startswith('/**') or previous_line.startswith('/*'):  # 这些都属于方法声明前的注解或文档注释。
            prefix_start -= 1  # 把当前行纳入方法前缀。
            continue  # 继续向上回溯。
        break  # 遇到普通代码行时停止。
    return prefix_start  # 返回最终应纳入的起始行。


def _file_contains_method_definition(content: str, method_name: str) -> bool:  # 判断当前文件里是否已经定义了同名方法，避免重复迁回 helper。
    return bool(_find_method_declaration_candidates((content or '').splitlines(), method_name))  # 只要能定位到同名方法声明就视为已经存在。


def _append_helper_method_to_file(content: str, helper_method: str, method_name: str) -> Tuple[str, bool]:  # 把 helper 方法追加到当前测试类末尾，保持 generated_patch 本身不变。
    normalized_helper = (helper_method or '').strip()  # 先规整待追加的方法文本。
    if not normalized_helper or _file_contains_method_definition(content, method_name):  # 空 helper 或当前文件已经有定义时都无需继续。
        return content, False  # 返回原文并说明没有发生变化。
    return _append_class_member_to_file(content, normalized_helper)  # 复用统一的类成员追加逻辑。


def _file_contains_field_definition(content: str, field_name: str) -> bool:  # 判断当前文件里是否已经定义了同名字段或常量，避免重复迁回 fixed_sha 字段。
    return bool(_extract_top_level_field_from_content(content, field_name))  # 只要能在顶层类成员里提取到该字段就视为已经存在。


def _append_class_member_to_file(content: str, member_code: str) -> Tuple[str, bool]:  # 把字段或 helper 方法安全地追加到当前测试类的顶层类体末尾。
    normalized_member = (member_code or '').strip()  # 先规整待追加的类成员源码。
    if not normalized_member:  # 空成员源码时无需继续。
        return content, False  # 返回原文并说明没有发生变化。
    stripped_content = (content or '').rstrip()  # 去掉尾部空白，便于准确找到顶层类的最后一个右大括号。
    closing_index = _find_top_level_class_closing_brace(stripped_content)  # 只接受顶层类真正的闭合大括号，避免被内部类或匿名块误导。
    if closing_index == -1:  # 找不到类结束位置时无法安全追加成员。
        return content, False  # 返回原文，让上层保留当前失败状态。
    prefix = stripped_content[:closing_index].rstrip()  # 取出类闭合大括号前的全部正文。
    suffix = stripped_content[closing_index:]  # 保留最终类闭合大括号。
    updated_content = prefix + '\n\n' + normalized_member + '\n' + suffix  # 在类结束前插入成员，并保留一个空行分隔。
    if content.endswith('\n') and not updated_content.endswith('\n'):  # 尽量保持原文件的换行风格。
        updated_content += '\n'  # 补回尾部换行。
    return updated_content, updated_content != content  # 返回更新后的文件内容和是否真的发生变化。


def _filter_relevant_import_lines(import_lines: Tuple[str, ...], code_fragment: str) -> Tuple[str, ...]:  # 只保留当前 helper 或字段真正引用到的 import，避免把整文件无关依赖一并迁回。
    normalized_code = code_fragment or ''  # 统一规整为空串，便于后续匹配。
    filtered_imports = []  # 保存当前成员真正依赖的 import。
    for import_line in import_lines:  # 逐条检查 fixed_sha 源文件里的 import。
        stripped_line = (import_line or '').strip()  # 去掉首尾空白便于解析。
        if not stripped_line.startswith('import '):  # 非 import 行无需处理。
            continue  # 跳过无关文本。
        simple_name = _import_simple_name(stripped_line)  # 普通 import 时读取简单类名。
        if simple_name and re.search(rf'\b{re.escape(simple_name)}\b', normalized_code):  # 当前成员真正引用到了该简单类名时才保留。
            filtered_imports.append(stripped_line)  # 记录当前必需的普通 import。
            continue  # 当前 import 已命中，无需再尝试 static import 逻辑。
        static_match = re.match(r'^\s*import\s+static\s+([A-Za-z0-9_.]*\.([A-Za-z_][A-Za-z0-9_]*))\s*;\s*$', stripped_line)  # 再检查是否为 static import。
        if static_match and re.search(rf'\b{re.escape(static_match.group(2))}\b', normalized_code):  # 当前成员真正引用到了该静态方法或字段时才保留。
            filtered_imports.append(stripped_line)  # 记录当前必需的 static import。
    return tuple(dict.fromkeys(filtered_imports))  # 去重并保持原始顺序稳定。


def _find_top_level_class_closing_brace(content: str) -> int:  # 找到顶层类真正的闭合大括号位置，避免简单 rfind 被内部类或注释误导。
    class_decl_match = re.search(r'^\s*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+|static\s+)*(?:class|interface|enum|record)\s+\w+', content or '', re.MULTILINE)  # 先找到顶层类型声明起点，避免被文件头或注解参数里的大括号干扰。
    if not class_decl_match:  # 无法定位顶层类型声明时直接返回失败。
        return -1
    opening_index = content.find('{', class_decl_match.end())  # 仅从顶层类型声明之后寻找类体起始大括号。
    if opening_index == -1:  # 顶层类型没有类体时无法安全追加成员。
        return -1
    depth = 1  # 从顶层类体起始大括号后开始计数。
    index = opening_index + 1  # 从顶层类体内部继续扫描。
    while index < len(content):  # 顺序扫描原始源码文本。
        ch = content[index]  # 读取当前字符。
        if ch == '/' and index + 1 < len(content) and content[index + 1] == '*':  # 跳过块注释。
            end = content.find('*/', index + 2)
            index = len(content) if end == -1 else end + 2
            continue
        if ch == '/' and index + 1 < len(content) and content[index + 1] == '/':  # 跳过行注释。
            end = content.find('\n', index + 2)
            index = len(content) if end == -1 else end + 1
            continue
        if content.startswith('"""', index):  # Java text block 需要整体跳过，避免其中的大括号影响类体深度。
            end = content.find('"""', index + 3)
            index = len(content) if end == -1 else end + 3
            continue
        if ch == '"':  # 跳过普通字符串字面量。
            index = _skip_string(content, index, '"')
            continue
        if ch == "'":  # 跳过字符字面量。
            index = _skip_string(content, index, "'")
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:  # 回到 0 时说明刚好走出了顶层类体。
                return index
        index += 1
    return -1  # 扫描结束仍未找到完整顶层类时返回失败。


def _extract_top_level_field_from_content(content: str, field_name: str) -> str:  # 从 fixed_sha 文件内容里抽出顶层类上的字段或常量定义。
    lines = (content or '').splitlines()  # 按行拆分文件内容，便于结合大括号深度判断顶层成员。
    brace_depth = 0  # 记录当前行扫描前所在的大括号深度。
    for index, raw_line in enumerate(lines):  # 顺序扫描源码中的每一行。
        sanitized_line = _strip_strings_and_comments_full(raw_line)  # 去掉字符串与注释，避免局部文本误导模式匹配。
        current_depth = brace_depth  # 当前行开始前的深度就代表这行属于哪一级类体。
        brace_depth += sanitized_line.count('{') - sanitized_line.count('}')  # 更新下一行会看到的深度。
        if current_depth != 1:  # 只在顶层类体里寻找字段定义，跳过方法体和内部类。
            continue  # 当前行不属于顶层类成员区域。
        if not re.search(rf'\b{re.escape(field_name)}\b', raw_line):  # 行里不含目标字段名时无需继续。
            continue  # 跳过当前行。
        candidate_start = _extend_member_start_to_annotations(lines, index)  # 把字段前紧邻的注解与注释一起保留下来。
        candidate_lines = []  # 收集字段定义直到分号结束。
        for end_index in range(index, len(lines)):  # 向下扩展直到遇到字段定义结束。
            candidate_lines.append(lines[end_index])  # 保留当前字段定义的原始行。
            candidate_text = '\n'.join(candidate_lines)  # 拼接当前已收集的字段定义片段。
            if re.search(rf'\b{re.escape(field_name)}\s*\(', candidate_text):  # 命中方法声明时说明这不是字段而是方法。
                break  # 当前候选不是字段，停止继续扩展。
            if ';' in _strip_strings_and_comments_full(lines[end_index]):  # 普通字段定义在顶层类里以分号结束。
                return '\n'.join(lines[candidate_start:end_index + 1])  # 返回完整字段定义源码。
            if '{' in _strip_strings_and_comments_full(lines[end_index]):  # 字段定义里若直接进入代码块，通常说明这不是普通字段。
                break  # 避免把初始化块或内部类误当成字段。
    return ''  # 未找到可安全迁回的字段定义时返回空串。


def _extend_member_start_to_annotations(lines: List[str], member_start: int) -> int:  # 把字段或方法前紧邻的注解与注释一起纳入迁移范围。
    prefix_start = member_start  # 默认只从成员声明行开始。
    while prefix_start > 0:  # 向上回溯直到遇到非注解/非注释行。
        previous_line = lines[prefix_start - 1].strip()  # 读取上一行并去掉空白。
        if not previous_line:  # 空行说明注解块已经结束。
            break  # 停止继续向上回溯。
        if previous_line.startswith('@') or previous_line.startswith('*') or previous_line.startswith('/**') or previous_line.startswith('/*'):  # 这些都属于成员声明前的注解或文档注释。
            prefix_start -= 1  # 把当前行纳入成员前缀。
            continue  # 继续向上回溯。
        break  # 遇到普通代码行时停止。
    return prefix_start  # 返回最终应纳入的起始行。


def apply_import_context(test_file: str, import_lines: List[str]) -> Tuple[bool, str]:  # 将参考补丁给出的 import 语句幂等地插入目标测试文件。
    try:  # 读取失败时直接返回明确错误。
        with open(test_file, 'r', encoding='utf-8') as f:  # 读取当前测试文件内容。
            original_content = f.read()  # 保存原文以便判断是否真正发生修改。
    except Exception as e:  # 文件读取失败时返回错误。
        return False, f"Failed to read test file for reference imports: {e}"
    normalized_imports = [line.strip() for line in import_lines if line.strip().startswith('import ')]  # 仅保留规范化后的 import 语句。
    if not normalized_imports:  # 没有任何有效 import 时无需继续。
        return True, ''  # 视为上下文已经满足。
    updated_content = _insert_import_lines(original_content, normalized_imports)  # 将 import 片段幂等插入文件头部。
    if updated_content == original_content:  # 所有 import 都已存在时无需重写文件。
        return True, ''  # 返回空摘要表示本轮没有新增内容。
    with open(test_file, 'w', encoding='utf-8') as f:  # 将更新后的文件内容写回。
        f.write(updated_content)  # 落盘参考补丁提供的 import 上下文。
    return True, f"Applied reference imports: {', '.join(normalized_imports)}"


def find_module_pom(repo_dir: str, module: str) -> Optional[str]:  # 根据模块名定位当前案例最相关的 pom.xml。
    normalized_module = module if module and module != '.' else '.'  # 空模块统一视为仓库根目录。
    if normalized_module != '.':  # 多模块项目优先使用目标模块自己的 pom。
        module_pom = os.path.join(repo_dir, normalized_module, 'pom.xml')  # 拼出目标模块 pom 路径。
        if os.path.isfile(module_pom):  # 命中模块 pom 时直接返回。
            return module_pom
    root_pom = os.path.join(repo_dir, 'pom.xml')  # 回退到仓库根 pom。
    if os.path.isfile(root_pom):  # 根 pom 存在时返回。
        return root_pom
    return None  # 当前仓库不存在可用 pom 时返回空值。


def apply_dependency_snippet_to_pom(pom_file: str, pom_snippet: str) -> Tuple[bool, str]:  # 将参考补丁里的依赖片段幂等插入目标 pom.xml。
    dependency_match = re.search(r'(<dependency>[\s\S]*?</dependency>)', pom_snippet or '', re.IGNORECASE)  # 从原始说明文本中提取标准 dependency 片段。
    if not dependency_match:  # 没有标准 dependency 片段时无需继续。
        return True, ''  # 视为当前候选不需要实际修改 pom。
    dependency_snippet = dependency_match.group(1).strip()  # 提取真正要写入 pom 的 dependency XML。
    try:  # 读取 pom 失败时返回错误。
        with open(pom_file, 'r', encoding='utf-8') as f:  # 读取目标 pom。
            original_content = f.read()  # 保存原文以便判断是否真正发生修改。
    except Exception as e:  # 文件读取失败时直接返回错误。
        return False, f"Failed to read pom.xml for reference dependency: {e}"
    dependency_key = _dependency_coordinate_key(dependency_snippet)  # 读取当前 dependency 的 groupId:artifactId 标识。
    if dependency_key and _pom_contains_dependency(original_content, dependency_key):  # pom 中已存在同坐标依赖时无需重复写入。
        return True, ''  # 返回空摘要表示当前依赖已经满足。
    if _normalize_xml_for_match(dependency_snippet) in _normalize_xml_for_match(original_content):  # 精确片段已存在时同样无需再次写入。
        return True, ''  # 避免重复注入同一 dependency 片段。
    updated_content = _insert_dependency_into_pom(original_content, dependency_snippet)  # 将依赖插入现有 pom。
    if updated_content == original_content:  # 插入失败时返回显式错误，避免静默继续。
        return False, f"Failed to insert reference dependency into {os.path.basename(pom_file)}"
    with open(pom_file, 'w', encoding='utf-8') as f:  # 将修改后的 pom 写回磁盘。
        f.write(updated_content)  # 落盘新的依赖片段。
    return True, f"Applied reference dependency in {os.path.basename(pom_file)}: {dependency_key or 'custom dependency'}"


def fix_unreported_exception_declaration(test_file: str, method_name: str, build_output: str) -> Tuple[bool, str]:  # 根据编译日志中的 checked exception 提示补齐目标方法的 throws 声明。
    exception_fqcn, error_line_number = _extract_unreported_exception_context(build_output, test_file)  # 从构建输出中提取异常全限定名和最可信的报错行号。
    if not exception_fqcn:  # 没有 unreported exception 提示时无需处理。
        return False, "No unreported checked exception found in build output"
    try:  # 读取失败时直接返回错误。
        with open(test_file, 'r', encoding='utf-8') as f:  # 读取当前测试文件。
            original_content = f.read()  # 保存原文以便后续判断是否发生修改。
    except Exception as e:  # 文件读取失败时返回明确错误。
        return False, f"Failed to read test file for checked exception repair: {e}"
    lines = original_content.splitlines()  # 将源码按行拆分，便于复用现有的方法定位逻辑。
    method_start = _find_method_declaration(lines, method_name, '')  # 先按目标测试方法名定位当前测试方法。
    if error_line_number is not None:  # 构建日志给出具体报错行时，再尝试按实际报错位置回推所在方法。
        line_based_method_start = _find_enclosing_method_declaration(lines, error_line_number - 1)  # `javac` 行号从 1 开始，这里转换成 0-based 下标。
        if line_based_method_start is not None:  # 报错行成功映射到某个具体方法时优先使用它。
            method_start = line_based_method_start  # 这样可以覆盖方法名解析失准或错误发生在同文件另一方法内的场景。
    if method_start is None:  # 找不到目标方法时无法安全修改 throws 声明。
        return False, f"Method '{method_name}' not found for checked exception repair"
    original_method = _extract_method_text(lines, method_start)  # 提取完整方法源码以便重写方法头。
    if not original_method:  # 无法提取完整方法时直接失败。
        return False, f"Method '{method_name}' could not be extracted for checked exception repair"
    method_end = _find_method_end(lines, method_start)  # 获取方法结束位置，后续替换整段方法源码。
    if method_end is None:  # 没有闭合大括号时无法安全重写方法。
        return False, f"Could not determine end of method '{method_name}' for checked exception repair"
    parts = _split_method_prefix_and_body(original_method)  # 拆分出方法头和方法体。
    if parts is None:  # 无法安全拆分方法体时不继续处理。
        return False, f"Method '{method_name}' could not be split for checked exception repair"
    original_prefix, method_body = parts  # 读取方法声明和方法体。
    updated_prefix = _append_exception_to_method_prefix(original_prefix, exception_fqcn)  # 将异常全限定名追加到 throws 声明中。
    if updated_prefix == original_prefix:  # 方法头没有发生变化时说明当前异常已经被声明。
        return False, f"Checked exception already declared: {exception_fqcn}"
    updated_method = updated_prefix + method_body  # 拼回更新后的完整方法源码。
    updated_lines = lines[:method_start] + updated_method.splitlines() + lines[method_end + 1:]  # 用新方法替换原始方法段。
    updated_content = '\n'.join(updated_lines)  # 重新拼接完整文件内容。
    if original_content.endswith('\n') and not updated_content.endswith('\n'):  # 尽量保持原文件换行风格。
        updated_content += '\n'
    with open(test_file, 'w', encoding='utf-8') as f:  # 将更新后的文件写回。
        f.write(updated_content)  # 落盘新的 throws 声明。
    return True, f"Declared checked exception on {method_name}: {exception_fqcn}"


def fix_missing_imports(test_file: str, build_output: str) -> Tuple[bool, str]:  # 根据编译错误信息为 Java 测试文件自动补充缺失 import 与高置信度符号修复。
    try:
        with open(test_file, 'r', encoding='utf-8') as f:  # 读取当前测试文件内容。
            original_content = f.read()  # 保留原内容以便判断是否真正发生修改。
    except Exception as e:  # 读取失败时直接返回错误。
        return False, f"Failed to read test file for import fix: {e}"  # 返回读取失败原因。

    missing_symbols = _extract_missing_symbols(build_output, test_file)  # 只提取当前目标测试文件对应的缺失符号名。
    missing_methods = _extract_missing_methods(build_output, test_file)  # 同时提取当前目标测试文件对应的缺失方法名。
    if not missing_symbols and not missing_methods:  # 没有检测到任何可修复的缺失符号或方法时直接结束。
        return False, "No fixable missing symbols or methods found in build output"  # 返回没有可修复问题的信息。

    updated_content = original_content  # 从原始文件内容开始累积本轮自动修复修改。
    imports_to_add = []  # 收集需要新增的普通 import 语句。
    static_imports_to_add = []  # 收集需要新增的 static import 语句。
    replacement_messages = []  # 收集本轮发生的符号替换说明。
    qualified_method_messages = []  # 收集通过现有断言类限定符完成的方法修复说明。
    for symbol in missing_symbols:  # 逐个处理缺失的符号名。
        resolved_symbol = _resolve_missing_symbol_reference(test_file, symbol)  # 优先尝试解析安全的类名修正与 import 路径。
        if not resolved_symbol:  # 无法安全解析当前符号时跳过。
            continue  # 保留当前编译错误给后续真实失败诊断。
        actual_symbol, import_path = resolved_symbol  # 读取修正后的实际符号名与可选 import 路径。
        if actual_symbol != symbol:  # 当缺失符号只是大小写或近似类名错误时尝试直接修正源码里的引用。
            if not _is_safe_symbol_case_replacement(symbol, actual_symbol, test_file, import_path):  # 仅允许明显属于类型名且来源可信的大小写修正，避免把局部变量误改成类名。
                continue  # 对不安全的大小写修正直接放弃，避免生成新的编译错误。
            updated_content, replacement_count = _replace_symbol_tokens(updated_content, symbol, actual_symbol)  # 将源码中的错误符号名替换为解析出的真实符号名。
            if replacement_count > 0:  # 只有真的改动了文件时才记录替换说明。
                replacement_messages.append(f'{symbol}->{actual_symbol}')  # 记录当前符号替换关系便于日志输出。
        if not import_path:  # 没有 import 路径时说明当前修复只需要做符号替换或同包类无需 import。
            continue  # 跳过 import 收集逻辑。
        import_line = f'import {import_path};'  # 生成标准 Java import 行。
        if import_line in updated_content or import_line in imports_to_add:  # 已存在对应 import 或已经计划插入时无需重复添加。
            continue  # 跳过重复 import。
        imports_to_add.append(import_line)  # 将待新增普通 import 收集起来。

    for method_name in missing_methods:  # 逐个处理缺失的方法名。
        updated_content, qualification_msg = _qualify_missing_method_reference(updated_content, method_name)  # 先尝试基于文件中已存在的断言类导入做安全限定符修复。
        if qualification_msg:  # 当前缺失方法已经通过限定符修复时无需继续追加 static import。
            qualified_method_messages.append(qualification_msg)  # 记录当前方法限定符修复详情。
            continue  # 进入下一个缺失方法。
        import_path = _resolve_missing_method_reference(test_file, method_name)  # 优先利用仓库内现有 static import 线索再回退到内置高置信度映射。
        if not import_path:  # 未收录的方法名不做不安全猜测。
            continue  # 继续处理下一个缺失方法。
        import_line = f'import static {import_path};'  # 生成 Java static import 行。
        if import_line in updated_content or import_line in static_imports_to_add:  # 已存在或已计划插入时无需重复添加。
            continue  # 跳过重复 static import。
        static_imports_to_add.append(import_line)  # 收集待新增 static import。

    if not imports_to_add and not static_imports_to_add and not replacement_messages and not qualified_method_messages:  # 没有任何真实修复动作时直接返回失败。
        return False, "Missing symbols detected but no safe import or symbol fixes available"  # 明确告知当前错误仍无法安全自动修复。

    updated_content = _insert_import_lines(updated_content, imports_to_add + static_imports_to_add)  # 将普通 import 与 static import 一次性插入文件头部。
    if updated_content == original_content:  # 所有修复尝试都未产生实际文件改动时无需落盘。
        return False, "Import fix produced no file changes"  # 返回无变化说明。

    with open(test_file, 'w', encoding='utf-8') as f:  # 将修复后的内容写回测试文件。
        f.write(updated_content)  # 覆盖写入新文件内容。
    change_messages = []  # 按顺序拼接本轮自动修复的摘要说明。
    if imports_to_add:  # 只有新增普通 import 时才追加对应摘要。
        change_messages.append(f"Added imports: {', '.join(imports_to_add)}")  # 记录新增普通 import 列表。
    if static_imports_to_add:  # 只有新增 static import 时才追加对应摘要。
        change_messages.append(f"Added static imports: {', '.join(static_imports_to_add)}")  # 记录新增 static import 列表。
    if replacement_messages:  # 只有发生源码符号替换时才追加对应摘要。
        change_messages.append(f"Replaced symbols: {', '.join(replacement_messages)}")  # 记录本轮发生的符号纠正关系。
    if qualified_method_messages:  # 只有发生方法限定符修复时才追加对应摘要。
        change_messages.append(f"Qualified methods: {', '.join(qualified_method_messages)}")  # 记录本轮通过现有断言类完成的方法修复。
    return True, '; '.join(change_messages)  # 返回成功信息与本轮具体修复动作。


def fix_related_test_imports(repo_dir: str, target_test_file: str, build_output: str, max_files: int = 12) -> Tuple[bool, str]:  # 当模块内其他测试文件也被 test-compile 连带卡住时，尝试对这些非目标测试文件做同样的保守 import 修复。
    related_test_files = _extract_related_test_error_files(repo_dir, target_test_file, build_output)  # 先从当前构建输出里找出真正报错的非目标测试文件。
    if not related_test_files:  # 没有命中任何可修复的非目标测试文件时直接返回。
        return False, 'No related test files require import repair'
    change_messages = []  # 收集每个非目标测试文件上真正发生的修复动作。
    repaired_count = 0  # 记录本轮真正被修复的文件数量。
    for related_test_file in related_test_files[:max_files]:  # 限制单轮最多处理的文件数，避免大模块一次改动过多。
        if not backup_file(related_test_file):  # 第一次修改该文件前先保存原始基线。
            continue  # 某个文件无法备份时直接跳过，不阻断其他文件修复。
        repaired, repair_msg = fix_missing_imports(related_test_file, build_output)  # 直接复用现有的保守 import 修复逻辑。
        if not repaired:  # 当前文件没有安全修复动作时继续处理下一个。
            continue
        repaired_count += 1  # 记录当前文件已经被成功修复。
        change_messages.append(f'{os.path.relpath(related_test_file, repo_dir)}: {repair_msg}')  # 保留相对路径和具体修复摘要，便于最终诊断。
    if repaired_count == 0:  # 没有任何非目标测试文件被真正修改时直接返回失败。
        return False, 'Related test files found but no safe import fixes were available'
    return True, ' | '.join(change_messages)  # 返回所有非目标测试文件的修复摘要。


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_method_declaration_candidates(lines: List[str], method_name: str) -> List[int]:  # 收集当前文件中所有同名方法声明位置，供后续做相似度选择与唯一性判断。
    sig_pattern = re.compile(rf'\b{re.escape(method_name)}\s*\(')  # 只在声明上下文里匹配目标方法名。
    candidates = []  # 保存所有通过声明检查的同名方法位置。
    for i, line in enumerate(lines):  # 逐行扫描整个文件。
        if not sig_pattern.search(line):  # 当前行没有出现目标方法名时无需继续判断。
            continue  # 跳过当前行。
        if _is_method_declaration(lines, i, method_name):  # 只有真正的方法声明才加入候选。
            candidates.append(i)  # 记录当前候选方法起始行。
    return candidates  # 返回当前文件中所有同名方法声明位置。


def _find_method_declaration(lines: List[str], method_name: str,
                             reference_code: str = '') -> Optional[int]:  # 用参考代码在多候选场景中选择最匹配的方法。
    """Find the line index of the method declaration.

    Handles various Java method declaration styles:
    - public void testFoo() {
    - public void testFoo() throws Exception {
    - void testFoo()
    -   throws Exception {    (multi-line signature)
    """
    candidates = _find_method_declaration_candidates(lines, method_name)  # 先收集当前文件中的所有同名方法声明位置。

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    if reference_code:  # 优先使用原 flaky 方法文本在多个候选之间做判别。
        scored_candidates = []  # 保存候选方法及其相似度分数。
        for idx in candidates:  # 遍历每个可能的方法声明位置。
            method_text = _extract_method_text(lines, idx)  # 提取当前候选方法的完整源码。
            scored_candidates.append((idx, _method_similarity(method_text, reference_code)))  # 记录相似度供排序使用。
        scored_candidates.sort(key=lambda item: item[1], reverse=True)  # 让最相似的方法排在前面。
        best_idx, best_score = scored_candidates[0]  # 取相似度最高的候选。
        logger.info(f"Multiple declarations found for '{method_name}', chose similarity={best_score:.2f}")  # 记录多候选选择结果。
        return best_idx  # 返回最像原 flaky 方法的那个候选。

    # Multiple candidates: prefer the one with @Test annotation nearby
    for idx in candidates:
        # Check lines above for @Test
        for j in range(max(0, idx - 5), idx):
            if '@Test' in lines[j] or '@org.junit' in lines[j]:
                return idx

    # Fall back to first candidate
    logger.warning(f"Multiple declarations found for '{method_name}', using first match")
    return candidates[0]


def _is_method_declaration(lines: List[str], line_idx: int, method_name: str) -> bool:
    """Check if the line at line_idx is a method declaration (not a call)."""
    line = lines[line_idx].strip()

    # Method declarations have modifiers or return types before the method name
    decl_pattern = re.compile(
        rf'^((@\w+[\s\S]*?\s+)?(public|protected|private)\s+)?'
        rf'(static\s+)?(final\s+)?(synchronized\s+)?'
        rf'(\w[\w<>\[\],\s]*?\s+)'
        rf'{re.escape(method_name)}\s*\(',
        re.DOTALL
    )

    if decl_pattern.search(line):
        return True

    # Also check for simple patterns like "void methodName("
    simple_pattern = re.compile(
        rf'^\s*(public|protected|private|static|final|synchronized|void|\w+)\s+'
        rf'.*{re.escape(method_name)}\s*\('
    )
    if simple_pattern.match(line):
        return True

    # Check if there's @Test or @Before etc. annotation in preceding lines
    for j in range(max(0, line_idx - 5), line_idx):
        prev = lines[j].strip()
        if prev.startswith('@') and any(kw in prev for kw in
                                         ['Test', 'Before', 'After', 'Setup', 'Ignore']):
            return True

    return False


def _pick_best_test_file(candidates: List[str], entry: TestEntry) -> Optional[str]:  # 综合路径、方法名和 flaky 代码为候选文件打分。
    unique_candidates = []  # 使用列表保留原始顺序，便于稳定排序。
    seen = set()  # 用集合去重避免重复扫描同一文件。
    for candidate in candidates:  # 遍历所有候选文件。
        normalized = os.path.abspath(candidate)  # 统一为绝对路径便于判重。
        if normalized in seen:  # 同一个文件只保留一次。
            continue  # 跳过重复项。
        seen.add(normalized)  # 记录已见过的文件。
        unique_candidates.append(normalized)  # 追加到去重后的候选列表。
    if not unique_candidates:  # 没有任何候选时直接返回空值。
        return None  # 交由上层输出未找到文件。
    scored = [(_score_test_file(path, entry), path) for path in unique_candidates]  # 计算每个候选文件的综合分数。
    scored.sort(key=lambda item: item[0], reverse=True)  # 让得分最高的候选排在最前面。
    best_score, best_path = scored[0]  # 读取最优候选。
    if best_score <= 0:  # 分数过低说明所有候选都缺乏可信证据。
        return None  # 避免在低置信度情况下盲目选择。
    return best_path  # 返回最优文件路径。


def _score_test_file(path: str, entry: TestEntry) -> float:  # 通过文件路径和内容为候选测试文件评分。
    score = 0.0  # 初始化基础分数。
    normalized_path = path.replace(os.sep, '/')  # 统一路径分隔符以便做后缀比较。
    if normalized_path.endswith(entry.class_path):  # 类路径后缀精确匹配最可信。
        score += 5.0  # 给予较高基础分。
    if '/src/test/' in normalized_path or normalized_path.endswith('/test'):  # 测试源码目录更符合预期。
        score += 1.0  # 给予目录结构奖励分。
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:  # 读取候选文件内容做细粒度评分。
            content = f.read()  # 获取完整文件文本。
    except Exception:  # 读取失败时保留已有路径分数。
        return score  # 返回当前已有分数。
    if entry.test_method in content:  # 文件中包含目标方法名时提升分数。
        score += 1.5  # 方法名命中说明候选更可信。
    lines = content.splitlines()  # 按行切分，便于复用已有方法提取逻辑。
    method_start = _find_method_declaration(lines, entry.test_method, entry.flaky_code)  # 尝试定位目标方法。
    if method_start is not None:  # 只有找到了方法声明才进行进一步比较。
        score += 2.0  # 成功找到方法本身就说明候选较强。
        method_text = _extract_method_text(lines, method_start)  # 提取完整方法文本。
        score += _method_similarity(method_text, entry.flaky_code) * 5.0  # 用与 flaky 代码的相似度进一步拉开差距。
    return score  # 返回最终综合得分。


def _extract_method_text(lines: List[str], method_start: int) -> str:  # 从方法起始行提取完整源码文本。
    method_end = _find_method_end(lines, method_start)  # 先找到方法的闭合大括号位置。
    if method_end is None:  # 找不到闭合位置时返回空串。
        return ''  # 上层会将其视为低置信度候选。
    return '\n'.join(lines[method_start:method_end + 1])  # 返回完整方法源码。


def _preserve_original_declaration(original_method: str, patch_text: str) -> Tuple[str, bool]:  # 在补丁只应改方法体时保留原始方法声明。
    original_parts = _split_method_prefix_and_body(original_method)  # 先拆出原始方法的方法头与方法体。
    patch_parts = _split_method_prefix_and_body(patch_text)  # 再拆出生成补丁的方法头与方法体。
    if original_parts is None or patch_parts is None:  # 任一侧无法拆分出方法体时不做强制修补。
        return patch_text, False  # 保持原补丁不变以免误伤特殊格式。
    original_prefix, _ = original_parts  # 读取原始方法声明前缀。
    patch_prefix, patch_body = patch_parts  # 读取补丁方法声明前缀与方法体部分。
    merged_prefix = _merge_method_declarations(original_prefix, patch_prefix)  # 保留原始方法头，同时吸收补丁新增的 throws 信息。
    if _normalize_code_for_match(merged_prefix) == _normalize_code_for_match(patch_prefix):  # 合并后如果与补丁本身一致，则无需改写。
        return patch_text, False  # 直接返回原补丁文本。
    return merged_prefix + patch_body, True  # 使用合并后的方法头并拼接补丁方法体。


def _split_method_prefix_and_body(method_text: str) -> Optional[Tuple[str, str]]:  # 按首个方法体左大括号切分方法声明和主体。
    brace_index = method_text.find('{')  # Java 方法声明后的首个左大括号通常就是方法体起点。
    if brace_index == -1:  # 没有方法体大括号时无法安全切分。
        return None  # 返回空值让上层跳过声明保护。
    return method_text[:brace_index], method_text[brace_index:]  # 返回大括号前的方法声明与包含大括号的方法体。


def _merge_method_declarations(original_prefix: str, patch_prefix: str) -> str:  # 合并原始方法头与补丁方法头的安全差异。
    patch_throws = _extract_throws_clause(patch_prefix)  # 提取补丁方法头里的 throws 声明。
    if not patch_throws:  # 补丁没有新增 throws 时直接保留原始方法头。
        return original_prefix  # 返回原始方法头以避免签名被 LLM 擅自改动。
    original_throws = _extract_throws_clause(original_prefix)  # 提取原始方法头里的 throws 声明。
    if original_throws == patch_throws:  # 两边 throws 一致时无需额外处理。
        return original_prefix  # 直接保留原始方法头。
    stripped_original = original_prefix.rstrip()  # 先去掉结尾空白，便于做安全替换和追加。
    trailing_whitespace = original_prefix[len(stripped_original):]  # 保存原始结尾空白以尽量维持排版风格。
    if original_throws:  # 原始方法本身存在 throws 时用补丁的 throws 子句替换之。
        replaced_prefix = re.sub(r'\bthrows\b[\s\S]*$', f'throws {patch_throws}', stripped_original)  # 保留原始签名主体，只更新异常声明。
        return replaced_prefix + trailing_whitespace  # 将原始结尾空白补回去，避免破坏换行样式。
    return stripped_original + f' throws {patch_throws}' + trailing_whitespace  # 在原始方法声明末尾追加补丁新增的异常声明。


def _extract_throws_clause(method_prefix: str) -> str:  # 提取方法声明中的 throws 子句内容。
    match = re.search(r'\bthrows\b\s+([\w\s,.$<>?&]+)\s*$', method_prefix.strip(), re.DOTALL)  # 匹配 throws 后直到声明结尾的异常列表。
    if not match:  # 没有 throws 子句时返回空串。
        return ''  # 交由上层按“无 throws”处理。
    return re.sub(r'\s+', ' ', match.group(1)).strip()  # 规范化异常列表中的空白，便于比较与替换。


def _method_similarity(left: str, right: str) -> float:  # 使用规范化文本相似度衡量两个方法是否为同一逻辑。
    if not left or not right:  # 任一侧为空时无法给出可靠分数。
        return 0.0  # 返回最低分。
    normalized_left = _normalize_code_for_match(left)  # 去掉空白差异以减少格式噪声。
    normalized_right = _normalize_code_for_match(right)  # 对参考代码做同样规范化。
    if not normalized_left or not normalized_right:  # 规范化后为空时视为无法比较。
        return 0.0  # 返回最低分。
    return difflib.SequenceMatcher(None, normalized_left, normalized_right).ratio()  # 返回 0 到 1 之间的相似度。


def _normalize_code_for_match(code: str) -> str:  # 规范化源码文本以便进行稳健比较。
    stripped = _strip_strings_and_comments_full(code)  # 先移除注释和字符串，降低无关差异影响。
    return re.sub(r'\s+', '', stripped)  # 再压缩所有空白得到稳定的比较串。


def _find_method_end(lines: List[str], start_idx: int) -> Optional[int]:
    """Find the closing brace of a method starting at start_idx.

    Uses brace counting with proper handling of strings, chars, and comments.
    Handles multi-line method signatures (where '{' is on a later line).
    """
    brace_count = 0
    found_open = False
    in_block_comment = False
    in_text_block = False

    for i in range(start_idx, len(lines)):
        line = lines[i]
        j = 0
        while j < len(line):
            if in_text_block:  # Java text block 可能跨多行，必须在逐行扫描时保留状态。
                end_text_block = line.find('"""', j)
                if end_text_block == -1:
                    break  # 当前行剩余部分仍属于 text block。
                j = end_text_block + 3
                in_text_block = False
                continue
            # Handle block comments
            if in_block_comment:
                end_comment = line.find('*/', j)
                if end_comment == -1:
                    break  # rest of line is comment
                j = end_comment + 2
                in_block_comment = False
                continue

            ch = line[j]

            # Start of block comment
            if ch == '/' and j + 1 < len(line) and line[j + 1] == '*':
                in_block_comment = True
                j += 2
                continue

            # Line comment
            if ch == '/' and j + 1 < len(line) and line[j + 1] == '/':
                break  # rest of line is comment

            # String literal
            if line.startswith('"""', j):
                in_text_block = True
                j += 3
                continue
            if ch == '"':
                j = _skip_string(line, j, '"')
                continue

            # Char literal
            if ch == "'":
                j = _skip_string(line, j, "'")
                continue

            if ch == '{':
                brace_count += 1
                found_open = True
            elif ch == '}':
                brace_count -= 1
                if found_open and brace_count == 0:
                    return i

            j += 1

    return None


def _skip_string(line: str, start: int, quote_char: str) -> int:
    """Skip a string/char literal starting at position start. Returns next position."""
    i = start + 1
    while i < len(line):
        if line[i] == '\\':
            i += 2  # skip escaped char
            continue
        if line[i] == quote_char:
            return i + 1
        i += 1
    return len(line)


def _detect_base_indent(patch_lines: List[str]) -> int:
    """Detect the base indentation level of the patch.

    The base indent is the indentation of the first non-empty line
    (usually the method signature).
    """
    for line in patch_lines:
        stripped = line.lstrip()
        if stripped:
            return len(line) - len(stripped)
    return 0


def _reindent_patch(patch_lines: List[str], patch_base_indent: int,
                    file_indent_str: str) -> List[str]:
    """Re-indent patch lines to match the file's indentation.

    Args:
        patch_lines: Lines of the generated patch.
        patch_base_indent: The base indentation of the patch (number of spaces).
        file_indent_str: The indentation string used in the file for this method.

    Returns:
        List of re-indented lines.
    """
    result = []
    for line in patch_lines:
        if not line.strip():
            result.append('')
            continue

        # Calculate relative indentation from patch base
        current_indent = len(line) - len(line.lstrip())
        relative_indent = max(0, current_indent - patch_base_indent)

        # Apply file indentation + relative indentation
        result.append(file_indent_str + ' ' * relative_indent + line.lstrip())

    return result


def _verify_patch_applied(test_file: str, method_name: str,
                          generated_patch: str) -> Tuple[bool, str]:
    """Verify that the patch was applied correctly.

    Checks:
    1. The file is valid (no obvious syntax issues)
    2. The method exists in the patched file
    3. Key code from the patch is present
    """
    try:
        with open(test_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check 1: Method name exists
        if method_name not in content:
            return False, f"Method name '{method_name}' not found after patch"

        lines = content.splitlines()  # 将文件按行拆分以便重新定位补丁后的方法。
        method_start = _find_method_declaration(lines, method_name, generated_patch)  # 使用生成补丁辅助选中正确的方法定义。
        if method_start is None:  # 没有找到目标方法时直接失败。
            return False, f"Method declaration '{method_name}' not found after patch"  # 明确说明失败原因。
        patched_method = _extract_method_text(lines, method_start)  # 提取补丁后的完整方法源码。
        patch_similarity = _method_similarity(patched_method, generated_patch)  # 比较补丁后的方法与目标补丁的一致性。
        if patch_similarity < 0.70:  # 相似度过低时说明很可能改错了方法或内容被破坏。
            return False, f"Patched method similarity too low ({patch_similarity:.2f})"  # 返回低相似度错误信息。

        # Check 2: Brace balance in the whole file
        brace_count = 0
        for ch in _strip_strings_and_comments_full(content):
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
            if brace_count < 0:
                return False, "Unbalanced braces (too many closing braces)"
        if brace_count != 0:
            return False, f"Unbalanced braces (count={brace_count})"

        # Check 3: Key tokens from the patch exist in the file
        # Extract identifiers from the patch that should be present
        patch_stripped = generated_patch.strip()
        # Check that at least the first meaningful line of the patch body is present
        patch_body_lines = [l.strip() for l in patch_stripped.splitlines() if l.strip()]
        if len(patch_body_lines) > 1:
            # Check second line (first line of body, after method signature)
            second_line = patch_body_lines[1]
            # Normalize for comparison
            if second_line not in content and len(second_line) > 10:
                # Try more lenient check
                tokens = re.findall(r'\w+', second_line)
                unique_tokens = [t for t in tokens if len(t) > 3]
                if unique_tokens:
                    found = sum(1 for t in unique_tokens if t in content)
                    if found < len(unique_tokens) * 0.5:
                        return False, "Key patch tokens not found in output"

        return True, "OK"

    except Exception as e:
        return False, f"Verification error: {e}"


def _strip_strings_and_comments_full(content: str) -> str:
    """Remove all string literals and comments from Java source for brace counting."""
    result = []
    i = 0
    length = len(content)

    while i < length:
        ch = content[i]

        # Block comment
        if ch == '/' and i + 1 < length and content[i + 1] == '*':
            end = content.find('*/', i + 2)
            i = end + 2 if end != -1 else length
            continue

        # Line comment
        if ch == '/' and i + 1 < length and content[i + 1] == '/':
            end = content.find('\n', i)
            i = end + 1 if end != -1 else length
            continue

        # String literal
        if content.startswith('"""', i):  # Java text block 需要整体跳过，避免其中的大括号干扰 brace 计数。
            end = content.find('"""', i + 3)
            i = end + 3 if end != -1 else length
            continue
        if ch == '"':
            i += 1
            while i < length:
                if content[i] == '\\':
                    i += 2
                    continue
                if content[i] == '"':
                    i += 1
                    break
                i += 1
            continue

        # Char literal
        if ch == "'":
            i += 1
            while i < length:
                if content[i] == '\\':
                    i += 2
                    continue
                if content[i] == "'":
                    i += 1
                    break
                i += 1
            continue

        result.append(ch)
        i += 1

    return ''.join(result)


def _extract_missing_symbols(build_output: str, test_file: str = '') -> List[str]:  # 从 Maven 或 Gradle 编译输出来提取缺失符号名。
    symbols = []  # 按出现顺序收集缺失符号。
    target_name = os.path.basename(test_file) if test_file else ''  # 提取目标测试文件名用于过滤错误块。
    target_class_name = os.path.splitext(target_name)[0] if target_name else ''  # 同时保留目标测试类简单类名，兼容 location 行不再重复文件名的场景。
    current_error_matches_target = not target_name  # 未提供文件名时默认接受所有缺失符号。
    for raw_line in build_output.splitlines():  # 逐行分析编译输出。
        line = raw_line.strip()  # 去掉首尾空白后便于做模式匹配。
        if ('cannot find symbol' in line or '找不到符号' in line):  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = _matches_target_error_line(line, target_name, target_class_name)  # 同时接受“同文件命中”和“location 简单类名命中”的错误块。
            continue  # 进入新的错误块后直接处理下一行的 symbol 明细。
        if 'location:' in line.lower() or '位置:' in line:  # 某些编译器会在前面保留 `[ERROR]` 前缀，不能只按行首判断。
            current_error_matches_target = current_error_matches_target or _matches_target_location_line(line, target_class_name)  # 命中目标简单类名时继续保留当前块。
        match = re.search(r'(?:symbol|符号)\s*:\s*(?:variable|class|变量|类)\s+([A-Za-z_][A-Za-z0-9_]*)', line, re.IGNORECASE)  # 兼容英文与中文 Maven 输出中的缺失类名格式。
        if not match:  # 当前行不包含缺失符号信息时继续。
            continue  # 跳到下一行。
        if not current_error_matches_target:  # 非目标文件的缺失符号不能拿来修当前测试文件。
            continue  # 跳过无关错误块中的符号。
        symbol = match.group(1)  # 提取缺失的简单类名或变量名。
        if symbol not in symbols:  # 保持顺序的同时避免重复。
            symbols.append(symbol)  # 记录新的缺失符号。
    return symbols  # 返回收集到的缺失符号列表。


def _extract_missing_methods(build_output: str, test_file: str = '') -> List[str]:  # 从 Maven 或 Gradle 编译输出来提取缺失方法名。
    return [reference.method_name for reference in _extract_missing_method_references(build_output, test_file)]  # 复用包含 owner 类信息的更完整提取逻辑。


def _extract_missing_method_references(build_output: str, test_file: str = '') -> List[MissingMethodReference]:  # 从 Maven 或 Gradle 编译输出来提取缺失方法及其 location 指向的类名。
    references: List[MissingMethodReference] = []  # 按出现顺序收集缺失方法引用。
    target_name = os.path.basename(test_file) if test_file else ''  # 提取目标测试文件名用于过滤错误块。
    target_class_name = os.path.splitext(target_name)[0] if target_name else ''  # 同时保留目标测试类简单类名，兼容 location 行只给类名的场景。
    current_error_matches_target = not target_name  # 未提供文件名时默认接受所有缺失方法。
    current_error_file = ''  # 保存当前错误块对应的源文件路径。
    pending_method_name = ''  # 保存当前错误块里刚刚读取到的方法名。
    for raw_line in build_output.splitlines():  # 逐行分析编译输出。
        line = raw_line.strip()  # 去掉首尾空白后便于做模式匹配。
        if ('cannot find symbol' in line or '找不到符号' in line):  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = _matches_target_error_line(line, target_name, target_class_name)  # 同时接受“同文件命中”和“location 简单类名命中”的错误块。
            current_error_file = _extract_error_file_path(line)  # 保存当前错误块对应的源文件路径。
            pending_method_name = ''  # 新错误块开始时清空上一条待定方法名。
            continue
        method_match = re.search(r'(?:symbol|符号)\s*:\s*(?:method|方法)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(', line, re.IGNORECASE)  # 兼容中英文 Maven 输出中的缺失方法格式。
        if method_match:  # 当前行给出了缺失方法名。
            if current_error_matches_target:  # 只有目标文件对应的错误块才参与后续方法提取。
                pending_method_name = method_match.group(1)  # 暂存当前缺失方法名，等待 location 行补充 owner 类信息。
            continue  # 继续向下读取 location 行。
        if not pending_method_name:  # 没有待定的方法名时无需继续处理 location 行。
            continue  # 跳过当前行。
        if 'location:' in line.lower() or '位置:' in line:  # location 行里通常包含真正的 owner 类名。
            current_error_matches_target = current_error_matches_target or _matches_target_location_line(line, target_class_name)  # 命中目标简单类名时也接受当前错误块。
            if not current_error_matches_target:  # 当前块仍不属于目标测试文件时跳过。
                pending_method_name = ''  # 清空待定方法名，避免误绑到后续错误块。
                continue
            owner_class_name = _extract_owner_class_name(line)  # 从 location 行提取实际 owner 类简单名。
            reference = MissingMethodReference(method_name=pending_method_name, owner_class_name=owner_class_name, error_file_path=current_error_file)  # 构造完整的方法引用对象。
            if reference not in references:  # 保持顺序的同时避免重复。
                references.append(reference)  # 记录新的缺失方法引用。
            pending_method_name = ''  # 当前方法引用已经收集完成。
    if pending_method_name and current_error_matches_target:  # 某些老版本编译器不会补充 location 行，这时至少保留方法名本身。
        fallback_reference = MissingMethodReference(method_name=pending_method_name, owner_class_name=target_class_name, error_file_path=current_error_file)  # 回退到目标测试类本身。
        if fallback_reference not in references:  # 保持顺序去重。
            references.append(fallback_reference)
    return references  # 返回包含 owner 类信息的缺失方法列表。


def _extract_missing_variables(build_output: str, test_file: str = '') -> List[str]:  # 从编译输出来提取当前目标测试文件里缺失的字段或常量名。
    variables = []  # 按出现顺序收集缺失变量。
    target_name = os.path.basename(test_file) if test_file else ''  # 提取目标测试文件名用于过滤错误块。
    target_class_name = os.path.splitext(target_name)[0] if target_name else ''  # 同时保留目标简单类名，兼容 location 行只给类名的场景。
    current_error_matches_target = not target_name  # 未提供文件名时默认接受所有缺失变量。
    for raw_line in build_output.splitlines():  # 逐行分析编译输出。
        line = raw_line.strip()  # 去掉首尾空白后便于模式匹配。
        if ('cannot find symbol' in line or '找不到符号' in line):  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = _matches_target_error_line(line, target_name, target_class_name)  # 同时接受“同文件命中”和“location 简单类名命中”的错误块。
            continue
        if 'location:' in line.lower() or '位置:' in line:  # location 行也可能提供目标类名线索。
            current_error_matches_target = current_error_matches_target or _matches_target_location_line(line, target_class_name)  # 命中目标简单类名时继续接受当前错误块。
        match = re.search(r'(?:symbol|符号)\s*:\s*(?:variable|变量)\s+([A-Za-z_][A-Za-z0-9_]*)', line, re.IGNORECASE)  # 兼容中英文 Maven 输出中的缺失变量格式。
        if not match or not current_error_matches_target:  # 非变量行或非目标文件错误块都无需继续。
            continue  # 跳过当前行。
        variable_name = match.group(1)  # 提取缺失变量名。
        if variable_name not in variables:  # 保持顺序的同时避免重复。
            variables.append(variable_name)  # 记录新的缺失变量。
    return variables  # 返回当前目标测试文件里缺失的变量名列表。


def _extract_missing_classes(build_output: str, test_file: str = '') -> List[str]:  # 从编译输出来提取当前目标测试文件里缺失的类名。
    classes = []  # 按出现顺序收集缺失类名。
    target_name = os.path.basename(test_file) if test_file else ''  # 提取目标测试文件名用于过滤错误块。
    target_class_name = os.path.splitext(target_name)[0] if target_name else ''  # 同时保留目标简单类名，兼容 location 行只给类名的场景。
    current_error_matches_target = not target_name  # 未提供文件名时默认接受所有缺失类。
    for raw_line in build_output.splitlines():  # 逐行分析编译输出。
        line = raw_line.strip()  # 去掉首尾空白后便于模式匹配。
        if ('cannot find symbol' in line or '找不到符号' in line):  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = _matches_target_error_line(line, target_name, target_class_name)  # 同时接受“同文件命中”和“location 简单类名命中”的错误块。
            continue
        if 'location:' in line.lower() or '位置:' in line:  # location 行也可能提供目标类名线索。
            current_error_matches_target = current_error_matches_target or _matches_target_location_line(line, target_class_name)  # 命中目标简单类名时继续接受当前错误块。
        match = re.search(r'(?:symbol|符号)\s*:\s*(?:class|类)\s+([A-Za-z_][A-Za-z0-9_]*)', line, re.IGNORECASE)  # 兼容中英文 Maven 输出中的缺失类名格式。
        if not match or not current_error_matches_target:  # 非类名行或非目标文件错误块都无需继续。
            continue  # 跳过当前行。
        class_name = match.group(1)  # 提取缺失类名。
        if class_name not in classes:  # 保持顺序的同时避免重复。
            classes.append(class_name)  # 记录新的缺失类名。
    return classes  # 返回当前目标测试文件里缺失的类名列表。


def _matches_target_error_line(line: str, target_name: str, target_class_name: str) -> bool:  # 判断当前编译错误块是否明显属于目标测试文件。
    if not target_name:  # 未提供目标文件名时直接接受所有错误块。
        return True  # 返回真以保持调用方逻辑简单。
    return target_name in line or (target_class_name and f'{target_class_name}.java' in line)  # 同时接受绝对路径和偶尔只剩简单文件名的编译输出。


def _matches_target_location_line(line: str, target_class_name: str) -> bool:  # 判断 location 行是否指向目标测试类。
    if not target_class_name:  # 缺少目标简单类名时无法继续判断。
        return False  # 返回假让调用方保持保守。
    owner_class_name = _extract_owner_class_name(line)  # 从 location 行提取 owner 类简单名。
    return owner_class_name == target_class_name  # 只有命中目标简单类名时才接受当前错误块。


def _extract_owner_class_name(location_line: str) -> str:  # 从 `location: class com.example.FooTest` 这类行里提取简单类名。
    match = re.search(r'(?:class|类)\s+([A-Za-z0-9_$.]+)', location_line, re.IGNORECASE)  # 兼容中英文 location 行格式。
    if not match:  # 无法匹配时返回空串让调用方继续保持保守。
        return ''
    return match.group(1).split('.')[-1]  # 仅返回简单类名，便于与目标测试类快速比较。


def _extract_error_file_path(error_line: str) -> str:  # 从编译错误行里提取出报错源文件路径。
    match = re.search(r'((?:[A-Za-z]:)?[^:\s]+\.java):\[\d+,\d+\]', error_line)  # 兼容 Unix 和 Windows 风格的 `path:[line,col]` 片段。
    return match.group(1) if match else ''  # 匹配成功时返回路径，否则返回空串。


def _extract_related_test_error_files(repo_dir: str, target_test_file: str, build_output: str) -> List[str]:  # 从构建日志里提取“同模块、非目标”的测试源码错误文件列表。
    target_abs_path = os.path.abspath(target_test_file) if target_test_file else ''  # 统一规整目标测试文件绝对路径。
    target_module_root = _module_root_for_test_file(target_abs_path)  # 计算目标测试文件所属模块根目录，只修当前模块里的连带测试。
    related_files: List[str] = []  # 按出现顺序收集非目标测试文件。
    for raw_line in build_output.splitlines():  # 顺序扫描构建输出。
        error_file = _extract_error_file_path(raw_line)  # 读取当前错误行对应的源文件路径。
        if not error_file:  # 非文件路径行无需继续。
            continue
        normalized_error_file = os.path.abspath(error_file)  # 统一转成绝对路径便于比较。
        if normalized_error_file == target_abs_path:  # 目标测试文件本身不属于“非目标测试”修复范围。
            continue
        if f'{os.sep}src{os.sep}test{os.sep}' not in normalized_error_file:  # 只处理测试源码，不碰 main source。
            continue
        if target_module_root and not normalized_error_file.startswith(target_module_root + os.sep):  # 只处理当前模块里的连带测试，避免越模块修改。
            continue
        if not normalized_error_file.startswith(os.path.abspath(repo_dir)):  # 工作区外路径一律忽略。
            continue
        if normalized_error_file not in related_files and os.path.isfile(normalized_error_file):  # 保持顺序去重，同时确保文件真实存在。
            related_files.append(normalized_error_file)
    return related_files  # 返回当前构建中真正报错的非目标测试文件列表。


def _module_root_for_test_file(test_file: str) -> str:  # 根据测试文件路径推断所属 Maven/Gradle 模块根目录。
    if not test_file:  # 缺少路径时无法继续推断。
        return ''  # 返回空串让调用方保持保守。
    marker = f'{os.sep}src{os.sep}test{os.sep}'  # 当前只关心标准测试源码目录。
    if marker not in test_file:  # 不在标准测试源码树下时不做额外推断。
        return os.path.dirname(test_file)
    return test_file.split(marker, 1)[0]  # `src/test/...` 之前的目录就是当前模块根。


def _resolve_missing_symbol_reference(test_file: str, symbol: str) -> Optional[Tuple[str, Optional[str]]]:  # 为缺失符号解析安全的实际类名与 import 路径。
    current_file_reference = _find_import_reference_in_content(_read_file_quietly(test_file), symbol)  # 先尊重当前测试文件已经存在的 import，避免再引入同名冲突导入。
    if current_file_reference:  # 当前文件自己已经给出了稳定导入时直接复用。
        return current_file_reference  # 返回文件内现成的真实符号名与导入路径。
    repo_root = _find_repo_root(test_file)  # 先定位当前测试文件所属仓库根目录以便复用仓库内已有 import 线索。
    if repo_root is not None:  # 只有成功定位仓库根目录时才尝试跨文件解析现有 import。
        imported_symbol = _find_existing_import_reference(repo_root, symbol)  # 优先复用仓库其他源码文件里已经存在的 import 语句。
        if imported_symbol:  # 一旦命中唯一现有 import 线索就直接采用。
            return imported_symbol  # 这类线索通常比内置映射和文件名猜测都更可靠。
    contextual_reference = _resolve_contextual_symbol_reference(test_file, symbol)  # 在部分高歧义符号上结合当前文件上下文做更保守的推断。
    if contextual_reference:  # 只有上下文线索足够明确时才采用。
        return contextual_reference  # 返回基于当前文件上下文解析出的安全引用。
    import_path = JAVA_IMPORT_CANDIDATES.get(symbol)  # 再回退到内置的高置信度标准库或第三方映射。
    if import_path:  # 命中内置映射时无需继续搜索仓库源码。
        return symbol, import_path  # 返回原符号名与对应 import 路径。
    project_reference = _find_project_symbol_reference(test_file, symbol)  # 再回退到仓库源码与现有 import 语句中解析唯一候选。
    if project_reference:  # 找到安全的仓库内候选时直接返回。
        return project_reference  # 返回解析出的实际符号名与可选 import 路径。
    return None  # 无法安全解析时返回空值。


def _resolve_missing_method_reference(test_file: str, method_name: str) -> Optional[str]:  # 为缺失静态方法解析安全的 static import 路径。
    repo_root = _find_repo_root(test_file)  # 先定位当前测试文件所属仓库根目录以便复用仓库内现有 static import 线索。
    if method_name == 'assertThat':  # `assertThat` 同时横跨 JUnit、Hamcrest 和 AssertJ，必须用更保守的专门分支处理。
        return _resolve_assert_that_reference(test_file, repo_root)  # 只有在调用形态和仓库线索都明确时才追加 static import。
    if method_name == 'entry':  # `entry(...)` 在当前失败集中主要对应 AssertJ 的 map entry helper，需要和仓库 AssertJ 证据一起判断。
        if repo_root is not None and _repo_supports_assertj(repo_root):  # 只有仓库里已经依赖或使用 AssertJ 时才补这个 static import。
            return 'org.assertj.core.api.Assertions.entry'  # 返回 AssertJ 的 map entry helper 导入路径。
        return None  # 缺少 AssertJ 证据时保持保守。
    if repo_root is not None:  # 只有成功定位仓库根目录时才尝试跨文件解析已有 static import。
        existing_import = _find_existing_static_import_reference(repo_root, method_name)  # 优先复用仓库其他源码文件里已经存在的 static import。
        if existing_import:  # 一旦命中唯一现有 static import 线索就直接采用。
            return existing_import  # 这类线索比内置映射更能反映当前项目真实依赖。
    return JAVA_STATIC_IMPORT_CANDIDATES.get(method_name)  # 无仓库线索时再回退到高置信度静态导入映射。


def _qualify_missing_method_reference(content: str, method_name: str) -> Tuple[str, str]:  # 基于文件内已有断言类导入将裸方法调用修复为限定符调用。
    owner = _resolve_existing_assertion_owner(content, method_name)  # 先判断当前文件是否已经导入了可安全复用的断言类。
    if not owner:  # 没有足够明确的断言类线索时不做限定符修复。
        return content, ''  # 返回原内容并说明未修复。
    pattern = re.compile(rf'(?<![A-Za-z0-9_$.]){re.escape(method_name)}(\s*\()')  # 仅匹配未被现有限定符绑定的裸方法调用。
    updated_content, replacement_count = pattern.subn(rf'{owner}.{method_name}\1', content)  # 将裸方法调用统一改写为现有断言类的限定符形式。
    if replacement_count == 0:  # 没有发生任何真实替换时不记录修复动作。
        return content, ''  # 保持原内容不变并说明没有修复。
    return updated_content, f'{method_name}->{owner}.{method_name}'  # 返回修复后的内容和可写入日志的修复摘要。


def _resolve_existing_assertion_owner(content: str, method_name: str) -> Optional[str]:  # 根据当前文件里已有的断言类导入或用法选择最安全的方法限定符所有者。
    candidate_owners: List[str] = []  # 保存当前可被安全复用的断言类限定符候选。
    if method_name in JUNIT_ASSERT_METHODS and _has_class_import_or_usage(content, 'org.junit.Assert', 'Assert'):  # 只有当前文件已经显式使用 JUnit Assert 时才回退到 `Assert.xxx(...)`。
        candidate_owners.append('Assert')  # 记录 JUnit Assert 作为可复用限定符。
    if method_name == 'assertThat' and _has_class_import_or_usage(content, 'org.hamcrest.MatcherAssert', 'MatcherAssert'):  # 如果当前文件已经显式使用 MatcherAssert，则可以安全复用它的 `assertThat`。
        candidate_owners.append('MatcherAssert')  # 记录 Hamcrest MatcherAssert 作为可复用限定符。
    if method_name == 'assertThat' and _has_class_import_or_usage(content, 'org.assertj.core.api.Assertions', 'Assertions'):  # 如果当前文件已经显式使用 AssertJ Assertions，则可以安全复用它的 `assertThat`。
        candidate_owners.append('Assertions')  # 记录 AssertJ Assertions 作为可复用限定符。
    unique_owners = list(dict.fromkeys(candidate_owners))  # 去重同时保留候选优先级顺序。
    if len(unique_owners) == 1:  # 只有唯一候选时才做自动限定符修复，避免在断言库冲突时误修。
        return unique_owners[0]  # 返回唯一且安全的断言类限定符。
    return None  # 没有候选或存在多种候选时都不做自动限定符修复。


def _resolve_assert_that_reference(test_file: str, repo_root: Optional[str]) -> Optional[str]:  # 结合调用形态和仓库导入线索为 `assertThat` 选择最可能的静态导入。
    try:  # 读取失败时宁可放弃修复，也不做高风险猜测。
        with open(test_file, 'r', encoding='utf-8', errors='ignore') as f:  # 读取当前测试文件以分析 `assertThat` 的使用方式。
            content = f.read()  # 获取完整源码文本供后续正则匹配。
    except Exception:  # 读文件失败时直接跳过该修复分支。
        return None  # 无法分析调用形态时不进行自动修复。
    if _looks_like_assertj_assert_that(content):  # 出现链式 `assertThat(...).startsWith()/isEqualTo()/contains()` 时更像 AssertJ。
        if repo_root is not None and _repo_supports_assertj(repo_root):  # 只要仓库里已经使用或依赖 AssertJ，就允许补对应 static import。
            return 'org.assertj.core.api.Assertions.assertThat'  # 返回 AssertJ 风格的 static import。
    if _looks_like_matcher_assert_that(content):  # 两参数 `assertThat(actual, matcher)` 更像 Hamcrest 风格。
        if repo_root is not None and _repo_supports_hamcrest_matcher_assert(repo_root):  # 只有仓库里已经存在 Hamcrest MatcherAssert 线索时才自动采用。
            return 'org.hamcrest.MatcherAssert.assertThat'  # 返回 Hamcrest 风格的 static import。
    return None  # 其余场景保持保守，不为 `assertThat` 自动猜测依赖。


def _looks_like_assertj_assert_that(content: str) -> bool:  # 通过链式断言特征识别 AssertJ 风格的 `assertThat`。
    method_pattern = '|'.join(re.escape(method_name) for method_name in ASSERTJ_ASSERT_THAT_METHODS)  # 将当前支持的 AssertJ 链式方法安全拼成正则候选。
    assertj_pattern = re.compile(rf'assertThat\s*\([\s\S]{{0,240}}?\)\s*\.\s*(?:{method_pattern})\b')  # 允许参数中再出现一层括号，覆盖 `assertThat(foo.bar())` 这类真实调用。
    return bool(assertj_pattern.search(content))  # 命中任一典型链式模式时即可判断为更像 AssertJ。


def _looks_like_matcher_assert_that(content: str) -> bool:  # 通过双参数调用特征识别 Hamcrest 风格的 `assertThat`。
    matcher_pattern = re.compile(r'assertThat\s*\([^,\n]+,\s*[^\n]+\)')  # Hamcrest 的 `assertThat` 通常至少包含被测值和 matcher 两个参数。
    return bool(matcher_pattern.search(content))  # 命中双参数模式时视为更像 Hamcrest。


def _has_class_import_or_usage(content: str, import_path: str, qualifier: str) -> bool:  # 判断当前文件是否已经显式导入或实际使用了某个限定符类。
    import_pattern = re.compile(rf'^\s*import\s+{re.escape(import_path)}\s*;\s*$', re.MULTILINE)  # 先检查是否显式导入了目标类。
    if import_pattern.search(content):  # 命中显式导入时说明限定符类已经在当前文件语义中成立。
        return True  # 返回真表示可以安全复用该限定符。
    usage_pattern = re.compile(rf'\b{re.escape(qualifier)}\.')  # 再检查文件中是否已经出现过该限定符的真实调用。
    return bool(usage_pattern.search(content))  # 命中现有调用时同样说明该限定符可被安全复用。


def _replace_symbol_tokens(content: str, wrong_symbol: str, correct_symbol: str) -> Tuple[str, int]:  # 将源码中的错误符号名替换为解析出的真实符号名。
    pattern = re.compile(rf'\b{re.escape(wrong_symbol)}\b')  # 仅按完整标识符做替换，避免误伤更长的变量名或字符串片段。
    return pattern.subn(correct_symbol, content)  # 返回替换后的文本和实际替换次数。


def _is_safe_symbol_case_replacement(wrong_symbol: str, correct_symbol: str, test_file: str = '', import_path: Optional[str] = None) -> bool:  # 仅允许高置信度的类型名大小写修正，避免把变量误改成类名。
    if not wrong_symbol or not correct_symbol:  # 任一符号为空时都不应该执行替换。
        return False  # 明确拒绝空符号替换。
    if wrong_symbol.lower() != correct_symbol.lower():  # 当前保护逻辑只处理大小写差异，不处理不同单词之间的“近似修复”。
        return False  # 避免把缺失符号误修成另一个名字相近的类。
    if not wrong_symbol[:1].isupper() or not correct_symbol[:1].isupper():  # Java 类型名通常首字母大写，局部变量和字段大多不是。
        return False  # 像 `list->List` 这类局部变量误修在这里被拦住。
    if re.search(r'[A-Z]{2,}', wrong_symbol[1:]) or re.search(r'[A-Z]{2,}', correct_symbol[1:]):  # `JSONObject->JsonObject` 这类首字母缩写规范化通常不是安全的“大小写修正”。
        if not _repo_contains_source_for_import(test_file, import_path):  # 只有当正确类名明确来自当前仓库源码时，才允许这类缩写风格修正继续进行。
            return False  # 三方库之间仅大小写不同的类名风险太高，必须拦住。
    return True  # 满足上述条件时，视为可接受的类型名大小写修正。


def _repo_contains_source_for_import(test_file: str, import_path: Optional[str]) -> bool:  # 判断当前 import 路径是否确实对应仓库内源码文件。
    repo_root = _find_repo_root(test_file)  # 先定位当前测试文件所属仓库根目录。
    if repo_root is None or not import_path:  # 缺少仓库根路径或导入路径时无法继续判断。
        return False  # 返回假以保持调用方逻辑简单。
    relative_suffix = import_path.replace('.', os.sep) + '.java'  # 将 import 路径转换成 Java 源文件后缀。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和无关目录。
    for root, dirs, files in os.walk(repo_root):  # 遍历当前仓库目录树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 裁剪无关目录以减少扫描开销。
        for filename in files:  # 逐个检查当前目录下的文件。
            if filename != os.path.basename(relative_suffix):  # 文件名不同则不可能对应目标导入。
                continue  # 跳过当前文件。
            candidate_path = os.path.join(root, filename)  # 拼出当前候选文件路径。
            if candidate_path.endswith(relative_suffix):  # 只有包路径也一致时才视为命中真正的仓库内类定义。
                return True  # 当前 import 明确对应仓库内源码文件。
    return False  # 扫描完仍未命中时说明它更可能来自第三方依赖。


def _resolve_contextual_symbol_reference(test_file: str, symbol: str, extra_content: str = '') -> Optional[Tuple[str, Optional[str]]]:  # 在少量高歧义符号上结合当前文件上下文和当前补丁片段做保守推断。
    content = _read_file_quietly(test_file)  # 先读取当前测试文件内容。
    if extra_content:  # fixed_sha helper 或 generated_patch 正文里的类型语义有时比当前文件旧 import 更可信。
        content = content + '\n' + extra_content  # 把额外代码片段拼进分析上下文。
    if not content:  # 当前文件和额外上下文都不可读时无法继续推断。
        return None  # 返回空值交给其余推断逻辑继续处理。
    normalized_content = content.lower()  # 统一转小写，便于做大小写无关的上下文匹配。
    if symbol == 'Document' and any(marker in normalized_content for marker in ['org.w3c.dom', 'documentbuilder', 'namednodemap', 'inputsource', 'createelement', 'getdocumentelement']):  # DOM/XML 测试里缺少 `Document` 通常就是 `org.w3c.dom.Document`。
        return symbol, 'org.w3c.dom.Document'  # 返回基于 DOM 上下文的 `Document` 导入路径。
    if symbol == 'Node' and any(marker in normalized_content for marker in ['org.w3c.dom', 'documentbuilder', 'namednodemap', 'inputsource']):  # 只有当文件已经明显处于 DOM/XML 上下文中时才推断 `Node` 为 `org.w3c.dom.Node`。
        return symbol, 'org.w3c.dom.Node'  # 返回基于 DOM 上下文的安全导入路径。
    if symbol == 'JsonParser':  # `JsonParser` 同时可能来自 Gson 和 Jackson，需要结合当前文件上下文做更保守的区分。
        gson_markers = ('gsonutils', 'jsonelement', 'jsonobject', 'parsestring(', 'new jsonparser(', '.getasjsonobject(', '.getasjsonarray(', '.parse(')  # 这些标记更像 Gson 语义。
        jackson_markers = ('jsonfactory', 'jsontoken', 'deserializationcontext', 'serializerprovider')  # 这些标记更像 Jackson core parser。
        if any(marker in normalized_content for marker in gson_markers):  # 当前文件明显在用 Gson 风格 API 时才补 Gson 的 `JsonParser`。
            return symbol, 'com.google.gson.JsonParser'  # 返回 Gson 的安全导入路径。
        if any(marker in normalized_content for marker in jackson_markers):  # 当前文件明显在用 Jackson core 语义时才补 Jackson 的 `JsonParser`。
            return symbol, 'com.fasterxml.jackson.core.JsonParser'  # 返回 Jackson core 的安全导入路径。
    if symbol in {'JSONObject', 'JSONArray'}:  # `JSONObject` 和 `JSONArray` 在 org.json 与 jettison 之间存在常见歧义。
        if 'org.codehaus.jettison.json' in normalized_content:  # 当前文件已经显式处于 jettison 语境时优先绑定到 jettison。
            return symbol, f'org.codehaus.jettison.json.{symbol}'  # 返回 jettison 的安全导入路径。
        if any(marker in normalized_content for marker in ('org.json.', 'jsonassert', 'new jsonobject(', 'new jsonarray(')):  # 其余 JSON 断言和对象构造语境优先绑定到 org.json。
            return symbol, f'org.json.{symbol}'  # 返回 org.json 的安全导入路径。
    if symbol == 'Sets':  # `Sets` 在 Guava 和 Mockito 内部工具类之间存在歧义，需要看具体调用形态。
        if 'newset(' in normalized_content:  # 当前失败集里 `Sets.newSet(...)` 来自 Mockito 内部工具类，而不是 Guava。
            return symbol, 'org.mockito.internal.util.collections.Sets'  # 返回 skywalking 一类样本真正可编译的导入路径。
        guava_markers = ('immutablelist', 'immutablemap', 'immutableset', 'com.google.common', 'multimap', 'hashmultimap', 'sets.newhashset', 'sets.intersection', 'sets.cartesianproduct')  # 这些线索才更像 Guava Sets 语义。
        if any(marker in normalized_content for marker in guava_markers):  # 只有 Guava 语境足够明确时才补对应导入。
            return symbol, 'com.google.common.collect.Sets'  # 返回 Guava Sets 的安全导入路径。
    if symbol in {'Lists', 'Maps'}:  # 其余集合工具类仍需结合当前文件里的 Guava 线索判断，避免误绑到其他同名工具类。
        guava_markers = ('immutablelist', 'immutablemap', 'immutableset', 'com.google.common', 'multimap', 'hashmultimap', 'lists.newarraylist', 'maps.newhashmap', 'maps.immutableentry')  # 这组标记更像 Guava 语义。
        if any(marker in normalized_content for marker in guava_markers):  # 只有在 Guava 语境足够明显时才补对应 import。
            return symbol, f'com.google.common.collect.{symbol}'  # 返回 Guava 的安全导入路径。
    if symbol == 'Config':  # `Config` 在很多项目里都可能是自定义类，必须配合 typesafe 特征一起判断。
        if 'configfactory' in normalized_content or 'configresolveoptions' in normalized_content or 'com.typesafe.config' in normalized_content:  # 只有在 typesafe 语境明显时才绑定到该依赖。
            return symbol, 'com.typesafe.config.Config'  # 返回 typesafe Config 导入路径。
    if symbol == 'Option':  # `Option` 既可能来自 json-path，也可能来自 json-unit。
        if 'ignoring_array_order' in normalized_content or 'assertthatjson' in normalized_content or 'jsonassertions' in normalized_content:  # 这些线索属于 json-unit 的 Option。
            return symbol, 'net.javacrumbs.jsonunit.core.Option'  # 返回 json-unit 的安全导入路径。
        if any(marker in normalized_content for marker in ['jsonpath', 'documentcontext', 'readcontext', 'com.jayway.jsonpath']):  # 这些线索属于 json-path。
            return symbol, 'com.jayway.jsonpath.Option'  # 返回 json-path 的安全导入路径。
    if symbol == 'JSONException':  # `JSONException` 在 org.json 与 jettison 之间也存在项目级差异。
        if 'org.codehaus.jettison.json' in normalized_content:  # 当前文件已经明显处于 jettison 语境中时优先绑定到 jettison。
            return symbol, 'org.codehaus.jettison.json.JSONException'  # 返回 jettison 的安全导入路径。
        if 'org.json.' in normalized_content or 'jsonassert' in normalized_content or 'jsonobject' in normalized_content or 'jsonarray' in normalized_content:  # 其余 JSON 断言和对象构造上下文仍优先使用 org.json。
            return symbol, 'org.json.JSONException'  # 返回 org.json 的安全导入路径。
    return None  # 其余符号暂不做额外的上下文推断。


def _resolve_reference_symbol_import_line(test_file: str, symbol: str, fallback_import_line: str, code_fragment: str = '') -> str:  # 基于当前测试文件、候选代码片段和仓库上下文为参考信号解析最终 import 行。
    contextual_symbol = _resolve_contextual_symbol_reference(test_file, symbol, extra_content=code_fragment) if test_file and code_fragment else None  # fixed_sha helper 自身的语义有时比当前文件旧 import 更可信。
    if contextual_symbol and contextual_symbol[1]:  # 只要补丁片段本身已经足够说明真正依赖哪个库，就优先采用它。
        return f'import {contextual_symbol[1]};'  # 返回基于当前候选代码片段解析出的 import。
    resolved_symbol = _resolve_missing_symbol_reference(test_file, symbol) if test_file else None  # 优先复用当前文件和仓库里已经存在的真实导入线索。
    if resolved_symbol and resolved_symbol[1]:  # 只有真正解析出 import 路径时才转成 import 行。
        return f'import {resolved_symbol[1]};'  # 返回基于真实仓库线索解析出的 import。
    return fallback_import_line  # 仓库里没有更强证据时再回退到内置参考导入。


def _repo_supports_assertj(repo_root: str) -> bool:  # 判断仓库里是否已有足够证据表明可以安全使用 AssertJ。
    if _find_existing_static_import_reference(repo_root, 'assertThat') == 'org.assertj.core.api.Assertions.assertThat':  # 已有完全一致的 static import 时最可信。
        return True  # 直接复用当前仓库已有的 AssertJ `assertThat` 线索。
    return _repo_contains_text(repo_root, 'org.assertj.core.api.Assertions') or _repo_contains_text(repo_root, '<artifactId>assertj-core</artifactId>')  # 其余情况接受类导入或 pom 依赖证据。


def _repo_supports_hamcrest_matcher_assert(repo_root: str) -> bool:  # 判断仓库里是否已有足够证据表明可以安全使用 Hamcrest 的 `MatcherAssert.assertThat`。
    if _find_existing_static_import_reference(repo_root, 'assertThat') == 'org.hamcrest.MatcherAssert.assertThat':  # 已有完全一致的 static import 时最可信。
        return True  # 直接复用当前仓库已有的 Hamcrest `assertThat` 线索。
    return _repo_contains_text(repo_root, 'org.hamcrest.MatcherAssert')  # 其余情况接受类导入或限定符使用证据。


def _repo_contains_text(repo_root: str, needle: str) -> bool:  # 在仓库源码和 pom 中做一次轻量的文本证据搜索。
    if not repo_root or not needle:  # 缺少仓库根路径或待查文本时无法继续搜索。
        return False  # 返回假以保持调用方逻辑简单。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和无关目录。
    for root, dirs, files in os.walk(repo_root):  # 遍历当前仓库目录树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 裁剪无关目录以减少扫描开销。
        for filename in files:  # 逐个扫描当前目录下的文件。
            if not (filename.endswith('.java') or filename == 'pom.xml'):  # 只在 Java 源码和 pom 中查找依赖线索。
                continue  # 其余文件对当前判断帮助不大。
            file_path = os.path.join(root, filename)  # 拼出当前文件路径。
            try:  # 个别文件读取失败时直接跳过即可。
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取文本文件。
                    if needle in f.read():  # 命中文本线索时立即返回。
                        return True  # 当前仓库已具备目标依赖或导入证据。
            except Exception:  # 单个文件读取失败不影响整体扫描。
                continue  # 跳过当前文件继续扫描。
    return False  # 扫描完仍未命中时返回假。


def _find_project_symbol_reference(test_file: str, symbol: str) -> Optional[Tuple[str, Optional[str]]]:  # 在仓库源码中按大小写不敏感方式解析真实符号名与 import 路径。
    repo_root = _find_repo_root(test_file)  # 先定位当前测试文件所属仓库根目录。
    if repo_root is None:  # 找不到仓库根目录时无法继续搜索源码。
        return None  # 返回空值交由上层跳过该符号。
    candidate_paths = []  # 收集与目标符号精确同名的源码路径，避免只因大小写接近就误改成仓库内其他类。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和无关目录。
    target_filename = f'{symbol}.java'  # 先构造目标符号对应的标准文件名。
    for root, dirs, files in os.walk(repo_root):  # 遍历整个仓库源码树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 就地裁剪无关目录以减少误命中和耗时。
        if target_filename in files:  # 只保留与缺失符号完全同名的 Java 文件，避免把 JsonPath 误修成 JSONPath 之类的仓库内类。
            candidate_paths.append(os.path.join(root, target_filename))  # 记录当前候选类文件路径。
    if len(candidate_paths) != 1:  # 只有唯一匹配时才做自动修复，避免歧义。
        return None  # 多个或零个候选都视为无法安全推断。
    candidate_package = _read_java_package(candidate_paths[0])  # 读取候选类的 package 声明。
    test_package = _read_java_package(test_file)  # 读取当前测试文件的 package 声明。
    if not candidate_package or candidate_package == test_package:  # 同包类或读不到包名时无需 import，但仍可能需要修正类名大小写。
        return symbol, None  # 返回原符号名并说明不需要新增 import。
    return symbol, f'{candidate_package}.{symbol}'  # 返回原符号名与完整 import 路径。


def _read_file_quietly(file_path: str) -> str:  # 以宽松模式读取文件内容，失败时返回空串。
    if not file_path:  # 路径为空时无需继续读取。
        return ''  # 返回空串保持调用方逻辑简单。
    try:  # 当前辅助逻辑只应增强成功率，不应因读文件失败中断主流程。
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取文本文件。
            return f.read()  # 返回完整文本内容。
    except Exception:  # 读文件失败时直接回退为空串。
        return ''  # 由上层继续走其余推断路径。


def _find_import_reference_in_content(content: str, symbol: str) -> Optional[Tuple[str, str]]:  # 在当前文件正文中搜索与目标符号对应的现成 import。
    if not content or not symbol:  # 缺少文本或符号名时无法继续解析。
        return None  # 返回空值保持调用方逻辑简单。
    import_pattern = re.compile(r'^\s*import\s+([A-Za-z0-9_.]*\.([A-Za-z_][A-Za-z0-9_]*))\s*;\s*$', re.MULTILINE)  # 匹配普通 import 语句并提取简单类名。
    candidate_imports = []  # 保存当前文件内命中的导入候选。
    for match in import_pattern.finditer(content):  # 顺序扫描当前文件头部 import。
        if match.group(2).lower() == symbol.lower():  # 大小写无关地匹配目标符号名。
            candidate_imports.append((match.group(2), match.group(1)))  # 保存真实符号名与完整导入路径。
    unique_imports = list(dict.fromkeys(candidate_imports))  # 去重同时保留原始顺序。
    if len(unique_imports) == 1:  # 只有唯一导入时才安全复用。
        return unique_imports[0]  # 返回当前文件里已经存在的导入线索。
    return None  # 多个或零个候选都视为无法安全推断。


def _find_project_import_path(test_file: str, symbol: str) -> Optional[str]:  # 在当前仓库源码中搜索唯一类定义并推断 import 路径。
    repo_root = _find_repo_root(test_file)  # 先定位当前测试文件所属仓库根目录。
    if repo_root is None:  # 找不到仓库根目录时无法继续搜索源码。
        return None  # 返回空值交由上层跳过该符号。

    imported_symbol = _find_existing_import_path(repo_root, symbol)  # 优先尝试复用仓库其他源码文件里已经存在的 import 语句。
    if imported_symbol:  # 如果在仓库中找到了唯一 import 线索则直接返回。
        return imported_symbol  # 使用已有 import 线索通常比类文件搜索更能覆盖第三方依赖类型。

    candidate_paths = []  # 收集匹配 symbol.java 的源码路径。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和非源码目录。
    target_filename = f'{symbol}.java'  # 只搜索与符号同名的 Java 源文件。
    for root, dirs, files in os.walk(repo_root):  # 遍历整个仓库源码树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 就地裁剪无关目录以减少误命中和耗时。
        if target_filename in files:  # 命中同名 Java 文件时记录完整路径。
            candidate_paths.append(os.path.join(root, target_filename))  # 追加到候选路径列表。

    if len(candidate_paths) != 1:  # 只有唯一匹配时才做自动导入，避免歧义。
        return None  # 多个或零个候选都视为无法安全推断。

    candidate_package = _read_java_package(candidate_paths[0])  # 读取候选类的 package 声明。
    if not candidate_package:  # 读不到 package 时无法构造完整 import。
        return None  # 返回空值表示无法安全导入。
    test_package = _read_java_package(test_file)  # 读取当前测试文件的 package 声明。
    if candidate_package == test_package:  # 同包类无需 import。
        return None  # 避免生成无意义的同包 import。
    return f'{candidate_package}.{symbol}'  # 返回完整的包路径加类名。


def _find_existing_import_path(repo_root: str, symbol: str) -> Optional[str]:  # 在仓库源码中搜索唯一的现成 import 语句。
    reference = _find_existing_import_reference(repo_root, symbol)  # 复用统一的 import 参考搜索逻辑。
    if not reference:  # 没有唯一参考时直接返回空值。
        return None  # 交由上层继续其他推断路径。
    _, import_path = reference  # 只保留完整 import 路径。
    return import_path  # 返回最终 import 路径。


def _find_existing_import_reference(repo_root: str, symbol: str) -> Optional[Tuple[str, str]]:  # 在仓库源码中搜索唯一的现成 import 语句并保留真实符号大小写。
    import_pattern = re.compile(r'^\s*import\s+([A-Za-z0-9_.]*\.([A-Za-z_][A-Za-z0-9_]*))\s*;\s*$')  # 匹配完整的 Java import 语句并单独提取末尾符号名。
    candidate_imports: List[Tuple[str, str]] = []  # 保存命中的实际符号名与 import 路径。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和无关目录。
    for root, dirs, files in os.walk(repo_root):  # 遍历整个仓库源码树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 先裁剪无关目录减少扫描开销。
        for filename in files:  # 遍历当前目录下的文件。
            if not filename.endswith('.java'):  # 只扫描 Java 源文件中的 import 语句。
                continue  # 非 Java 文件无需处理。
            file_path = os.path.join(root, filename)  # 拼出当前 Java 文件的完整路径。
            try:  # 单个文件读取失败时直接跳过。
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取 Java 文件。
                    for line in f:  # 按行扫描 import 语句。
                        match = import_pattern.match(line)  # 检查当前行是否导入了目标符号。
                        if match and match.group(2).lower() == symbol.lower():  # 命中大小写无关相等的目标 import 语句时记录下来。
                            candidate_imports.append((match.group(2), match.group(1)))  # 保存实际符号名与完整 import 路径。
                            break  # 一个文件命中一次即可停止继续扫描该文件。
                        stripped = line.strip()  # 去掉空白以便判断是否已经离开 import 区。
                        if stripped.startswith('public ') or stripped.startswith('class ') or stripped.startswith('@'):  # 一旦进入类型定义区域就无需继续扫描该文件头部。
                            break  # 提前结束当前文件扫描以减少开销。
            except Exception:  # 个别文件读取失败不影响整体导入推断。
                continue  # 跳过当前文件继续扫描其他文件。
    unique_imports = list(dict.fromkeys(candidate_imports))  # 去重同时保留原始发现顺序。
    if len(unique_imports) == 1:  # 只有唯一候选时才安全复用。
        return unique_imports[0]  # 返回仓库中唯一的符号名与 import 路径。
    return None  # 多个或零个候选都视为无法安全推断。


def _find_existing_static_import_reference(repo_root: str, method_name: str) -> Optional[str]:  # 在仓库源码中搜索唯一的现成 static import 语句。
    import_pattern = re.compile(r'^\s*import\s+static\s+([A-Za-z0-9_.]*\.([A-Za-z_][A-Za-z0-9_]*))\s*;\s*$')  # 匹配完整的 Java static import 语句并单独提取末尾方法名。
    candidate_imports: List[str] = []  # 保存命中的完整 static import 路径。
    skip_dirs = {'.git', 'target', 'build', '.gradle', '.mvn', 'node_modules'}  # 跳过构建产物和无关目录。
    for root, dirs, files in os.walk(repo_root):  # 遍历整个仓库源码树。
        dirs[:] = [d for d in dirs if d not in skip_dirs]  # 先裁剪无关目录减少扫描开销。
        for filename in files:  # 遍历当前目录下的文件。
            if not filename.endswith('.java'):  # 只扫描 Java 源文件中的 static import 语句。
                continue  # 非 Java 文件无需处理。
            file_path = os.path.join(root, filename)  # 拼出当前 Java 文件的完整路径。
            try:  # 单个文件读取失败时直接跳过。
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取 Java 文件。
                    for line in f:  # 按行扫描 static import 语句。
                        match = import_pattern.match(line)  # 检查当前行是否导入了目标静态方法。
                        if match and match.group(2) == method_name:  # 命中方法名完全相同的 static import 时记录下来。
                            candidate_imports.append(match.group(1))  # 保存完整 static import 路径。
                            break  # 一个文件命中一次即可停止继续扫描该文件。
                        stripped = line.strip()  # 去掉空白以便判断是否已经离开 import 区。
                        if stripped.startswith('public ') or stripped.startswith('class ') or stripped.startswith('@'):  # 一旦进入类型定义区域就无需继续扫描该文件头部。
                            break  # 提前结束当前文件扫描以减少开销。
            except Exception:  # 个别文件读取失败不影响整体 static import 推断。
                continue  # 跳过当前文件继续扫描其他文件。
    unique_imports = list(dict.fromkeys(candidate_imports))  # 去重同时保留原始发现顺序。
    if len(unique_imports) == 1:  # 只有唯一候选时才安全复用。
        return unique_imports[0]  # 返回仓库中唯一的 static import 路径。
    return None  # 多个或零个候选都视为无法安全推断。


def _find_repo_root(test_file: str) -> Optional[str]:  # 从测试文件开始向上寻找包含 .git 的仓库根目录。
    current_dir = os.path.abspath(os.path.dirname(test_file))  # 从测试文件所在目录开始向上搜索。
    while True:  # 逐层向上回溯直到文件系统根目录。
        if os.path.isdir(os.path.join(current_dir, '.git')):  # 命中 .git 目录时视为仓库根目录。
            return current_dir  # 返回仓库根目录路径。
        parent_dir = os.path.dirname(current_dir)  # 获取父目录准备继续搜索。
        if parent_dir == current_dir:  # 到达文件系统根目录时停止。
            return None  # 无法定位仓库根目录时返回空值。
        current_dir = parent_dir  # 继续向上搜索父目录。


def _read_java_package(java_file: str) -> str:  # 读取 Java 源文件的 package 声明。
    try:  # 文件读取失败时返回空串即可。
        with open(java_file, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取 Java 源文件。
            for line in f:  # 按行扫描，package 声明一般位于文件顶部。
                stripped = line.strip()  # 去掉首尾空白便于识别。
                if stripped.startswith('package ') and stripped.endswith(';'):  # 命中标准 package 声明格式。
                    return stripped[len('package '):-1].strip()  # 返回去掉关键字和分号后的包名。
                if stripped.startswith('import ') or stripped.startswith('public ') or stripped.startswith('class '):  # 一旦进入 import 或类型声明区仍未见 package，即视为默认包。
                    break  # 提前结束扫描以减少无谓读取。
    except Exception:  # 读取失败时忽略异常并返回空串。
        return ''  # 返回空串表示未能读取有效包名。
    return ''  # 未找到 package 声明时返回空串。


def _insert_import_lines(content: str, import_lines: List[str]) -> str:  # 将新的 import 行插入到 package 和现有 import 之后。
    lines = content.splitlines()  # 先按行切分文件内容。
    cleaned_imports = []  # 保存去重后的 import 列表。
    existing_import_lines = {line.strip() for line in lines if line.strip().startswith('import ')}  # 先收集当前文件里已有的 import 语句，便于过滤同名冲突导入。
    existing_simple_names = {}  # 保存当前文件里已导入的简单类名与完整 import 路径。
    for existing_import in existing_import_lines:  # 逐条解析当前文件里的 import。
        simple_name = _import_simple_name(existing_import)  # 读取当前 import 对应的简单名称。
        if simple_name:  # 只有可解析出简单名称时才记录。
            existing_simple_names.setdefault(simple_name, existing_import)  # 当前文件里已有的导入优先级最高，后续同名新导入会被过滤。
    planned_simple_names = dict(existing_simple_names)  # 再维护一份“本轮准备插入”的简单类名映射，避免同一批次里再次出现 single-type-import 冲突。
    for import_line in import_lines:  # 逐个处理传入的 import 行。
        normalized_import_line = import_line.strip()  # 去掉首尾空白后再参与比较，避免同一 import 因格式差异被重复插入。
        simple_name = _import_simple_name(normalized_import_line)  # 读取当前待插入 import 对应的简单名称。
        if simple_name and simple_name in planned_simple_names and planned_simple_names[simple_name] != normalized_import_line:  # 当前文件或本轮待插入列表里已经存在同名但不同路径的普通 import 时不能再追加。
            continue  # 跳过当前冲突导入，避免出现 fastjson 一类 single-type-import 冲突。
        if normalized_import_line not in cleaned_imports:  # 去重以避免重复写入。
            cleaned_imports.append(normalized_import_line)  # 只保留第一次出现的 import。
            if simple_name:  # 普通 import 进入待插入列表后也要记录其简单类名。
                planned_simple_names[simple_name] = normalized_import_line  # 后续再遇到同名不同库导入时就会被过滤掉。
    if not cleaned_imports:  # 没有需要插入的 import 时直接返回原文。
        return content  # 保持原文件不变。

    package_idx = None  # 记录 package 声明位置。
    import_indices = []  # 记录现有 import 行位置。
    for idx, line in enumerate(lines):  # 遍历当前文件的全部行。
        stripped = line.strip()  # 去掉空白后方便识别语句类型。
        if stripped.startswith('package '):  # 找到 package 声明。
            package_idx = idx  # 记录 package 行下标。
        if stripped.startswith('import '):  # 找到已有 import 声明。
            import_indices.append(idx)  # 记录 import 行下标。

    if import_indices:  # 如果文件已经存在 import 区块。
        insert_at = import_indices[-1] + 1  # 将新 import 插入到最后一个 import 后面。
    elif package_idx is not None:  # 如果只有 package 声明没有 import。
        insert_at = package_idx + 1  # 将新 import 插入到 package 之后。
    else:  # 没有 package 声明的极端情况。
        insert_at = 0  # 直接插到文件开头。

    trailing_newline = content.endswith('\n')  # 记住原文件是否以换行结尾。
    if package_idx is not None and not import_indices:  # package 后首次插入 import 时补一个空行更符合 Java 风格。
        lines = lines[:insert_at] + [''] + cleaned_imports + [''] + lines[insert_at:]  # 在 package 与 import、import 与正文之间各留一空行。
    else:  # 其余情况只在现有 import 区块后直接插入。
        lines = lines[:insert_at] + cleaned_imports + lines[insert_at:]  # 将新 import 追加到已有 import 之后。

    updated_content = '\n'.join(lines)  # 重新拼接为完整文件内容。
    if trailing_newline and not updated_content.endswith('\n'):  # 尽量保持原文件的换行风格。
        updated_content += '\n'  # 补回末尾换行。
    return updated_content  # 返回插入 import 后的文件文本。


def _import_simple_name(import_line: str) -> str:  # 从普通 import 语句中提取简单类名，static import 不参与该冲突检测。
    match = re.match(r'^\s*import\s+(?!static\b)([A-Za-z0-9_.]*\.([A-Za-z_][A-Za-z0-9_]*))\s*;\s*$', import_line or '')  # 只匹配普通 import。
    return match.group(2) if match else ''  # 成功时返回简单类名，否则返回空串。


def _extract_unreported_exception_fqcn(build_output: str) -> str:  # 从编译错误日志里提取“必须捕获或声明抛出”的异常全限定名。
    patterns = [  # 同时兼容英文和中文 Maven/Javac 错误输出。
        r'unreported exception\s+([\w$.]+)\s*;',  # 英文 javac 常见格式。
        r'未报告的异常错误\s*([\w$.]+)\s*;',  # 中文 javac 常见格式。
    ]  # 只提取一个异常全限定名供后续声明修复使用。
    for pattern in patterns:  # 按优先顺序逐个尝试模式匹配。
        match = re.search(pattern, build_output or '', re.IGNORECASE)  # 在完整构建输出里搜索异常全名。
        if match:  # 命中后直接返回。
            return match.group(1).strip()  # 返回去掉空白后的异常全限定名。
    return ''  # 未找到时返回空串。


def _extract_unreported_exception_context(build_output: str, test_file: str = '') -> Tuple[str, Optional[int]]:  # 从 checked exception 错误里同时提取异常全限定名和报错行号。
    normalized_test_file = os.path.abspath(test_file) if test_file else ''  # 统一规整目标测试文件路径，便于优先匹配当前文件。
    contextual_patterns = [  # 先尝试解析带文件路径和行号的编译错误行。
        r'((?:[A-Za-z]:)?[^:\s]+\.java):\[(\d+),\d+\][^\n]*unreported exception\s+([\w$.]+)\s*;',  # 英文 javac 常见格式。
        r'((?:[A-Za-z]:)?[^:\s]+\.java):\[(\d+),\d+\][^\n]*未报告的异常错误\s*([\w$.]+)\s*;',  # 中文 javac 常见格式。
    ]  # 这些模式允许我们直接回到真正出错的方法，而不是只靠请求里的测试方法名。
    for pattern in contextual_patterns:  # 按优先顺序逐个尝试带上下文的模式。
        for match in re.finditer(pattern, build_output or '', re.IGNORECASE):  # 顺序扫描完整构建输出。
            error_file = os.path.abspath(match.group(1))  # 读取当前错误行对应的源文件绝对路径。
            if normalized_test_file and error_file != normalized_test_file and os.path.basename(error_file) != os.path.basename(normalized_test_file):  # Docker 日志里的 `/workspace/...` 和宿主机绝对路径不同，但文件名一致时仍应视为同一个测试文件。
                continue
            return match.group(3).strip(), int(match.group(2))  # 返回异常全限定名和 1-based 行号。
    return _extract_unreported_exception_fqcn(build_output), None  # 找不到行号时退回旧逻辑，只保留异常全限定名。


def _find_enclosing_method_declaration(lines: List[str], line_index: int) -> Optional[int]:  # 根据报错行号回推它所在的方法声明位置。
    if line_index < 0 or line_index >= len(lines):  # 非法行号无法继续定位。
        return None  # 返回空值交给调用方回退到旧逻辑。
    for candidate_idx in range(line_index, -1, -1):  # 从报错行向上回溯寻找最近的方法声明。
        if not _looks_like_any_method_declaration(lines, candidate_idx):  # 当前行不像方法声明时继续向上找。
            continue
        method_end = _find_method_end(lines, candidate_idx)  # 取出当前候选方法的结束位置，确认报错行是否落在其范围内。
        if method_end is not None and candidate_idx <= line_index <= method_end:  # 只有真正包住报错行的方法才是可信候选。
            return candidate_idx  # 返回该方法的声明起始行。
    return None  # 回溯完整个文件仍未找到可用方法时返回空值。


def _looks_like_any_method_declaration(lines: List[str], line_idx: int) -> bool:  # 粗略判断当前行是否像一个 Java 方法声明。
    line = lines[line_idx].strip()  # 去掉首尾空白，便于做语法特征判断。
    if not line or line.startswith('//') or line.startswith('*') or line.startswith('@'):  # 注释和注解行本身不是方法声明。
        return False
    method_name_match = re.search(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', line)  # 提取当前行里最可能的方法名。
    if not method_name_match:  # 没有形似方法名的标识符时无法继续。
        return False
    method_name = method_name_match.group(1)  # 读取候选方法名。
    if method_name in {'if', 'for', 'while', 'switch', 'catch', 'new', 'return', 'assert'}:  # 这些关键字和语句都不是方法声明。
        return False
    return _is_method_declaration(lines, line_idx, method_name)  # 复用已有的声明判断逻辑做最后确认。


def _append_exception_to_method_prefix(method_prefix: str, exception_fqcn: str) -> str:  # 将新的 checked exception 幂等追加到方法声明的 throws 子句中。
    stripped_prefix = method_prefix.rstrip()  # 先去掉尾部空白便于操作 throws 子句。
    trailing_whitespace = method_prefix[len(stripped_prefix):]  # 保存原始尾部空白以尽量维持格式。
    existing_throws = _extract_throws_clause(stripped_prefix)  # 读取当前方法头里已有的 throws 列表。
    if existing_throws:  # 原方法已经声明了一些异常时追加到现有列表末尾。
        existing_items = [item.strip() for item in existing_throws.split(',') if item.strip()]  # 规范化现有异常列表。
        if exception_fqcn in existing_items:  # 当前异常已经被显式声明时无需重复添加。
            return method_prefix
        existing_items.append(exception_fqcn)  # 在现有异常列表末尾追加新的异常全名。
        updated_throws = ', '.join(existing_items)  # 重新拼接 throws 子句。
        replaced_prefix = re.sub(r'\bthrows\b[\s\S]*$', f'throws {updated_throws}', stripped_prefix)  # 用新的异常列表替换旧 throws 子句。
        return replaced_prefix + trailing_whitespace  # 补回原始尾部空白。
    return stripped_prefix + f' throws {exception_fqcn}' + trailing_whitespace  # 原方法没有 throws 时直接追加新的子句。


def _dependency_coordinate_key(dependency_snippet: str) -> str:  # 从 dependency XML 中提取 groupId:artifactId 坐标。
    group_match = re.search(r'<groupId>\s*([^<]+)\s*</groupId>', dependency_snippet or '', re.IGNORECASE)  # 提取 groupId。
    artifact_match = re.search(r'<artifactId>\s*([^<]+)\s*</artifactId>', dependency_snippet or '', re.IGNORECASE)  # 提取 artifactId。
    if not group_match or not artifact_match:  # 任一关键元素缺失时无法构造坐标。
        return ''  # 返回空串供上层退回到全文匹配。
    return f"{group_match.group(1).strip()}:{artifact_match.group(1).strip()}"  # 返回标准 groupId:artifactId 坐标。


def _pom_contains_dependency(pom_content: str, dependency_key: str) -> bool:  # 判断当前 pom 是否已经声明了同坐标依赖。
    if not dependency_key:  # 缺失坐标时无法做结构化判断。
        return False  # 交由上层回退到全文片段比较。
    group_id, artifact_id = dependency_key.split(':', 1)  # 拆出 groupId 与 artifactId。
    dependency_pattern = re.compile(r'<dependency>[\s\S]*?<groupId>\s*' + re.escape(group_id) + r'\s*</groupId>[\s\S]*?<artifactId>\s*' + re.escape(artifact_id) + r'\s*</artifactId>[\s\S]*?</dependency>', re.IGNORECASE)  # 按 dependency 块结构匹配同坐标依赖。
    return bool(dependency_pattern.search(pom_content or ''))  # 命中即说明依赖已存在。


def _normalize_xml_for_match(content: str) -> str:  # 为 XML 片段比较做保守的空白归一化。
    return re.sub(r'\s+', '', content or '')  # 去掉所有空白以便做幂等匹配。


def _insert_dependency_into_pom(pom_content: str, dependency_snippet: str) -> str:  # 将 dependency XML 幂等插入到现有 pom 中。
    normalized_dependency = dependency_snippet.strip()  # 统一去掉首尾空白以便后续插入。
    dependency_lines = [line.rstrip() for line in normalized_dependency.splitlines()]  # 保留现有相对缩进并去掉每行右侧空白。
    dependency_block = '\n'.join(('        ' + line.lstrip()) if line.strip() else '' for line in dependency_lines)  # 使用 Maven 常见的 8 空格缩进包裹 dependency 块。
    top_level_dependencies = _find_first_direct_project_tag_block(pom_content, 'dependencies')  # 只查找 project 直系子级的 dependencies，避免误插进 plugin/dependencyManagement。
    if top_level_dependencies is not None:  # 已存在顶层 dependencies 区块时直接追加到其末尾。
        _, _, close_start, _ = top_level_dependencies  # 读取顶层 `</dependencies>` 的起始位置。
        return pom_content[:close_start] + dependency_block + '\n    ' + pom_content[close_start:]  # 仅在顶层依赖区块末尾追加新的 dependency。
    if '</project>' in pom_content:  # 没有 dependencies 区块时在 project 结束前创建一段新的 dependencies。
        insertion_point = _find_first_direct_project_child_start(pom_content, {'build', 'reporting', 'profiles', 'modules'})  # 若存在 build/reporting 等直系子节点，则把新依赖块插在它们之前。
        if insertion_point is None:  # 没找到更合适的直系子节点时直接回退到 project 结束前。
            insertion_point = pom_content.rfind('</project>')  # 使用 project 结束标签作为最终插入点。
        insertion = '    <dependencies>\n' + dependency_block + '\n    </dependencies>\n'  # 构造新的顶层 dependencies 区块。
        return pom_content[:insertion_point] + insertion + pom_content[insertion_point:]  # 将新的依赖区块插入 project 直系层级。
    return pom_content  # 非标准 pom 结构下直接返回原文，让上层显式报错。


def _iter_xml_like_tags(content: str):  # 以轻量方式遍历 POM 里的 XML 风格标签，供顶层依赖区块定位复用。
    tag_pattern = re.compile(r'<!--[\s\S]*?-->|<(/?)([A-Za-z0-9_.:-]+)(?:\s[^<>]*?)?(/?)>', re.DOTALL)  # 兼容注释、普通标签与自闭合标签。
    for match in tag_pattern.finditer(content or ''):  # 顺序扫描整个 pom 文本。
        token = match.group(0)  # 读取当前匹配到的完整标签文本。
        if token.startswith('<!--'):  # XML 注释不参与层级计算。
            continue
        yield match.start(), match.end(), match.group(1) == '/', match.group(2).split(':')[-1], bool(match.group(3))  # 统一返回位置、闭合标记、无命名空间的标签名和是否自闭合。


def _find_first_direct_project_tag_block(content: str, tag_name: str) -> Optional[Tuple[int, int, int, int]]:  # 定位 project 直系子节点里的第一个目标标签区块。
    tags = list(_iter_xml_like_tags(content))  # 先把全部标签扫描出来，便于二次匹配闭合位置。
    stack: List[str] = []  # 维护当前 XML 层级栈。
    for index, (start, end, is_closing, name, is_self_closing) in enumerate(tags):  # 顺序遍历全部标签。
        if is_closing:  # 闭合标签只负责维护层级。
            if stack and stack[-1] == name:  # 只在结构吻合时弹栈。
                stack.pop()
            continue
        if stack == ['project'] and name == tag_name:  # 只有 project 直系子节点才是我们要插依赖的目标区块。
            if is_self_closing:  # `dependencies` 不应自闭合，但这里仍做保守处理。
                return start, end, end, end
            nested_depth = 1  # 从当前标签之后继续向下找到匹配的闭合标签。
            for nested_start, nested_end, nested_is_closing, nested_name, nested_is_self_closing in tags[index + 1:]:  # 顺序扫描后续标签。
                if nested_name != tag_name:  # 只对同名标签维护局部深度。
                    continue
                if not nested_is_closing and not nested_is_self_closing:  # 命中同名子标签时深度加一。
                    nested_depth += 1
                elif nested_is_closing:  # 命中同名闭合标签时深度减一。
                    nested_depth -= 1
                    if nested_depth == 0:  # 回到 0 时说明找到了当前直系子区块的闭合标签。
                        return start, end, nested_start, nested_end
            return None  # 找不到匹配闭合标签时返回空值，让调用方回退。
        if not is_self_closing:  # 普通开始标签进入层级栈。
            stack.append(name)
    return None  # project 直系层级里不存在目标标签时返回空值。


def _find_first_direct_project_child_start(content: str, tag_names: set) -> Optional[int]:  # 返回 project 直系子节点里第一个命中标签的起始位置。
    stack: List[str] = []  # 维护当前 XML 层级栈。
    for start, _, is_closing, name, is_self_closing in _iter_xml_like_tags(content):  # 顺序扫描全部标签。
        if is_closing:  # 闭合标签只负责维护层级。
            if stack and stack[-1] == name:  # 结构匹配时才弹栈。
                stack.pop()
            continue
        if stack == ['project'] and name in tag_names:  # 只有 project 直系子节点才是稳定插入点。
            return start  # 返回当前命中的标签起始位置。
        if not is_self_closing:  # 普通开始标签进入层级栈。
            stack.append(name)
    return None  # 没找到合适的直系子节点时返回空值。
