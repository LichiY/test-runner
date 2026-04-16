"""Offline reference-patch analysis helpers.

This module is intentionally kept out of the runtime rerun path.
It exists only for learning from historical successful patch artifacts.
"""

import glob
import os
from typing import Dict, List, Tuple

from .patch import (REFERENCE_PROJECT_HELPER_MARKERS, ReferencePatchCandidate, _candidate_has_explicit_reference_context,
                    _candidate_uses_risky_json_helpers, _method_similarity, _normalize_code_for_match,
                    _normalize_reference_import_lines)

REFERENCE_PATCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'nondex_script', 'patch'))  # 保存离线参考补丁库根目录，真实运行时不依赖它。


def find_reference_patch_candidates(entry, reference_root: str = REFERENCE_PATCH_ROOT) -> List[ReferencePatchCandidate]:  # 仅供离线分析使用：从参考补丁库读取同仓库、同提交、同测试的成功补丁产物。
    project_name = getattr(entry, 'project_name', '').strip()  # 读取项目名以匹配参考补丁目录结构。
    original_sha = getattr(entry, 'original_sha', '').strip()  # 读取原始提交号以缩小搜索范围。
    test_class = getattr(entry, 'test_class', '').strip()  # 读取测试类名。
    test_method = getattr(entry, 'test_method', '').strip()  # 读取测试方法名。
    if not project_name or not original_sha or not test_class or not test_method:  # 任一关键键缺失时都无法安全定位参考补丁。
        return []  # 返回空列表让上层保持原有失败判定。
    test_key = f'{test_class}.{test_method}'  # 参考补丁目录以 `类名.方法名` 命名。
    patch_paths = _reference_patch_paths(reference_root=reference_root, project_name=project_name, original_sha=original_sha, test_key=test_key)  # 支持不同目录层级的 patch 家族，并在必要时回退到同项目同测试名的任意 SHA 候选。
    if not patch_paths:  # 当前案例说明本地参考补丁库里没有可用候选。
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


def find_reference_context_candidates(entry, reference_root: str = REFERENCE_PATCH_ROOT, similarity_threshold: float = 0.85) -> List[ReferencePatchCandidate]:  # 仅供离线分析使用：返回与当前 generated_patch 足够接近的成功参考补丁。
    if not getattr(entry, 'generated_patch', '').strip():  # 没有原始生成补丁时就没有“同一补丁”的上下文匹配可言。
        return []  # 直接返回空列表，避免误把别的补丁当成本案例上下文。
    matched_candidates = []  # 保存与当前 generated_patch 足够接近的参考候选。
    for candidate in find_reference_patch_candidates(entry, reference_root=reference_root):  # 先读取同案例的全部成功参考补丁。
        if _reference_candidate_patch_similarity(candidate, entry) < similarity_threshold:  # 只有当参考补丁与当前 generated_patch 足够接近时，才保留为离线可学习样本。
            continue  # 跳过语义已经明显不是同一个补丁的候选。
        matched_candidates.append(candidate)  # 记录当前可用的上下文候选。
    return matched_candidates  # 返回与当前被评估补丁相匹配的成功上下文候选。


def _reference_patch_paths(reference_root: str, project_name: str, original_sha: str, test_key: str) -> List[str]:  # 在离线参考库中同时支持不同家族的目录层级，并在必要时放宽到同项目同测试名的任意 SHA。
    if not reference_root or not os.path.isdir(reference_root):  # patch 根目录不存在时直接返回空列表。
        return []  # 让离线分析调用方拿到空结果即可。
    exact_pattern = os.path.join(reference_root, '**', project_name, original_sha, '**', test_key, '*.patch')  # 先精确匹配当前项目、当前 SHA 和当前测试名。
    exact_paths = sorted(set(glob.glob(exact_pattern, recursive=True)))  # 去重并稳定排序，兼容 `gpt/gpt1/all_rounds` 与 `magicoder/all_rounds` 等不同层级。
    if exact_paths:  # 只要 exact-sha 已命中，就优先使用这些来源。
        return exact_paths  # 返回 exact-sha 候选列表。
    fallback_pattern = os.path.join(reference_root, '**', project_name, '*', '**', test_key, '*.patch')  # exact-sha 缺失时再回退到同项目同测试名的任意 SHA 候选。
    return sorted(set(glob.glob(fallback_pattern, recursive=True)))  # 返回去重后的回退候选列表。


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


def _reference_candidate_priority(candidate: ReferencePatchCandidate) -> Tuple[int, int, int, int, int]:  # 为离线参考补丁候选生成越小越优的排序键。
    source_rank = _reference_candidate_source_rank(candidate)  # 离线分析里仍优先 GoodPatches。
    explicit_context_rank = 0 if _candidate_has_explicit_reference_context(candidate) else 1  # 对当前语义来说，带显式 import/pom 的成功补丁更有价值。
    risky_code_rank = 1 if _candidate_uses_risky_json_helpers(candidate.test_code) else 0  # 明显依赖幻觉 helper 的候选继续后移。
    return (source_rank, explicit_context_rank, risky_code_rank, len(candidate.test_code), 0)  # 最后用代码长度打破平局，优先尝试更集中的候选。


def _reference_candidate_sort_key(candidate: ReferencePatchCandidate, entry) -> Tuple[int, float, int, int, int, int, int]:  # 将“来源可信度、结构保持程度、API 兼容性”合并成离线分析用排序键。
    normalized_source_path = (candidate.source_path or '').replace(os.sep, '/')  # 统一路径分隔符，便于判断当前候选是否来自 exact-sha。
    exact_sha_rank = 0 if f'/{getattr(entry, "original_sha", "").strip()}/' in normalized_source_path else 1  # 只优先当前提交号下的成功补丁产物。
    compatibility_rank = _reference_candidate_compatibility_rank(candidate)  # 把明显依赖幻觉 helper 的候选放到后面。
    source_rank = _reference_candidate_source_rank(candidate)  # GoodPatches 先于 all_rounds。
    structure_distance = round(1.0 - _reference_candidate_patch_similarity(candidate, entry), 4)  # 离线分析里优先看它和当前 generated_patch 是否像同一类修复。
    priority_tail = _reference_candidate_priority(candidate)  # 最后复用已有的 pom/第三方依赖保守排序。
    return (exact_sha_rank, compatibility_rank, source_rank, structure_distance, *priority_tail)  # Python 会按元组逐项升序排序，越小越优。


def _reference_candidate_compatibility_rank(candidate: ReferencePatchCandidate) -> int:  # 按“在原始 SHA 上是否容易直接编过”给候选分级。
    signals = '\n'.join(filter(None, [candidate.test_code, '\n'.join(candidate.imports), candidate.pom_snippet]))  # 合并代码、import 和 pom 片段做整体判断。
    if any(marker in signals for marker in REFERENCE_PROJECT_HELPER_MARKERS):  # 这些 helper 在失败样本里已经确认经常不存在于原始项目。
        return 2  # 明显依赖缺失 helper 的候选排在最后。
    if _candidate_uses_risky_json_helpers(candidate.test_code):  # 其余只在名字层面看起来像幻觉 helper 的候选次之。
        return 1  # 不是直接排除，但要放在标准 API 候选后面。
    return 0  # JSONAssert、JsonPath、ObjectMapper 这类稳定库 API 不再被误判成“不兼容”。


def _reference_candidate_source_rank(candidate: ReferencePatchCandidate) -> int:  # 为不同来源类型建立一个稳定的次级优先级。
    source_path = candidate.source_path or ''  # 统一处理空来源路径。
    if 'GoodPatches' in source_path:  # 显式标记为 GoodPatches 的参考补丁通常已经过额外筛选。
        return 0
    if 'all_rounds' in source_path:  # 其他 patch 生成轮次仍可作为上下文线索，但优先级低于 GoodPatches。
        return 1
    return 2  # 其余来源最后再试。


def _reference_candidate_patch_similarity(candidate: ReferencePatchCandidate, entry) -> float:  # 计算成功参考补丁与当前被评估 generated_patch 的结构相似度。
    generated_patch = getattr(entry, 'generated_patch', '')  # 离线分析也只围绕当前真正被评估的补丁比较结构相似度。
    if not generated_patch:  # 缺少生成补丁时无法继续比较。
        return 0.0
    return _method_similarity(candidate.test_code, generated_patch)  # 返回当前参考补丁和被评估补丁之间的相似度。
