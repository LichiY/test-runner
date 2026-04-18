"""Result collection and CSV output."""

import csv  # 导入 CSV 读写工具。 
import json  # 导入 JSON 序列化工具。 
import logging  # 导入日志模块。 
import os  # 导入路径工具。 
import re  # 导入正则工具以解析 checkpoint 列。 
from typing import Dict, List, Tuple  # 导入类型注解工具。 

from .runner import TestRunResult  # 导入统一测试结果对象。 

logger = logging.getLogger(__name__)  # 创建当前模块的日志记录器。 
CSV_ERROR_MESSAGE_LIMIT = 4000  # 为结果 CSV 保留更完整的错误上下文，避免关键尾部再次被过度截断。 


def write_results_csv(results: List[TestRunResult], output_path: str, rerun_count: int) -> str:  # 将结果对象列表写回结果 CSV。 
    """Write rerun results to a CSV file.

    Args:
        results: List of TestRunResult objects.
        output_path: Path to the output CSV file.
        rerun_count: Number of reruns performed.

    Returns:
        Path to the written CSV file.
    """
    checkpoint_targets = _checkpoint_targets(rerun_count)  # 根据当前 rerun 次数生成需要落盘的关键阶段。 
    fieldnames = [  # 定义结果 CSV 的基础字段。 
        'request_key',  # 保存稳定请求键以支持 resume。 
        'index',  # 保存原始输入索引。 
        'workflow',  # 保存工作流名称。 
        'runner_backend',  # 保存执行后端名称。 
        'input_source',  # 保存输入来源。 
        'patch_mode',  # 保存补丁模式。 
        'repo_url',  # 保存仓库地址。 
        'repo_owner',  # 保存仓库 owner，便于后续重新定位工作区。 
        'project_name',  # 保存项目名。 
        'module',  # 保存模块名。 
        'test_class',  # 保存测试类名。 
        'test_method',  # 保存测试方法名。 
        'full_test_name',  # 保存完整测试名。 
        'original_sha',  # 保存原始提交号。 
        'fixed_sha',  # 保存修复提交号，便于后续 fixed_sha 上下文回查。 
        'pr_link',  # 保存 PR 链接。 
        'is_correct_label',  # 保存标签字段。 
        'source_file',  # 保存源文件路径。 
        'flaky_code',  # 保存原始 flaky 方法文本，便于后续诊断。 
        'fixed_code',  # 保留 ground-truth 测试代码，仅供离线分析，不作为运行时补丁。 
        'diff',  # 保存原始 diff 文本供离线分析。 
        'generated_patch',  # 保存真实被评估的 generated_patch，便于复现实验。 
        'original_rerun_consistency',  # 保存原始输入里的 rerun_consistency 字段。 
        'status',  # 保存流程状态。 
        'rerun_results',  # 仅保存 JSON 数组形式的逐轮结果，不再展开为 run_i 列。 
        'pass_count',  # 保存通过次数。 
        'fail_count',  # 保存失败次数。 
        'error_count',  # 保存错误次数。 
        'total_runs',  # 保存总执行次数。 
        'total_elapsed_seconds',  # 保存包含克隆和构建在内的总耗时。 
        'rerun_elapsed_seconds',  # 保存纯 rerun 阶段耗时。 
        'verdict',  # 保存综合 verdict。 
        'error_message',  # 保存截断后的错误信息。 
    ]  # 完成基础字段定义。 
    for checkpoint in checkpoint_targets:  # 逐个追加关键阶段的 verdict 与耗时字段。 
        fieldnames.extend([  # 为每个阶段追加 1 个结果列和 2 个耗时列。 
            f'checkpoint_{checkpoint}_verdict',  # 保存前 N 次 rerun 的阶段 verdict。 
            f'checkpoint_{checkpoint}_total_elapsed_seconds',  # 保存从请求开始到该阶段的总耗时。 
            f'checkpoint_{checkpoint}_rerun_elapsed_seconds',  # 保存从 rerun 开始到该阶段的纯 rerun 耗时。 
        ])  # 完成当前阶段列名追加。 

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)  # 确保输出目录存在。 

    with open(output_path, 'w', newline='', encoding='utf-8') as f:  # 以 UTF-8 覆盖写入结果文件。 
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')  # 创建字典写入器。 
        writer.writeheader()  # 先输出表头。 

        for r in results:  # 逐条写入结果对象。 
            row = {  # 构造当前结果行的基础字段。 
                'request_key': _entry_request_key(r.entry),  # 写入稳定请求键以支持 resume。 
                'index': getattr(r.entry, 'index', -1),  # 写入原始输入索引。 
                'workflow': _entry_workflow_name(r.entry),  # 写入工作流名称。 
                'runner_backend': _entry_runner_backend_name(r.entry),  # 写入执行后端名称。 
                'input_source': _entry_input_source(r.entry),  # 写入输入来源。 
                'patch_mode': _entry_patch_mode(r.entry),  # 写入补丁模式。 
                'repo_url': getattr(r.entry, 'repo_url', ''),  # 写入仓库地址。 
                'repo_owner': getattr(r.entry, 'repo_owner', ''),  # 写入仓库 owner。 
                'project_name': getattr(r.entry, 'project_name', ''),  # 写入项目名。 
                'module': getattr(r.entry, 'module', ''),  # 写入模块名。 
                'test_class': getattr(r.entry, 'test_class', ''),  # 写入测试类名。 
                'test_method': getattr(r.entry, 'test_method', ''),  # 写入测试方法名。 
                'full_test_name': getattr(r.entry, 'full_test_name', ''),  # 写入完整测试名。 
                'original_sha': getattr(r.entry, 'original_sha', ''),  # 写入原始提交号。 
                'fixed_sha': getattr(r.entry, 'fixed_sha', ''),  # 写入修复提交号。 
                'pr_link': getattr(r.entry, 'pr_link', ''),  # 写入 PR 链接。 
                'is_correct_label': getattr(r.entry, 'is_correct', ''),  # 写入标签字段。 
                'source_file': getattr(r.entry, 'source_file', ''),  # 写入源文件路径。 
                'flaky_code': getattr(r.entry, 'flaky_code', ''),  # 写入原始 flaky 方法文本。 
                'fixed_code': getattr(r.entry, 'fixed_code', ''),  # 写入 ground-truth 测试代码文本，供离线诊断。 
                'diff': getattr(r.entry, 'diff', ''),  # 写入原始 diff 文本。 
                'generated_patch': getattr(r.entry, 'generated_patch', ''),  # 写入真实被评估的生成补丁。 
                'original_rerun_consistency': getattr(r.entry, 'original_rerun_consistency', ''),  # 写入原始输入中的 rerun consistency 字段。 
                'status': r.status,  # 写入流程状态。 
                'rerun_results': json.dumps(r.results),  # 仅保留 JSON 数组形式的逐轮结果。 
                'pass_count': r.pass_count,  # 写入通过次数。 
                'fail_count': r.fail_count,  # 写入失败次数。 
                'error_count': r.error_count,  # 写入错误次数。 
                'total_runs': len(r.results),  # 写入总执行次数。 
                'total_elapsed_seconds': _format_seconds(r.total_elapsed_seconds),  # 写入包含构建等阶段的总耗时。 
                'rerun_elapsed_seconds': _format_seconds(r.rerun_elapsed_seconds),  # 写入纯 rerun 阶段耗时。 
                'verdict': _compute_verdict(r),  # 写入综合 verdict。 
                'error_message': _compact_csv_error_message(r.error_message),  # 将更长的错误尾部写入结果文件时优先保留关键诊断片段。 
            }  # 完成结果行基础字段构造。 
            for checkpoint in checkpoint_targets:  # 逐个补全关键阶段的 verdict 与耗时。 
                partial_results = r.results[:checkpoint]  # 截取当前关键阶段范围内的结果数组。 
                row[f'checkpoint_{checkpoint}_verdict'] = _compute_verdict_from_parts(r.status, partial_results) if checkpoint <= len(r.results) else ''  # 写入当前阶段的汇总 verdict。 
                row[f'checkpoint_{checkpoint}_total_elapsed_seconds'] = _format_optional_seconds(r.checkpoint_total_elapsed_seconds.get(checkpoint))  # 写入当前阶段的总耗时。 
                row[f'checkpoint_{checkpoint}_rerun_elapsed_seconds'] = _format_optional_seconds(r.checkpoint_rerun_elapsed_seconds.get(checkpoint))  # 写入当前阶段的纯 rerun 耗时。 
            writer.writerow(row)  # 将当前行写入 CSV。 

    logger.info(f"Results written to {output_path}")  # 记录结果文件落盘路径。 
    return output_path  # 返回写出的结果文件路径。 


def load_results_csv(output_path: str, entry_lookup: Dict[int, object]) -> List[TestRunResult]:  # 从已有结果 CSV 中恢复断点续跑所需的结果对象。
    if not os.path.isfile(output_path):  # 结果文件不存在时直接返回空列表。
        return []  # 无可恢复结果。

    restored_results = []  # 保存恢复出的历史结果对象。
    seen_keys = set()  # 避免同一请求键被重复恢复。
    with open(output_path, 'r', newline='', encoding='utf-8') as f:  # 读取已有结果 CSV。
        reader = csv.DictReader(f)  # 按字典形式解析每一行结果。
        for row in reader:  # 逐行恢复历史结果。
            request_key = row.get('request_key', '').strip()  # 优先读取新格式结果里的稳定请求键。 
            dedupe_key = request_key or row.get('index', '').strip()  # 对旧格式结果回退到 index 做去重。 
            if not dedupe_key or dedupe_key in seen_keys:  # 去重键缺失或已经处理过时跳过。 
                continue  # 跳过当前结果行。 
            entry = entry_lookup.get(request_key) if request_key else None  # 优先按请求键恢复当前条目。 
            if entry is None:  # 新格式恢复失败时再回退到旧 index 恢复。 
                try:  # 某些旧结果行的 index 可能为空或格式错误。 
                    index = int(row.get('index', '').strip())  # 读取旧格式结果里的索引字段。 
                except Exception:  # 无法解析索引时只能跳过当前行。 
                    continue  # 继续处理下一条历史结果。 
                entry = entry_lookup.get(index)  # 按旧索引查找当前待处理条目。 
                dedupe_key = request_key or str(index)  # 为旧格式结果生成去重键。 
            if entry is None:  # 当前运行不包含该条历史结果时无需恢复。 
                continue  # 跳过无关历史结果。 
            results_json = row.get('rerun_results', '').strip()  # 读取序列化后的重跑结果数组。
            try:  # 优先解析 rerun_results 列中的 JSON 数组。
                parsed_results = json.loads(results_json) if results_json else []  # 解析出历史重跑结果列表。
            except Exception:  # 历史结果格式异常时回退为空列表。
                parsed_results = []  # 保持恢复流程继续进行。
            checkpoint_total_elapsed_seconds, checkpoint_rerun_elapsed_seconds = _parse_checkpoint_elapsed(row)  # 解析阶段性耗时列。 
            restored_results.append(TestRunResult(  # 根据历史 CSV 重建最小结果对象。
                entry=entry,  # 复用当前运行中的条目对象。
                status=row.get('status', '').strip(),  # 恢复历史状态值。
                results=parsed_results,  # 恢复历史重跑结果数组。
                error_message=row.get('error_message', '').strip(),  # 恢复历史错误信息。
                build_output='',  # 历史 CSV 不保存完整构建输出，这里保留空串即可。
                total_elapsed_seconds=_parse_float(row.get('total_elapsed_seconds', '0')),  # 恢复总耗时字段。 
                rerun_elapsed_seconds=_parse_float(row.get('rerun_elapsed_seconds', '0')),  # 恢复纯 rerun 耗时字段。 
                checkpoint_total_elapsed_seconds=checkpoint_total_elapsed_seconds,  # 恢复阶段性总耗时字典。 
                checkpoint_rerun_elapsed_seconds=checkpoint_rerun_elapsed_seconds,  # 恢复阶段性纯 rerun 耗时字典。 
            ))  # 结束单条结果对象恢复。
            seen_keys.add(dedupe_key)  # 记录该请求键已被恢复。
    return restored_results  # 返回恢复出的历史结果列表。


def _compact_csv_error_message(message: str, limit: int = CSV_ERROR_MESSAGE_LIMIT) -> str:  # 在结果落盘阶段尽量保留完整诊断，同时避免单格内容无限膨胀。
    normalized_message = (message or '').strip()  # 先统一规整空值与首尾空白。
    if len(normalized_message) <= limit:  # 常见错误信息直接完整写出。
        return normalized_message  # 保留完整上下文，避免再次丢失关键尾部。
    diagnostic_header, message_body = _split_csv_diagnostic_header_and_body(normalized_message)  # 先尝试分离前置诊断头和真正的构建输出主体。
    if diagnostic_header:  # 只有存在前置诊断头时才走“头尾兼顾”的压缩策略。
        available_suffix = limit - len(diagnostic_header) - 2  # 预留两个换行字符后再计算还能容纳多少尾部信息。
        if available_suffix > 0:  # 当前限制足够同时容纳前缀与部分尾部。
            return f"{diagnostic_header}\n\n{message_body[-available_suffix:]}" if message_body else diagnostic_header[:limit]  # 优先保留前置诊断头和最终错误尾部。
    return normalized_message[-limit:]  # 其余场景默认保留尾部，因为错误根因通常出现在日志末尾。


def _split_csv_diagnostic_header_and_body(message: str) -> Tuple[str, str]:  # 将结果 CSV 里的前置诊断头和真实错误主体拆开。
    diagnostic_prefixes = ('Generated patch context history:', 'Reference patch context history:', 'Reference patch fallback history:', 'Automatic repair history:', 'Related test import repair:', 'Fixed-SHA helper backport:')  # 同时兼容当前前缀和旧 reference 前缀，避免历史 CSV 的诊断头在压缩时丢失。
    parts = [part.strip() for part in (message or '').split('\n\n')]  # 按空行切分多个前置诊断块与后续日志主体。
    diagnostic_parts = []  # 收集连续出现在最前面的诊断块。
    body_start = 0  # 记录真正日志主体从哪一块开始。
    for idx, part in enumerate(parts):  # 顺序扫描切分后的区块。
        if part.startswith(diagnostic_prefixes):  # 只要还是诊断头就继续保留。
            diagnostic_parts.append(part)  # 收集当前诊断头。
            body_start = idx + 1  # 更新主体起点。
            continue
        break  # 一旦遇到普通日志，后续都视为主体。
    if not diagnostic_parts:  # 没有诊断头时返回空前缀。
        return '', message
    message_body = '\n\n'.join(part for part in parts[body_start:] if part).strip()  # 重新拼接真正的错误主体。
    return '\n\n'.join(diagnostic_parts), message_body  # 返回诊断头和主体。


def _compute_verdict(result: TestRunResult) -> str:  # 基于完整结果对象计算综合 verdict。 
    return _compute_verdict_from_parts(result.status, result.results)  # 直接复用可重用的状态与结果判定逻辑。 


def _compute_verdict_from_parts(status: str, results: List[str]) -> str:  # 基于状态和结果数组计算综合 verdict。 
    if status != "completed":  # 非完成状态优先按构建或准备错误处理。 
        if status == "build_failed":  # 构建失败需要单独区分。 
            return "BUILD_ERROR"  # 返回构建错误 verdict。 
        return "SETUP_ERROR"  # 其余非完成状态统一视为准备阶段错误。 
    if not results:  # 完成状态但没有结果数组时说明执行信息缺失。 
        return "SETUP_ERROR"  # 返回准备阶段错误 verdict。 
    if all(result == "error" for result in results):  # 如果全部都是执行 error 则视为运行错误。 
        return "RUN_ERROR"  # 返回运行错误 verdict。 
    test_results = [result for result in results if result != "error"]  # 过滤掉 error 后再判断 flaky 与稳定性。 
    if not test_results:  # 过滤后为空说明没有有效测试结果。 
        return "RUN_ERROR"  # 返回运行错误 verdict。 
    if all(result == "pass" for result in test_results):  # 有效结果全部通过时说明阶段内稳定通过。 
        return "STABLE_PASS"  # 返回稳定通过 verdict。 
    if all(result == "fail" for result in test_results):  # 有效结果全部失败时说明阶段内稳定失败。 
        return "STABLE_FAIL"  # 返回稳定失败 verdict。 
    return "FLAKY"  # 既有 pass 又有 fail 时说明当前阶段仍然 flaky。 


def print_summary(results: List[TestRunResult]):
    """Print a summary of all results to the console."""
    total = len(results)
    if total == 0:
        print("No results to summarize.")
        return

    completed = [r for r in results if r.status == "completed"]
    setup_errors = [r for r in results if r.status != "completed"]

    verdicts = {}
    for r in results:
        v = _compute_verdict(r)
        verdicts[v] = verdicts.get(v, 0) + 1

    print("\n" + "=" * 60)
    print("RERUN RESULTS SUMMARY")
    print("=" * 60)
    print(f"Total entries:     {total}")  # 打印总条目数量。 
    print(f"Completed:         {len(completed)}")  # 打印成功完成的条目数量。 
    print(f"Setup errors:      {len(setup_errors)}")  # 打印非完成状态的条目数量。 
    print("-" * 60)  # 打印分隔线便于查看 verdict 分类。 
    print("Verdicts:")  # 打印 verdict 标题。 
    for verdict, count in sorted(verdicts.items()):  # 逐项输出各类 verdict 的数量统计。 
        pct = count / total * 100  # 计算当前 verdict 在总结果中的占比。 
        print(f"  {verdict:<20s} {count:>4d}  ({pct:.1f}%)")  # 按固定宽度打印 verdict 统计。 
    print("=" * 60)  # 打印摘要尾部分隔线。 


def _entry_request_key(entry: object) -> str:  # 从兼容条目对象中提取稳定请求键。 
    return getattr(entry, 'request_key', f"legacy:{getattr(entry, 'original_sha', '')}:{getattr(entry, 'unique_id', '')}")  # 优先使用对象自带请求键，否则构造旧版兼容键。 


def _entry_workflow_name(entry: object) -> str:  # 从兼容条目对象中提取工作流名称。 
    return getattr(entry, 'workflow_name', 'verify_patch')  # 缺失时回退到旧版补丁验证工作流。 


def _entry_runner_backend_name(entry: object) -> str:  # 从兼容条目对象中提取执行后端名称。 
    return getattr(entry, 'runner_backend_name', 'standard')  # 缺失时回退到旧版标准执行后端。 


def _entry_input_source(entry: object) -> str:  # 从兼容条目对象中提取输入来源。 
    return getattr(entry, 'input_source', 'patch_csv')  # 缺失时回退到旧版 patch CSV 来源。 


def _entry_patch_mode(entry: object) -> str:  # 从兼容条目对象中提取补丁模式。 
    return getattr(entry, 'patch_mode', 'with_patch')  # 缺失时回退到旧版带补丁模式。 


def _checkpoint_targets(rerun_count: int) -> List[int]:  # 根据总 rerun 次数生成结果 CSV 需要展示的关键阶段。 
    if rerun_count <= 0:  # 非正次数时不生成任何关键阶段。 
        return []  # 直接返回空列表。 
    if rerun_count <= 10:  # 小样本执行只保留最终阶段即可避免列数过多。 
        return [rerun_count]  # 返回最终阶段。 
    checkpoints = list(range(10, rerun_count + 1, 10))  # 默认每 10 次记录一个关键阶段。 
    if checkpoints[-1] != rerun_count:  # 如果最终次数不是 10 的倍数则补充最终阶段。 
        checkpoints.append(rerun_count)  # 追加最终阶段确保总结果可见。 
    return checkpoints  # 返回按顺序排列的关键阶段列表。 


def _format_seconds(value: float) -> str:  # 将秒数格式化为固定 3 位小数的字符串。 
    return f"{value:.3f}"  # 返回统一格式的秒数字符串。 


def _format_optional_seconds(value: float | None) -> str:  # 将可选秒数格式化为字符串或空串。 
    if value is None:  # 缺失的阶段耗时保持空串，便于区分未记录与真实 0 秒。 
        return ''  # 返回空串。 
    return _format_seconds(value)  # 其余情况走统一秒数字符串格式化。 


def _parse_float(raw_value: str) -> float:  # 将 CSV 中的字符串安全解析为浮点秒数。 
    try:  # 只有合法浮点数字符串才会被成功解析。 
        return float((raw_value or '').strip() or '0')  # 对空串回退为 0 再解析。 
    except Exception:  # 遇到历史脏数据时保持恢复流程继续。 
        return 0.0  # 返回默认秒数 0。 


def _parse_checkpoint_elapsed(row: Dict[str, str]) -> Tuple[Dict[int, float], Dict[int, float]]:  # 从结果 CSV 行中恢复关键阶段耗时字典。 
    checkpoint_total_elapsed_seconds: Dict[int, float] = {}  # 保存阶段性总耗时。 
    checkpoint_rerun_elapsed_seconds: Dict[int, float] = {}  # 保存阶段性纯 rerun 耗时。 
    for key, value in row.items():  # 遍历当前结果行中的所有列。 
        match = re.match(r'^checkpoint_(\d+)_(total_elapsed_seconds|rerun_elapsed_seconds)$', key or '')  # 仅匹配关键阶段耗时列。 
        if not match:  # 非关键阶段耗时列直接跳过。 
            continue  # 继续处理下一列。 
        checkpoint = int(match.group(1))  # 解析当前列对应的阶段次数。 
        metric = match.group(2)  # 读取当前列对应的耗时指标名称。 
        parsed_value = _parse_float(value or '')  # 将当前单元格解析为浮点秒数。 
        if metric == 'total_elapsed_seconds':  # 区分写入总耗时字典还是纯 rerun 耗时字典。 
            checkpoint_total_elapsed_seconds[checkpoint] = parsed_value  # 写入阶段性总耗时。 
        else:  # 其余匹配项必然是纯 rerun 耗时列。 
            checkpoint_rerun_elapsed_seconds[checkpoint] = parsed_value  # 写入阶段性纯 rerun 耗时。 
    return checkpoint_total_elapsed_seconds, checkpoint_rerun_elapsed_seconds  # 返回两个阶段性耗时字典。 
