"""Repository cloning and management."""

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def clone_repo(repo_url: str, target_dir: str, sha: str,
               timeout: int = 600) -> bool:
    """Clone a repository and checkout to the specified SHA.

    If the repo is already cloned, just fetch and checkout.

    Args:
        repo_url: Git repository URL.
        target_dir: Local directory to clone into.
        sha: Commit SHA to checkout.
        timeout: Timeout in seconds for git operations.

    Returns:
        True if successful, False otherwise.
    """
    try:
        if os.path.exists(os.path.join(target_dir, '.git')):
            logger.info(f"Repo already exists at {target_dir}, checking out {sha[:8]}")
            # Reset any local changes
            _run_git(target_dir, ['git', 'checkout', '--', '.'], timeout=60)
            _run_git(target_dir, ['git', 'clean', '-fd'], timeout=60)
            # Try to checkout the SHA directly
            result = _run_git(target_dir, ['git', 'checkout', sha], timeout=60)
            if result.returncode != 0:
                # SHA not available, fetch and retry
                logger.info("SHA not available locally, fetching...")
                _run_git(target_dir, ['git', 'fetch', 'origin'], timeout=timeout)
                result = _run_git(target_dir, ['git', 'checkout', sha], timeout=60)
                if result.returncode != 0:
                    logger.error(f"Failed to checkout {sha}: {result.stderr}")
                    return False
        else:
            logger.info(f"Cloning {repo_url} to {target_dir}")
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            result = subprocess.run(
                ['git', 'clone', repo_url, target_dir],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0:
                logger.error(f"Clone failed: {result.stderr}")
                return False

            logger.info(f"Checking out {sha[:8]}")
            result = _run_git(target_dir, ['git', 'checkout', sha], timeout=60)
            if result.returncode != 0:
                logger.error(f"Checkout failed: {result.stderr}")
                return False

        return True
    except subprocess.TimeoutExpired:
        logger.error(f"Git operation timed out for {repo_url}")
        return False
    except Exception as e:
        logger.error(f"Git operation failed: {e}")
        return False


def reset_repo(repo_dir: str) -> bool:
    """Reset the repository to a clean state (discard all local changes).

    Args:
        repo_dir: Path to the repository.

    Returns:
        True if successful.
    """
    try:
        _run_git(repo_dir, ['git', 'checkout', '--', '.'], timeout=60)
        _run_git(repo_dir, ['git', 'clean', '-fd'], timeout=60)
        return True
    except Exception as e:
        logger.error(f"Reset failed: {e}")
        return False


def _run_git(repo_dir: str, cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a git command in the given directory."""
    return subprocess.run(
        cmd, cwd=repo_dir, capture_output=True, text=True, timeout=timeout
    )
