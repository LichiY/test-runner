"""Result collection and CSV output."""

import csv
import json
import logging
import os
from datetime import datetime
from typing import List

from .runner import TestRunResult

logger = logging.getLogger(__name__)


def write_results_csv(results: List[TestRunResult], output_path: str,
                      rerun_count: int) -> str:
    """Write rerun results to a CSV file.

    Args:
        results: List of TestRunResult objects.
        output_path: Path to the output CSV file.
        rerun_count: Number of reruns performed.

    Returns:
        Path to the written CSV file.
    """
    fieldnames = [
        'index',
        'repo_url',
        'project_name',
        'module',
        'test_class',
        'test_method',
        'full_test_name',
        'original_sha',
        'pr_link',
        'is_correct_label',
        'status',
        'rerun_results',
        'pass_count',
        'fail_count',
        'error_count',
        'total_runs',
        'verdict',
        'error_message',
    ]

    # Add individual run columns
    for i in range(rerun_count):
        fieldnames.append(f'run_{i + 1}')

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()

        for r in results:
            row = {
                'index': r.entry.index,
                'repo_url': r.entry.repo_url,
                'project_name': r.entry.project_name,
                'module': r.entry.module,
                'test_class': r.entry.test_class,
                'test_method': r.entry.test_method,
                'full_test_name': r.entry.full_test_name,
                'original_sha': r.entry.original_sha,
                'pr_link': r.entry.pr_link,
                'is_correct_label': r.entry.is_correct,
                'status': r.status,
                'rerun_results': json.dumps(r.results),
                'pass_count': r.pass_count,
                'fail_count': r.fail_count,
                'error_count': r.error_count,
                'total_runs': len(r.results),
                'verdict': _compute_verdict(r),
                'error_message': r.error_message[:500] if r.error_message else '',
            }

            # Add individual run results
            for i in range(rerun_count):
                if i < len(r.results):
                    row[f'run_{i + 1}'] = r.results[i]
                else:
                    row[f'run_{i + 1}'] = ''

            writer.writerow(row)

    logger.info(f"Results written to {output_path}")
    return output_path


def _compute_verdict(result: TestRunResult) -> str:
    """Compute a verdict for the test result.

    Verdicts:
    - STABLE_PASS: All runs passed - patch eliminates flakiness
    - STABLE_FAIL: All runs failed - patch does not fix the test
    - FLAKY: Mix of pass and fail - patch did not eliminate flakiness
    - BUILD_ERROR: Could not build/compile the project
    - SETUP_ERROR: Could not clone, find file, or apply patch
    - RUN_ERROR: All runs resulted in errors (not test failures)
    """
    if result.status != "completed":
        if result.status == "build_failed":
            return "BUILD_ERROR"
        return "SETUP_ERROR"

    if not result.results:
        return "SETUP_ERROR"

    # If all errors
    if result.error_count == len(result.results):
        return "RUN_ERROR"

    # Filter out error results for flakiness determination
    test_results = [r for r in result.results if r != "error"]

    if not test_results:
        return "RUN_ERROR"

    if all(r == "pass" for r in test_results):
        return "STABLE_PASS"
    if all(r == "fail" for r in test_results):
        return "STABLE_FAIL"
    return "FLAKY"


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
    print(f"Total entries:     {total}")
    print(f"Completed:         {len(completed)}")
    print(f"Setup errors:      {len(setup_errors)}")
    print("-" * 60)
    print("Verdicts:")
    for verdict, count in sorted(verdicts.items()):
        pct = count / total * 100
        print(f"  {verdict:<20s} {count:>4d}  ({pct:.1f}%)")
    print("=" * 60)
