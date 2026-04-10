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
from typing import List, Optional, Tuple

from .data import TestEntry

logger = logging.getLogger(__name__)

JAVA_IMPORT_CANDIDATES = {  # 为常见标准库与集合类型提供保守的 import 推断表。
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
    'Queue': 'java.util.Queue',  # Queue 属于 java.util。
    'Set': 'java.util.Set',  # Set 属于 java.util。
    'TreeMap': 'java.util.TreeMap',  # TreeMap 属于 java.util。
    'TreeSet': 'java.util.TreeSet',  # TreeSet 属于 java.util。
}


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


def apply_patch(test_file: str, entry: TestEntry) -> Tuple[bool, str]:
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

        # Create backup
        backup_path = test_file + '.bak'
        shutil.copy2(test_file, backup_path)

        method_name = entry.test_method
        lines = original_content.splitlines()

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
            return False, f"Target method mismatch (similarity={reference_similarity:.2f})"  # 主动失败以避免误贴补丁。

        # Step 3: Detect the file's indentation for this method
        method_line = lines[method_start]
        file_indent = len(method_line) - len(method_line.lstrip())
        file_indent_str = method_line[:file_indent]

        # Step 4: Detect the patch's base indentation
        patch_text = entry.generated_patch.strip()
        patch_lines = patch_text.splitlines()

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
        ok, msg = _verify_patch_applied(test_file, method_name, entry.generated_patch)  # 再次验证贴入后的方法结构和内容。
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


def fix_missing_imports(test_file: str, build_output: str) -> Tuple[bool, str]:  # 根据编译错误信息为 Java 测试文件自动补充缺失 import。
    try:
        with open(test_file, 'r', encoding='utf-8') as f:  # 读取当前测试文件内容。
            original_content = f.read()  # 保留原内容以便判断是否真正发生修改。
    except Exception as e:  # 读取失败时直接返回错误。
        return False, f"Failed to read test file for import fix: {e}"  # 返回读取失败原因。

    missing_symbols = _extract_missing_symbols(build_output, test_file)  # 只提取当前目标测试文件对应的缺失符号名。
    if not missing_symbols:  # 没有检测到可修复的缺失符号时直接结束。
        return False, "No fixable missing symbols found in build output"  # 返回没有可修复符号的信息。

    imports_to_add = []  # 收集需要新增的 import 语句。
    for symbol in missing_symbols:  # 逐个处理缺失的符号名。
        import_path = JAVA_IMPORT_CANDIDATES.get(symbol)  # 先尝试使用内置的高置信度标准库映射。
        if not import_path:  # 标准库映射无法覆盖时再回退到仓库内源码搜索。
            import_path = _find_project_import_path(test_file, symbol)  # 在当前仓库中查找唯一匹配的源码类并推断 import。
        if not import_path:  # 未知符号不做不安全猜测。
            continue  # 跳过没有映射关系的符号。
        import_line = f'import {import_path};'  # 生成标准 Java import 行。
        if import_line in original_content:  # 已存在对应 import 时无需重复添加。
            continue  # 跳过已导入的符号。
        imports_to_add.append(import_line)  # 将待新增 import 收集起来。

    if not imports_to_add:  # 没有任何可新增 import 时返回失败。
        return False, "Missing symbols detected but no safe import fixes available"  # 明确告知当前错误无法安全自动修复。

    updated_content = _insert_import_lines(original_content, imports_to_add)  # 将缺失 import 按 Java 规范插入文件头部。
    if updated_content == original_content:  # 插入结果未发生变化则无需落盘。
        return False, "Import fix produced no file changes"  # 返回无变化说明。

    with open(test_file, 'w', encoding='utf-8') as f:  # 将修复后的内容写回测试文件。
        f.write(updated_content)  # 覆盖写入新文件内容。
    return True, f"Added imports: {', '.join(imports_to_add)}"  # 返回成功信息和新增 import 列表。


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_method_declaration(lines: List[str], method_name: str,
                             reference_code: str = '') -> Optional[int]:  # 用参考代码在多候选场景中选择最匹配的方法。
    """Find the line index of the method declaration.

    Handles various Java method declaration styles:
    - public void testFoo() {
    - public void testFoo() throws Exception {
    - void testFoo()
    -   throws Exception {    (multi-line signature)
    """
    # Pattern to match the method name in a declaration context
    # Must be preceded by a return type or visibility modifier
    sig_pattern = re.compile(
        rf'\b{re.escape(method_name)}\s*\('
    )

    candidates = []
    for i, line in enumerate(lines):
        if sig_pattern.search(line):
            # Verify this is a method declaration, not a method call
            stripped = line.strip()

            # Skip lines that are clearly method calls (inside method bodies)
            # A method declaration has a return type before the method name
            if _is_method_declaration(lines, i, method_name):
                candidates.append(i)

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
        if 'cannot find symbol' in line:  # 进入新的编译错误块时先判断是否属于目标文件。
            current_error_matches_target = (target_name in line) if target_name else True  # 只有目标文件对应的错误块才参与后续符号提取。
        match = re.search(r'symbol:\s+(?:variable|class)\s+([A-Za-z_][A-Za-z0-9_]*)', line)  # 匹配缺失变量或类名。
        if not match:  # 当前行不包含缺失符号信息时继续。
            continue  # 跳到下一行。
        if not current_error_matches_target:  # 非目标文件的缺失符号不能拿来修当前测试文件。
            continue  # 跳过无关错误块中的符号。
        symbol = match.group(1)  # 提取缺失的简单类名或变量名。
        if symbol not in symbols:  # 保持顺序的同时避免重复。
            symbols.append(symbol)  # 记录新的缺失符号。
    return symbols  # 返回收集到的缺失符号列表。


def _find_project_import_path(test_file: str, symbol: str) -> Optional[str]:  # 在当前仓库源码中搜索唯一类定义并推断 import 路径。
    repo_root = _find_repo_root(test_file)  # 先定位当前测试文件所属仓库根目录。
    if repo_root is None:  # 找不到仓库根目录时无法继续搜索源码。
        return None  # 返回空值交由上层跳过该符号。

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
