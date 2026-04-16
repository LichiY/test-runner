"""Patch application logic for flaky tests.

Uses a robust method-name-based approach:
1. Find the test file by class name
2. Locate the target method by method name + regex
3. Extract the full method using brace counting
4. Replace it with the generated patch
5. Verify the replacement was correct
"""

import difflib  # 使用文本相似度帮助选择最可能的目标文件和方法。
import glob  # 用于按模式搜索参考补丁文件。
import logging
import os
import re
import shutil
import ast  # 用于宽松解析参考补丁 import 段里出现的 Python 列表文本。
import csv  # 用于从本地补丁数据集中补充参考候选。
from functools import lru_cache  # 用于缓存本地补丁数据集索引，避免每条样本都重复全表扫描。
from dataclasses import dataclass  # 用于承载参考补丁候选信息。
from typing import Dict, List, Optional, Tuple

from .data import TestEntry

logger = logging.getLogger(__name__)

REFERENCE_PATCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'nondex_script', 'patch'))  # 保存参考补丁库根目录。
REFERENCE_DATASET_CSV = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'patch-data', 'cleaned_mutation_data.csv'))  # 保留本地清洗补丁数据集路径用于离线分析，但运行时不再把 fixed_code 当作可回放补丁来源。

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
    'SortedSet': 'java.util.SortedSet',  # SortedSet 属于 java.util。
    'TreeMap': 'java.util.TreeMap',  # TreeMap 属于 java.util。
    'TreeSet': 'java.util.TreeSet',  # TreeSet 属于 java.util。
}

JAVA_STATIC_IMPORT_CANDIDATES = {  # 为常见的静态断言与匹配器方法提供保守的 static import 推断表。
    'containsString': 'org.hamcrest.Matchers.containsString',  # containsString 常见于 Hamcrest 匹配器。
    'containsInAnyOrder': 'org.hamcrest.Matchers.containsInAnyOrder',  # containsInAnyOrder 常见于 Hamcrest 匹配器。
    'tuple': 'org.assertj.core.groups.Tuple.tuple',  # tuple 常见于 AssertJ 分组断言。
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
    'Option': 'import com.jayway.jsonpath.Option;',
    'JSONAssert': 'import org.skyscreamer.jsonassert.JSONAssert;',
    'JSONCompareMode': 'import org.skyscreamer.jsonassert.JSONCompareMode;',
    'JsonObject': 'import com.google.gson.JsonObject;',
    'JsonParser': 'import com.google.gson.JsonParser;',
    'JsonElement': 'import com.google.gson.JsonElement;',
    'JsonNode': 'import com.fasterxml.jackson.databind.JsonNode;',
    'ObjectMapper': 'import com.fasterxml.jackson.databind.ObjectMapper;',
    'JsonProcessingException': 'import com.fasterxml.jackson.core.JsonProcessingException;',
    'JSONException': 'import org.json.JSONException;',
}  # 这些符号在当前失败集里高频出现，并且基本都对应稳定的第三方类。

REFERENCE_CODE_STATIC_IMPORT_INFERENCE = {  # 这些静态导入规则来自离线成功样本，只有正文明确使用 helper 时才会补入。
    'assertThatJson': 'import static net.javacrumbs.jsonunit.assertj.JsonAssertions.assertThatJson;',
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


@dataclass(frozen=True)  # 用结构化对象承载参考补丁候选，方便排序和日志记录。
class ReferencePatchCandidate:
    source_path: str  # 记录候选来自哪一个参考补丁文件。
    test_code: str  # 保存可直接替换测试方法的候选代码。
    imports: Tuple[str, ...] = ()  # 保留参考补丁里声明的 import 片段，便于后续诊断。
    pom_snippet: str = ''  # 保留参考补丁里的 pom 依赖提示，便于排序和排查。


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


def find_reference_patch_candidates(entry, reference_root: str = REFERENCE_PATCH_ROOT, dataset_csv: str = REFERENCE_DATASET_CSV) -> List[ReferencePatchCandidate]:  # 从参考补丁库读取同仓库、同提交、同测试的成功补丁产物，作为上下文来源而不是替代补丁来源。
    project_name = getattr(entry, 'project_name', '').strip()  # 读取项目名以匹配参考补丁目录结构。
    original_sha = getattr(entry, 'original_sha', '').strip()  # 读取原始提交号以缩小搜索范围。
    test_class = getattr(entry, 'test_class', '').strip()  # 读取测试类名。
    test_method = getattr(entry, 'test_method', '').strip()  # 读取测试方法名。
    if not project_name or not original_sha or not test_class or not test_method:  # 任一关键键缺失时都无法安全定位参考补丁。
        return []  # 返回空列表让上层保持原有失败判定。
    test_key = f'{test_class}.{test_method}'  # 参考补丁目录以 `类名.方法名` 命名。
    patch_paths = _reference_patch_paths(reference_root=reference_root, project_name=project_name, original_sha=original_sha, test_key=test_key)  # 支持不同目录层级的 patch 家族，并在必要时回退到同项目同测试名的任意 SHA 候选。
    if not patch_paths:  # 运行时只信任参考补丁库里的成功产物，fixed_code 仅用于离线分析。
        return []  # 当前案例说明本地参考源里没有可用候选。
    collected_candidates: Dict[str, ReferencePatchCandidate] = {}  # 基于规范化补丁文本聚合同代码候选，避免把更完整的上下文信息在去重时丢掉。
    for patch_path in patch_paths:  # 逐个解析匹配到的参考补丁文件。
        for candidate in _parse_reference_patch_file(patch_path):  # 一个 patch 文件里可能包含多个 round 的候选代码。
            normalized_code = _normalize_code_for_match(candidate.test_code)  # 用规范化后的方法文本做去重键，避免仅因格式不同重复尝试。
            if not normalized_code:  # 空候选无需继续处理。
                continue  # 跳过当前候选。
            existing_candidate = collected_candidates.get(normalized_code)  # 检查是否已经有同内容候选。
            if existing_candidate is None:  # 首次看到当前代码时直接保存。
                collected_candidates[normalized_code] = candidate  # 写入当前候选。
                continue  # 继续处理后续候选。
            collected_candidates[normalized_code] = _merge_reference_patch_candidates(existing_candidate, candidate)  # 合并散落在不同来源里的 import 与 pom 信息。
    sorted_candidates = sorted(collected_candidates.values(), key=lambda candidate: _reference_candidate_sort_key(candidate, entry))  # 先尝试与当前 generated_patch 更接近且上下文更完整的成功候选。
    return sorted_candidates  # 返回按优先级排序后的参考补丁候选列表。


def find_reference_context_candidates(entry, reference_root: str = REFERENCE_PATCH_ROOT, similarity_threshold: float = 0.85) -> List[ReferencePatchCandidate]:  # 仅返回与当前 generated_patch 足够接近、可作为上下文供给器的成功参考补丁。
    if not getattr(entry, 'generated_patch', '').strip():  # 没有原始生成补丁时就没有“同一补丁”的上下文匹配可言。
        return []  # 直接返回空列表，避免误把别的补丁当成本案例上下文。
    matched_candidates = []  # 保存与当前 generated_patch 足够接近的参考候选。
    for candidate in find_reference_patch_candidates(entry, reference_root=reference_root):  # 先读取同案例的全部成功参考补丁。
        if _reference_candidate_patch_similarity(candidate, entry) < similarity_threshold:  # 只有当参考补丁与当前 generated_patch 足够接近时，才允许借用它的 import/pom 上下文。
            continue  # 跳过语义已经明显不是同一个补丁的候选，避免变相替换被评估补丁。
        matched_candidates.append(candidate)  # 记录当前可用的上下文候选。
    return matched_candidates  # 返回与当前被评估补丁相匹配的成功上下文候选。


def _reference_patch_paths(reference_root: str, project_name: str, original_sha: str, test_key: str) -> List[str]:  # 在 patch 目录中同时支持不同家族的目录层级，并在必要时放宽到同项目同测试名的任意 SHA。
    if not reference_root or not os.path.isdir(reference_root):  # patch 根目录不存在时直接返回空列表。
        return []  # 保持主流程继续尝试其他来源。
    exact_pattern = os.path.join(reference_root, '**', project_name, original_sha, '**', test_key, '*.patch')  # 先精确匹配当前项目、当前 SHA 和当前测试名。
    exact_paths = sorted(set(glob.glob(exact_pattern, recursive=True)))  # 去重并稳定排序，兼容 `gpt/gpt1/all_rounds` 与 `magicoder/all_rounds` 等不同层级。
    if exact_paths:  # 只要 exact-sha 已命中，就优先使用这些来源。
        return exact_paths  # 返回 exact-sha 候选列表。
    fallback_pattern = os.path.join(reference_root, '**', project_name, '*', '**', test_key, '*.patch')  # exact-sha 缺失时再回退到同项目同测试名的任意 SHA 候选。
    return sorted(set(glob.glob(fallback_pattern, recursive=True)))  # 返回去重后的回退候选列表。


@lru_cache(maxsize=4)  # 不同测试回合会反复访问同一个 CSV，缓存索引可以避免重复全表扫描。
def _dataset_reference_index(dataset_csv: str) -> Dict[Tuple[str, str, str, str], List[Tuple[int, Dict[str, str]]]]:  # 为本地补丁数据集构建按项目、SHA、模块与测试名索引的候选表。
    index: Dict[Tuple[str, str, str, str], List[Tuple[int, Dict[str, str]]]] = {}  # 保存 exact-match 键到候选行的映射。
    if not dataset_csv or not os.path.isfile(dataset_csv):  # 数据集文件不存在时直接返回空索引。
        return index  # 交由上层继续 patch 目录来源。
    try:  # 单个 CSV 解析失败不应中断主流程。
        with open(dataset_csv, 'r', encoding='utf-8', errors='ignore', newline='') as f:  # 以宽松模式读取本地补丁数据集。
            for row_number, row in enumerate(csv.DictReader(f), start=2):  # 记录真实 CSV 行号，便于后续把来源写进诊断信息。
                key = _dataset_reference_lookup_key(project_name=row.get('project_name', ''), original_sha=row.get('original_sha', ''), module=row.get('module', ''), full_test_name=row.get('full_test_name', ''))  # 生成当前数据行的 exact-match 索引键。
                if not all(key):  # 缺少关键列的脏数据行无法安全用于参考补丁回退。
                    continue  # 跳过当前行。
                if not (row.get('fixed_code', '').strip() or row.get('generated_patch', '').strip()):  # 没有任何可直接尝试的方法代码时无需入索引。
                    continue  # 跳过没有候选代码的行。
                index.setdefault(key, []).append((row_number, row))  # 追加到当前 exact-match 键的候选列表中。
    except Exception:  # 数据集读取失败时返回空索引即可。
        return {}  # 让主流程回退到 patch 目录来源。
    return index  # 返回缓存好的候选索引。


def _dataset_reference_lookup_key(project_name: str, original_sha: str, module: str, full_test_name: str) -> Tuple[str, str, str, str]:  # 统一规整数据集与运行请求之间的匹配键。
    return (project_name.strip().lower(), original_sha.strip().lower(), (module or '.').strip().lower(), _normalize_reference_test_name(full_test_name))  # 用大小写无关且兼容重复方法尾缀的键减少格式噪声。


def _dataset_reference_candidates(dataset_csv: str, project_name: str, original_sha: str, module: str, full_test_name: str) -> List[ReferencePatchCandidate]:  # 从本地清洗补丁数据集中提取当前案例的 exact-match 候选。
    lookup_key = _dataset_reference_lookup_key(project_name=project_name, original_sha=original_sha, module=module, full_test_name=full_test_name)  # 生成当前案例的 exact-match 键。
    matched_rows = _dataset_reference_index(dataset_csv).get(lookup_key, [])  # 读取当前案例命中的全部数据集行。
    candidates: List[ReferencePatchCandidate] = []  # 保存从数据集提取出的候选补丁。
    for row_number, row in matched_rows:  # 顺序处理每个 exact-match 数据集行。
        source_prefix = f'{dataset_csv}#{row_number}'  # 用 `文件#行号` 标记候选来源，便于失败诊断直接回溯。
        fixed_code = (row.get('fixed_code', '') or '').strip()  # 读取人工修复代码文本。
        generated_patch = (row.get('generated_patch', '') or '').strip()  # 读取模型生成补丁文本。
        if fixed_code:  # 人工修复代码通常是最稳定的“可编译参考”来源。
            candidates.append(ReferencePatchCandidate(source_path=f'{source_prefix}:fixed_code', test_code=fixed_code))  # 把 fixed_code 当作最高优先级候选之一。
        if generated_patch and _normalize_code_for_match(generated_patch) != _normalize_code_for_match(fixed_code):  # 避免在 fixed_code 与 generated_patch 完全相同的情况下重复追加。
            marker = 'generated_patch_correct' if _is_truthy_label(row.get('isCorrect', '')) else 'generated_patch'  # 用标签区分“已知正确”的模型补丁和普通模型补丁。
            candidates.append(ReferencePatchCandidate(source_path=f'{source_prefix}:{marker}', test_code=generated_patch))  # 将生成补丁也纳入候选池供后续排序。
    return candidates  # 返回从数据集提取出的所有候选。


def _is_truthy_label(value: str) -> bool:  # 统一解析数据集里的布尔或 0/1 风格标签。
    return str(value or '').strip().lower() in {'1', '1.0', 'true', 'yes'}  # 接受当前数据集里常见的“真”标签写法。


def _parse_reference_patch_file(patch_path: str) -> List[ReferencePatchCandidate]:  # 从单个参考补丁文件中提取一个或多个可直接尝试的 test_code 候选。
    try:  # 解析失败时不应中断主流程。
        with open(patch_path, 'r', encoding='utf-8', errors='ignore') as f:  # 以宽松模式读取参考补丁文件。
            lines = f.read().splitlines()  # 按行处理比直接正则更能兼容两种 patch 文件格式。
    except Exception:  # 个别参考补丁读取失败时直接跳过。
        return []  # 返回空列表让上层继续处理其他候选文件。
    candidates: List[ReferencePatchCandidate] = []  # 保存当前文件中解析出的所有 test_code 候选。
    index = 0  # 用游标逐行扫描整个补丁文件。
    while index < len(lines):  # 顺序遍历每一行寻找 `test_code:` 区块。
        if lines[index].strip() != 'test_code:':  # 只有命中 test_code 标记时才开始提取候选。
            index += 1  # 继续扫描下一行。
            continue  # 当前行不属于候选补丁正文。
        index += 1  # 跳过 `test_code:` 标记行本身。
        while index < len(lines) and not lines[index].strip():  # 跳过 test_code 后的空行。
            index += 1  # 对齐到候选代码正文的第一行。
        code_lines: List[str] = []  # 收集当前候选的 test_code 正文。
        while index < len(lines) and lines[index].strip() != 'import:':  # 一直读到 import 标记为止。
            code_lines.append(lines[index])  # 保留原始缩进和换行风格，便于后续直接作为方法补丁尝试。
            index += 1  # 继续读取下一行。
        code = '\n'.join(code_lines).strip()  # 规范化当前候选代码。
        if index >= len(lines) or lines[index].strip() != 'import:':  # 没有 import 标记说明文件结构异常，停止解析当前文件。
            break  # 直接结束以免把后续文本误当成代码。
        index += 1  # 跳过 `import:` 标记行本身。
        while index < len(lines) and not lines[index].strip():  # 跳过 import 段前的空行。
            index += 1  # 对齐到 import 段正文。
        import_lines: List[str] = []  # 收集当前候选的 import 提示。
        while index < len(lines) and lines[index].strip() != 'pom:':  # 读取直到 pom 标记。
            import_lines.append(lines[index])  # 保留 import 原始文本供日志和排序使用。
            index += 1  # 继续读取下一行。
        if index >= len(lines) or lines[index].strip() != 'pom:':  # 缺少 pom 标记同样说明结构异常。
            break  # 停止解析当前文件。
        index += 1  # 跳过 `pom:` 标记。
        while index < len(lines) and not lines[index].strip():  # 跳过 pom 段前的空行。
            index += 1  # 对齐到 pom 段正文。
        pom_lines: List[str] = []  # 收集当前候选的 pom 提示文本。
        while index < len(lines) and lines[index].strip() != 'test_code:' and not lines[index].startswith('ROUND '):  # 到下一个候选或下一个 round 之前都视为 pom 段的一部分。
            pom_lines.append(lines[index])  # 保留 pom 原始文本供排序与错误输出使用。
            index += 1  # 继续读取下一行。
        if not code or code == 'None':  # 无实际代码的候选没有尝试价值。
            continue  # 直接跳过当前候选。
        cleaned_imports = tuple(line.strip() for line in import_lines if line.strip())  # 去掉 import 段的空白行并转为不可变元组。
        cleaned_imports = _normalize_reference_import_lines(cleaned_imports)  # 兼容 `[]`、Python 列表文本和普通多行 import 三种格式。
        pom_snippet = '\n'.join(pom_lines).strip()  # 将 pom 段恢复成便于诊断的多行文本。
        candidates.append(ReferencePatchCandidate(source_path=patch_path, test_code=code, imports=cleaned_imports, pom_snippet=pom_snippet))  # 记录当前解析出的候选补丁。
    return candidates  # 返回当前文件中的所有候选代码。


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


def _merge_reference_patch_candidates(primary: ReferencePatchCandidate, secondary: ReferencePatchCandidate) -> ReferencePatchCandidate:  # 在同一段 test_code 来自多个来源时合并它们的上下文信息。
    imports = []  # 收集合并后的 import 列表并保持稳定顺序。
    for import_line in primary.imports + secondary.imports:  # 按出现顺序合并两边的 import。
        if import_line and import_line not in imports:  # 去重以避免重复写入。
            imports.append(import_line)  # 保留当前 import。
    best_candidate = primary if _reference_candidate_priority(primary) <= _reference_candidate_priority(secondary) else secondary  # 保留优先级更好的来源路径与主体信息。
    pom_snippet = _prefer_meaningful_pom_snippet(best_candidate.pom_snippet, primary.pom_snippet, secondary.pom_snippet)  # 忽略 `None` 这类占位值，优先保留真正有内容的依赖片段。
    return ReferencePatchCandidate(source_path=best_candidate.source_path, test_code=best_candidate.test_code, imports=tuple(imports), pom_snippet=pom_snippet)  # 返回合并后的候选对象。


def _prefer_meaningful_pom_snippet(*snippets: str) -> str:  # 从多个候选 pom 文本里挑出真正有意义的一份。
    for snippet in snippets:  # 按优先顺序依次检查候选 pom 文本。
        normalized = (snippet or '').strip()  # 去掉空白后再判断是否只是占位符。
        if not normalized or normalized == 'None':  # 空串或 `None` 都不算真正的 pom 上下文。
            continue  # 继续检查后续候选。
        return snippet  # 返回首个真正有内容的 pom 片段。
    return ''  # 所有候选都没有有效 pom 时返回空串。


def _reference_candidate_priority(candidate: ReferencePatchCandidate) -> Tuple[int, int, int, int, int]:  # 为参考补丁候选生成越小越优的排序键。
    source_rank = _reference_candidate_source_rank(candidate)  # 运行时只在成功补丁产物之间排序，优先 GoodPatches。
    explicit_context_rank = 0 if _candidate_has_explicit_reference_context(candidate) else 1  # 对当前语义来说，带显式 import/pom 的成功补丁更有价值。
    risky_code_rank = 1 if _candidate_uses_risky_json_helpers(candidate.test_code) else 0  # 明显依赖幻觉 helper 的候选继续后移。
    return (source_rank, explicit_context_rank, risky_code_rank, len(candidate.test_code), 0)  # 最后用代码长度打破平局，优先尝试更集中的候选。


def _reference_candidate_sort_key(candidate: ReferencePatchCandidate, entry: TestEntry) -> Tuple[int, float, int, int, int, int, int]:  # 将“来源可信度、结构保持程度、API 兼容性”合并成统一排序键。
    normalized_source_path = (candidate.source_path or '').replace(os.sep, '/')  # 统一路径分隔符，便于判断当前候选是否来自 exact-sha。
    exact_sha_rank = 0 if f'/{getattr(entry, "original_sha", "").strip()}/' in normalized_source_path else 1  # 只优先当前提交号下的成功补丁产物。
    compatibility_rank = _reference_candidate_compatibility_rank(candidate)  # 把明显依赖幻觉 helper 的候选放到后面。
    source_rank = _reference_candidate_source_rank(candidate)  # GoodPatches 先于 all_rounds。
    structure_distance = round(1.0 - _reference_candidate_patch_similarity(candidate, entry), 4)  # 参考补丁必须首先像当前被评估的 generated_patch，而不是像 fixed_code。
    priority_tail = _reference_candidate_priority(candidate)  # 最后复用已有的 pom/第三方依赖保守排序。
    return (exact_sha_rank, compatibility_rank, source_rank, structure_distance, *priority_tail)  # Python 会按元组逐项升序排序，越小越优。


def _reference_candidate_compatibility_rank(candidate: ReferencePatchCandidate) -> int:  # 按“在原始 SHA 上是否容易直接编过”给候选分级。
    signals = '\n'.join(filter(None, [candidate.test_code, '\n'.join(candidate.imports), candidate.pom_snippet]))  # 合并代码、import 和 pom 片段做整体判断。
    if any(marker in signals for marker in REFERENCE_PROJECT_HELPER_MARKERS):  # 这些 helper 在失败样本里已经确认经常不存在于原始项目。
        return 2  # 明显依赖缺失 helper 的候选排在最后。
    if _candidate_uses_risky_json_helpers(candidate.test_code):  # 其余只在名字层面看起来像幻觉 helper 的候选次之。
        return 1  # 不是直接排除，但要放在标准 API 候选后面。
    return 0  # JSONAssert、JsonPath、ObjectMapper 这类稳定库 API 不再被误判成“不兼容”。


def _reference_candidate_structure_similarity(candidate: ReferencePatchCandidate, entry: TestEntry) -> float:  # 计算候选与原始数据三类方法文本的最大结构相似度。
    return _reference_candidate_patch_similarity(candidate, entry)  # 兼容旧调用点，但运行时只围绕当前 generated_patch 做比较。


def _reference_candidate_source_rank(candidate: ReferencePatchCandidate) -> int:  # 为不同来源类型建立一个稳定的次级优先级。
    source_path = candidate.source_path or ''  # 统一处理空来源路径。
    if 'GoodPatches' in source_path:  # 显式标记为 GoodPatches 的参考补丁通常已经过额外筛选。
        return 0
    if 'all_rounds' in source_path:  # 其他 patch 生成轮次仍可作为上下文线索，但优先级低于 GoodPatches。
        return 1
    return 2  # 其余来源最后再试。


def _candidate_requires_extra_pom(candidate: ReferencePatchCandidate) -> bool:  # 判断参考补丁是否显式要求补充 pom 依赖。
    normalized_pom = (candidate.pom_snippet or '').strip()  # 统一去掉首尾空白后再判断。
    if normalized_pom and normalized_pom != 'None' and 'No additional dependencies needed' not in normalized_pom and '<dependency>' in normalized_pom:  # 显式声明 dependency 的候选天然需要 pom 变更。
        return True  # 直接返回真以便在排序时把它们放后面。
    return bool(_infer_reference_dependency_snippets(getattr(candidate, 'test_code', ''), tuple(getattr(candidate, 'imports', ()) or ()), normalized_pom))  # 对未显式给出 pom 的候选，也按代码正文推断是否仍然需要额外依赖。


def _candidate_uses_risky_json_helpers(test_code: str) -> bool:  # 识别那些依赖额外 JSON helper 或三方库的高风险候选。
    risky_markers = ('assertJsonEqualsNonStrict', 'assertJsonStringEquals', 'assertJSONEqual(', 'assertJsonEquals(', 'assertJsonArrayEquals', 'assertJsonObjectEquals', 'assertThatJson(')  # 这些标记要么像幻觉 helper，要么会额外引入 json-unit 这类更重的上下文。
    return any(marker in (test_code or '') for marker in risky_markers)  # 只有命中明确的幻觉 helper 时才把候选后移。


def _looks_like_third_party_json_import(import_line: str) -> bool:  # 用于识别参考补丁 import 段里带来的高风险 JSON 依赖。
    risky_import_markers = ('com.jayway.jsonpath', 'org.skyscreamer.jsonassert', 'net.javacrumbs.jsonunit', 'org.json.', 'com.google.gson')  # 这些库在失败样本里经常导致额外编译问题。
    return any(marker in (import_line or '') for marker in risky_import_markers)  # 命中任一标记即视为高风险 JSON import。


def _reference_candidate_patch_similarity(candidate: ReferencePatchCandidate, entry: TestEntry) -> float:  # 计算成功参考补丁与当前被评估 generated_patch 的结构相似度。
    generated_patch = getattr(entry, 'generated_patch', '')  # 运行时只允许围绕当前真正被评估的补丁建立上下文。
    if not generated_patch:  # 缺少生成补丁时无法继续比较。
        return 0.0
    return _method_similarity(candidate.test_code, generated_patch)  # 返回当前参考补丁和被评估补丁之间的相似度。


def _candidate_has_explicit_reference_context(candidate: ReferencePatchCandidate) -> bool:  # 判断候选是否显式给出了 import 或 pom 上下文。
    if tuple(getattr(candidate, 'imports', ()) or ()):  # 只要明确给出了 import 段，就说明这条成功样本提供了可直接复用的上下文。
        return True
    normalized_pom = (getattr(candidate, 'pom_snippet', '') or '').strip()  # 读取并规整 pom 片段。
    return bool(normalized_pom and normalized_pom != 'None' and '<dependency>' in normalized_pom)  # 只有真正声明了 dependency 才算显式上下文。


def _normalize_reference_test_name(full_test_name: str) -> str:  # 统一规整测试名，兼容部分输入里“方法名重复两次”的格式。
    parts = [part for part in (full_test_name or '').strip().lower().split('.') if part]  # 先按点号拆成稳定的小写片段。
    if len(parts) >= 2 and parts[-1] == parts[-2]:  # 像 `Class.test.test` 这种尾部重复方法名只保留一次。
        parts = parts[:-1]
    return '.'.join(parts)  # 返回归一化后的测试名。


def _collect_reference_import_lines(candidate: ReferencePatchCandidate, entry: Optional[TestEntry] = None) -> List[str]:  # 汇总参考补丁显式给出的 import 与可从候选代码和原始方法签名推断出的 import。
    explicit_imports = _normalize_reference_import_lines(tuple(getattr(candidate, 'imports', ()) or ()))  # 先统一解析候选显式 import 段。
    inferred_imports = _infer_reference_import_lines(getattr(candidate, 'test_code', ''), getattr(entry, 'flaky_code', '') if entry else '')  # 再结合原始 flaky 方法签名推断补丁正文里没有显式出现的异常和类型。
    return list(dict.fromkeys(explicit_imports + inferred_imports))  # 去重并保持出现顺序稳定。


def _infer_reference_import_lines(*code_fragments: str) -> Tuple[str, ...]:  # 根据参考补丁正文和原始方法签名共同推断高频第三方 import 与 static import。
    code = '\n'.join(fragment for fragment in code_fragments if fragment)  # 把候选代码和原始 flaky 方法拼成统一信号串，兼顾“保留原声明”后的异常类型。
    inferred_imports: List[str] = []  # 保存按顺序推断出的 import 语句。
    for symbol, import_line in REFERENCE_CODE_IMPORT_INFERENCE.items():  # 逐个检查常见第三方类型符号。
        if re.search(rf'\b{re.escape(symbol)}\b', code):  # 只有代码正文明确出现该符号时才推断 import。
            inferred_imports.append(import_line)  # 记录当前推断出的普通 import。
    for helper_name, import_line in REFERENCE_CODE_STATIC_IMPORT_INFERENCE.items():  # 再检查高频 helper 的静态导入。
        if re.search(rf'\b{re.escape(helper_name)}\s*\(', code):  # 仅在代码里实际调用 helper 时才补对应 static import。
            inferred_imports.append(import_line)  # 记录当前推断出的 static import。
    return tuple(dict.fromkeys(inferred_imports))  # 去重并保持顺序稳定。


def _collect_reference_dependency_snippets(candidate: ReferencePatchCandidate, entry: Optional[TestEntry] = None) -> List[str]:  # 汇总参考补丁显式给出的 dependency 片段与可从候选代码和原始方法签名推断出的测试依赖。
    explicit_snippets = _extract_dependency_snippets(getattr(candidate, 'pom_snippet', ''))  # 先提取候选显式声明的所有 dependency 块。
    inferred_snippets = _infer_reference_dependency_snippets(  # 再根据代码正文和 import 线索推断缺失依赖。
        getattr(candidate, 'test_code', ''),
        tuple(getattr(candidate, 'imports', ()) or ()),
        getattr(candidate, 'pom_snippet', ''),
        getattr(entry, 'flaky_code', '') if entry else '',
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


def _infer_reference_dependency_snippets(test_code: str, import_lines: Tuple[str, ...], pom_snippet: str, supplemental_code: str = '') -> List[str]:  # 根据参考补丁正文、原始方法签名与 import 线索推断常见测试依赖。
    signals = '\n'.join(filter(None, [test_code or '', supplemental_code or '', '\n'.join(import_lines or ()), pom_snippet or '']))  # 把候选代码、原始方法签名和显式上下文一起拼成统一信号串。
    inferred_dependencies: List[str] = []  # 保存按顺序推断出的 dependency 片段。
    dependency_markers = {  # 当前失败集中高频出现的第三方 JSON 相关依赖标记。
        'assertj-core': ('Assertions.', 'org.assertj.core.api.Assertions', 'assertThat('),
        'commons-collections4': ('CollectionUtils', 'org.apache.commons.collections4'),
        'json-path': ('JsonPath', 'DocumentContext', 'ReadContext', 'Configuration', 'Option', 'com.jayway.jsonpath'),
        'jsonassert': ('JSONAssert', 'JSONCompareMode', 'org.skyscreamer.jsonassert'),
        'gson': ('JsonObject', 'JsonParser', 'JsonElement', 'com.google.gson'),
        'jackson-databind': ('JsonNode', 'ObjectMapper', 'JsonProcessingException', 'com.fasterxml.jackson'),
        'json': ('JSONException', 'org.json.'),
        'json-unit-assertj': ('assertThatJson', 'net.javacrumbs.jsonunit'),
    }  # 用少量高置信度 marker 覆盖当前 v3 剩余失败里最常见的依赖缺口。
    for dependency_key, markers in dependency_markers.items():  # 逐类检查是否命中依赖信号。
        if any(marker in signals for marker in markers):  # 只要命中任一 marker 即说明当前候选依赖对应库。
            inferred_dependencies.append(REFERENCE_DEPENDENCY_SNIPPETS[dependency_key])  # 追加对应的标准 dependency 片段。
    return inferred_dependencies  # 返回推断出的 dependency 片段列表。


def apply_reference_patch_context(repo_dir: str, entry: TestEntry, test_file: str, candidate: ReferencePatchCandidate) -> Tuple[bool, str]:  # 将“已知成功代码片段里显式或可推断的上下文”同步应用到工作区。
    change_messages = []  # 收集本次上下文补齐的摘要说明，便于写入失败诊断。
    import_lines = _collect_reference_import_lines(candidate, entry)  # 汇总显式 import 与从参考补丁正文、原始方法声明推断出的 import。
    if import_lines:  # 只要存在 import 上下文就尝试同步到目标测试文件。
        if not backup_file(test_file):  # 目标测试文件缺少备份时无法安全回退。
            return False, f"Failed to backup test file before applying reference imports: {os.path.basename(test_file)}"
        imported, import_msg = apply_import_context(test_file, import_lines)  # 将候选 import 合并到当前测试文件。
        if not imported:  # import 应用失败时直接返回，让上层继续尝试其他候选。
            return False, import_msg
        if import_msg:  # 只有真正发生变化时才记录摘要。
            change_messages.append(import_msg)
    dependency_snippets = _collect_reference_dependency_snippets(candidate, entry)  # 汇总显式与推断出的 dependency 片段。
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
    exception_fqcn = _extract_unreported_exception_fqcn(build_output)  # 从构建输出中提取最具体的异常全限定名。
    if not exception_fqcn:  # 没有 unreported exception 提示时无需处理。
        return False, "No unreported checked exception found in build output"
    try:  # 读取失败时直接返回错误。
        with open(test_file, 'r', encoding='utf-8') as f:  # 读取当前测试文件。
            original_content = f.read()  # 保存原文以便后续判断是否发生修改。
    except Exception as e:  # 文件读取失败时返回明确错误。
        return False, f"Failed to read test file for checked exception repair: {e}"
    lines = original_content.splitlines()  # 将源码按行拆分，便于复用现有的方法定位逻辑。
    method_start = _find_method_declaration(lines, method_name, '')  # 直接按目标方法名定位当前测试方法。
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
            if not _is_safe_symbol_case_replacement(symbol, actual_symbol):  # 仅允许明显属于类型名的大小写修正，避免把局部变量误改成类名。
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

    for i in range(start_idx, len(lines)):
        line = lines[i]
        j = 0
        while j < len(line):
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
    current_error_matches_target = not target_name  # 未提供文件名时默认接受所有缺失符号。
    for raw_line in build_output.splitlines():  # 逐行分析编译输出。
        line = raw_line.strip()  # 去掉首尾空白后便于做模式匹配。
        if ('cannot find symbol' in line or '找不到符号' in line):  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = (target_name in line) if target_name else True  # 只有目标文件对应的错误块才参与后续符号提取。
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
    methods = []  # 按出现顺序收集缺失方法。
    target_name = os.path.basename(test_file) if test_file else ''  # 提取目标测试文件名用于过滤错误块。
    current_error_matches_target = not target_name  # 未提供文件名时默认接受所有缺失方法。
    for raw_line in build_output.splitlines():  # 逐行分析编译输出。
        line = raw_line.strip()  # 去掉首尾空白后便于做模式匹配。
        if ('cannot find symbol' in line or '找不到符号' in line):  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = (target_name in line) if target_name else True  # 只有目标文件对应的错误块才参与后续方法提取。
        match = re.search(r'(?:symbol|符号)\s*:\s*(?:method|方法)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(', line, re.IGNORECASE)  # 兼容中英文 Maven 输出中的缺失方法格式。
        if not match:  # 当前行不包含缺失方法信息时继续。
            continue  # 跳到下一行。
        if not current_error_matches_target:  # 非目标文件的缺失方法不能拿来修当前测试文件。
            continue  # 跳过无关错误块中的方法。
        method_name = match.group(1)  # 提取缺失的方法名。
        if method_name not in methods:  # 保持顺序的同时避免重复。
            methods.append(method_name)  # 记录新的缺失方法。
    return methods  # 返回收集到的缺失方法列表。


def _resolve_missing_symbol_reference(test_file: str, symbol: str) -> Optional[Tuple[str, Optional[str]]]:  # 为缺失符号解析安全的实际类名与 import 路径。
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
    assertj_pattern = re.compile(rf'assertThat\s*\([^)]*\)\s*\.\s*(?:{method_pattern})\b')  # AssertJ 常见链式方法通常直接跟在 `assertThat(...)` 之后。
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


def _is_safe_symbol_case_replacement(wrong_symbol: str, correct_symbol: str) -> bool:  # 仅允许高置信度的类型名大小写修正，避免把变量误改成类名。
    if not wrong_symbol or not correct_symbol:  # 任一符号为空时都不应该执行替换。
        return False  # 明确拒绝空符号替换。
    if wrong_symbol.lower() != correct_symbol.lower():  # 当前保护逻辑只处理大小写差异，不处理不同单词之间的“近似修复”。
        return False  # 避免把缺失符号误修成另一个名字相近的类。
    if not wrong_symbol[:1].isupper() or not correct_symbol[:1].isupper():  # Java 类型名通常首字母大写，局部变量和字段大多不是。
        return False  # 像 `list->List` 这类局部变量误修在这里被拦住。
    return True  # 满足上述条件时，视为可接受的类型名大小写修正。


def _resolve_contextual_symbol_reference(test_file: str, symbol: str) -> Optional[Tuple[str, Optional[str]]]:  # 在少量高歧义符号上结合当前文件上下文做保守推断。
    try:  # 当前辅助逻辑只应增强成功率，不应因为读文件失败而中断主流程。
        with open(test_file, 'r', encoding='utf-8', errors='ignore') as f:  # 读取当前测试文件内容以便检查上下文导入线索。
            content = f.read()  # 读取完整文件文本供后续简单字符串判断使用。
    except Exception:  # 读取失败时直接跳过上下文推断。
        return None  # 返回空值交给其余推断逻辑继续处理。
    normalized_content = content.lower()  # 统一转小写，便于做大小写无关的上下文匹配。
    if symbol == 'Document' and any(marker in normalized_content for marker in ['org.w3c.dom', 'documentbuilder', 'namednodemap', 'inputsource', 'createelement', 'getdocumentelement']):  # DOM/XML 测试里缺少 `Document` 通常就是 `org.w3c.dom.Document`。
        return symbol, 'org.w3c.dom.Document'  # 返回基于 DOM 上下文的 `Document` 导入路径。
    if symbol == 'Node' and any(marker in normalized_content for marker in ['org.w3c.dom', 'documentbuilder', 'namednodemap', 'inputsource']):  # 只有当文件已经明显处于 DOM/XML 上下文中时才推断 `Node` 为 `org.w3c.dom.Node`。
        return symbol, 'org.w3c.dom.Node'  # 返回基于 DOM 上下文的安全导入路径。
    if symbol == 'JsonParser':  # `JsonParser` 同时可能来自 Gson 和 Jackson，需要结合当前文件上下文做更保守的区分。
        gson_markers = ('gsonutils', 'jsonelement', 'jsonobject', 'parsestring(', '.getasjsonobject(', '.getasjsonarray(')  # 这些标记更像 Gson 语义。
        jackson_markers = ('jsonfactory', 'jsontoken', 'deserializationcontext', 'serializerprovider')  # 这些标记更像 Jackson core parser。
        if any(marker in normalized_content for marker in gson_markers):  # 当前文件明显在用 Gson 风格 API 时才补 Gson 的 `JsonParser`。
            return symbol, 'com.google.gson.JsonParser'  # 返回 Gson 的安全导入路径。
        if any(marker in normalized_content for marker in jackson_markers):  # 当前文件明显在用 Jackson core 语义时才补 Jackson 的 `JsonParser`。
            return symbol, 'com.fasterxml.jackson.core.JsonParser'  # 返回 Jackson core 的安全导入路径。
    return None  # 其余符号暂不做额外的上下文推断。


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
    for import_line in import_lines:  # 逐个处理传入的 import 行。
        if import_line not in cleaned_imports:  # 去重以避免重复写入。
            cleaned_imports.append(import_line)  # 只保留第一次出现的 import。
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
    if '</dependencies>' in pom_content:  # 已存在 dependencies 区块时直接追加到其末尾。
        return pom_content.replace('</dependencies>', dependency_block + '\n    </dependencies>', 1)
    if '</project>' in pom_content:  # 没有 dependencies 区块时在 project 结束前创建一段新的 dependencies。
        insertion = '\n    <dependencies>\n' + dependency_block + '\n    </dependencies>\n'
        return pom_content.replace('</project>', insertion + '</project>', 1)
    return pom_content  # 非标准 pom 结构下直接返回原文，让上层显式报错。
