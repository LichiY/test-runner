"""CLI entry point for the Flaky Test Rerun Tool."""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from typing import List, Optional

from .data import TestEntry, load_csv
from .docker import should_use_docker
from .patch import apply_patch, find_test_file, fix_missing_imports, restore_backup
from .repo import clone_repo, reset_repo
from .results import print_summary, write_results_csv
from .runner import (RerunMode, TestRunResult, build_project, run_test)

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False, log_file: Optional[str] = None):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def process_entry(entry: TestEntry, workspace_dir: str,
                  rerun_count: int, mode: RerunMode,
                  docker_mode: str,
                  build_timeout: int, test_timeout: int,
                  build_retries: int) -> TestRunResult:
    """Process a single test entry: clone, patch, build, rerun.

    Args:
        entry: The test entry to process.
        workspace_dir: Directory to clone repos into.
        rerun_count: Number of test reruns.
        mode: Rerun mode (isolated or same_jvm).
        docker_mode: 'auto', 'always', or 'never'.
        build_timeout: Build timeout in seconds.
        test_timeout: Per-test-run timeout in seconds.
        build_retries: Max build retry attempts.

    Returns:
        TestRunResult with the outcome.
    """
    project_id = f"{entry.repo_owner}_{entry.project_name}" if entry.repo_owner else entry.project_name
    repo_dir = os.path.join(workspace_dir, project_id)

    logger.info(f"\n{'='*60}")
    logger.info(f"Processing: {entry.full_test_name}")
    logger.info(f"  Project: {entry.project_name} | Module: {entry.module}")
    logger.info(f"  SHA: {entry.original_sha[:8]}")

    # Step 1: Clone and checkout
    logger.info("Step 1: Cloning repository...")
    if not clone_repo(entry.repo_url, repo_dir, entry.original_sha):
        return TestRunResult(
            entry=entry, status="clone_failed",
            error_message=f"Failed to clone {entry.repo_url} at {entry.original_sha}"
        )

    # Step 2: Find test file
    logger.info("Step 2: Finding test file...")
    test_file = find_test_file(repo_dir, entry)
    if test_file is None:
        return TestRunResult(
            entry=entry, status="file_not_found",
            error_message=f"Test file not found for {entry.test_class}"
        )

    # Step 3: Apply patch
    logger.info("Step 3: Applying patch...")
    patch_ok, patch_msg = apply_patch(test_file, entry)
    if not patch_ok:
        reset_repo(repo_dir)
        return TestRunResult(
            entry=entry, status="patch_failed",
            error_message=f"Patch failed: {patch_msg}"
        )

    # Determine whether to use Docker
    if docker_mode == 'always':
        use_docker = True
    elif docker_mode == 'never':
        use_docker = False
    else:  # auto
        use_docker = should_use_docker(repo_dir, entry.module)  # 自动模式下按模块配置判断是否需要 Docker。

    logger.info(f"  Execution: {'Docker' if use_docker else 'Local'}")

    # Step 4: Build
    logger.info("Step 4: Building project...")
    build_ok, build_output = build_project(
        repo_dir, entry, use_docker=use_docker,
        timeout=build_timeout, max_retries=build_retries
    )
    if not build_ok:
        logger.warning("Initial build failed, attempting missing-import repair...")
        repaired, repair_msg = fix_missing_imports(test_file, build_output)  # 根据编译错误尝试补全高置信度缺失 import。
        if repaired:
            logger.info(f"Import repair applied: {repair_msg}")  # 记录本次自动修复详情。
            build_ok, build_output = build_project(  # 修复后立即重新构建一次以验证补丁是否可编译。
                repo_dir, entry, use_docker=use_docker,
                timeout=build_timeout, max_retries=build_retries
            )
        else:
            logger.info(f"Import repair skipped: {repair_msg}")  # 记录为什么没有执行自动修复。
    if not build_ok:
        logger.error(f"Build failed: {build_output[-500:]}")
        reset_repo(repo_dir)
        return TestRunResult(
            entry=entry, status="build_failed",
            error_message=build_output[-1000:],
            build_output=build_output
        )

    # Step 5: Run tests
    logger.info(f"Step 5: Running test {rerun_count} times (mode={mode.value})...")
    results = run_test(
        repo_dir, entry, rerun_count, mode=mode,
        use_docker=use_docker, timeout=test_timeout
    )

    # Reset repo for next entry
    reset_repo(repo_dir)

    logger.info(f"Results: {results}")

    return TestRunResult(
        entry=entry, status="completed", results=results
    )


def main():
    parser = argparse.ArgumentParser(
        description="Flaky Test Rerun Tool - Apply patches to flaky tests and verify via rerun",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run first 5 entries with 10 reruns (Docker, auto JDK version)
  python -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 5 --rerun 10

  # Run without Docker (local JDK)
  python -m rerun_tool --csv data.csv --limit 5 --rerun 10 --no-docker

  # Run specific rows
  python -m rerun_tool --csv data.csv --rows 0,1,2 --rerun 5

  # NIO type flaky tests (same JVM mode)
  python -m rerun_tool --csv data.csv --limit 3 --rerun 10 --mode same_jvm

  # Filter by project
  python -m rerun_tool --csv data.csv --project commons-lang --rerun 10
        """
    )

    # Input/Output
    parser.add_argument('--csv', required=True,
                        help='Path to the input CSV file with flaky test data')
    parser.add_argument('--output', '-o', default=None,
                        help='Path to output CSV file (default: results/rerun_results_<timestamp>.csv)')
    parser.add_argument('--workspace', '-w', default='workspace',
                        help='Directory to clone repositories into (default: workspace)')

    # Data selection
    parser.add_argument('--rows', type=str, default=None,
                        help='Comma-separated row indices (0-based, e.g., "0,1,5,10")')
    parser.add_argument('--limit', type=int, default=None,
                        help='Maximum number of entries to process')
    parser.add_argument('--project', type=str, default=None,
                        help='Filter by project name (substring match)')

    # Rerun configuration
    parser.add_argument('--rerun', '-n', type=int, default=10,
                        help='Number of reruns per test (default: 10)')
    parser.add_argument('--mode', type=str, choices=['isolated', 'same_jvm'],
                        default='isolated',
                        help='isolated: separate JVM per run (default); same_jvm: forkCount=0 for NIO type')

    # Docker
    parser.add_argument('--docker', dest='docker_mode', type=str,
                        choices=['auto', 'always', 'never'], default='auto',
                        help='Docker mode: auto (use Docker only when local JDK incompatible, default), '
                             'always (force Docker), never (always local)')

    # Timeouts
    parser.add_argument('--build-timeout', type=int, default=600,
                        help='Build timeout in seconds (default: 600)')
    parser.add_argument('--test-timeout', type=int, default=300,
                        help='Per-test-run timeout in seconds (default: 300)')
    parser.add_argument('--build-retries', type=int, default=2,
                        help='Max build retry attempts (default: 2)')

    # Logging
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose/debug logging')
    parser.add_argument('--log-file', type=str, default=None,
                        help='Path to log file')

    args = parser.parse_args()

    # Setup logging
    setup_logging(verbose=args.verbose, log_file=args.log_file)

    # Parse row selection
    row_indices = None
    if args.rows:
        row_indices = [int(x.strip()) for x in args.rows.split(',')]

    # Set default output path
    if args.output is None:
        os.makedirs('results', exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.output = f'results/rerun_results_{timestamp}.csv'

    # Load data
    logger.info(f"Loading data from {args.csv}...")
    load_limit = None if args.project else args.limit
    entries = load_csv(args.csv, rows=row_indices, limit=load_limit)

    if args.project:
        entries = [e for e in entries if args.project.lower() in e.project_name.lower()]
        if args.limit:
            entries = entries[:args.limit]

    if not entries:
        logger.error("No entries to process after filtering.")
        sys.exit(1)

    mode = RerunMode.ISOLATED if args.mode == 'isolated' else RerunMode.SAME_JVM

    logger.info(f"Loaded {len(entries)} entries to process")
    logger.info(f"Rerun count: {args.rerun} | Mode: {args.mode} | Docker: {args.docker_mode}")
    logger.info(f"Workspace: {args.workspace} | Output: {args.output}")

    # Process entries
    workspace = os.path.abspath(args.workspace)
    os.makedirs(workspace, exist_ok=True)

    all_results: List[TestRunResult] = []
    start_time = time.time()

    for i, entry in enumerate(entries):
        logger.info(f"\n[{i + 1}/{len(entries)}] Processing entry {entry.index}")

        result = process_entry(
            entry=entry,
            workspace_dir=workspace,
            rerun_count=args.rerun,
            mode=mode,
            docker_mode=args.docker_mode,
            build_timeout=args.build_timeout,
            test_timeout=args.test_timeout,
            build_retries=args.build_retries,
        )
        all_results.append(result)

        # Write intermediate results (crash safety)
        write_results_csv(all_results, args.output, args.rerun)

    elapsed = time.time() - start_time

    # Final output
    write_results_csv(all_results, args.output, args.rerun)
    print_summary(all_results)

    logger.info(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f}min)")
    logger.info(f"Results saved to: {args.output}")


if __name__ == '__main__':
    main()
