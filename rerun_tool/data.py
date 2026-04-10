"""CSV data loading and filtering for flaky test entries."""

import csv
import io
import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TestEntry:
    """A single flaky test entry from the CSV data."""
    index: int  # row index in original CSV
    repo_url: str
    repo_owner: str
    project_name: str
    original_sha: str
    fixed_sha: str
    module: str
    full_test_name: str
    pr_link: str
    flaky_code: str
    fixed_code: str
    diff: str
    generated_patch: str
    is_correct: str
    source_file: str

    @property
    def test_class(self) -> str:
        """Extract fully qualified class name from full_test_name.
        Handles two formats:
        - package.ClassName.methodName.methodName (method duplicated)
        - package.ClassName.methodName (method not duplicated)
        """
        parts = self.full_test_name.rsplit('.', 2)
        if len(parts) >= 3 and parts[1] == parts[2]:
            # Method name is duplicated: class is parts[0]
            return parts[0]
        # Method not duplicated or only 2 parts: class is everything before last part
        return self.full_test_name.rsplit('.', 1)[0]

    @property
    def test_method(self) -> str:
        """Extract test method name from full_test_name."""
        parts = self.full_test_name.rsplit('.', 2)
        if len(parts) >= 3 and parts[1] == parts[2]:
            return parts[1]
        return self.full_test_name.rsplit('.', 1)[-1]

    @property
    def simple_class_name(self) -> str:
        """Get simple class name (without package)."""
        return self.test_class.rsplit('.', 1)[-1]

    @property
    def class_path(self) -> str:
        """Convert class name to file path: com.foo.Bar -> com/foo/Bar.java"""
        return self.test_class.replace('.', '/') + '.java'

    @property
    def unique_id(self) -> str:
        """Unique identifier for this test entry."""
        return f"{self.project_name}_{self.simple_class_name}_{self.test_method}_{self.index}"


def load_csv(csv_path: str, rows: Optional[List[int]] = None,
             limit: Optional[int] = None) -> List[TestEntry]:
    """Load test entries from CSV file.

    Args:
        csv_path: Path to the CSV file.
        rows: Optional list of specific row indices (0-based, excluding header) to load.
        limit: Optional maximum number of entries to load.

    Returns:
        List of TestEntry objects.
    """
    entries = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            # Skip rows without repo_url
            if not row.get('repo_url', '').strip():
                continue

            # Skip rows without generated_patch
            if not row.get('generated_patch', '').strip():
                continue

            # Filter by specific row indices if provided
            if rows is not None and i not in rows:
                continue

            entry = TestEntry(
                index=i,
                repo_url=row['repo_url'].strip(),
                repo_owner=row.get('repo_owner', '').strip(),
                project_name=row.get('project_name', '').strip(),
                original_sha=row.get('original_sha', '').strip(),
                fixed_sha=row.get('fixed_sha', '').strip(),
                module=row.get('module', '').strip(),
                full_test_name=row.get('full_test_name', '').strip(),
                pr_link=row.get('pr_link', '').strip(),
                flaky_code=row.get('flaky_code', '').strip(),
                fixed_code=row.get('fixed_code', '').strip(),
                diff=row.get('diff', '').strip(),
                generated_patch=row.get('generated_patch', '').strip(),
                is_correct=row.get('isCorrect', '').strip(),
                source_file=row.get('source_file', '').strip(),
            )
            entries.append(entry)

            if limit is not None and len(entries) >= limit:
                break

    return entries
