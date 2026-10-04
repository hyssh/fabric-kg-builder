"""Keep local corpora out of Git without hiding intentional test fixtures."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(),
    reason="Repository hygiene requires a Git checkout",
)


def test_local_data_ignored_and_reusable_fixtures_visible():
    ignored = {
        "sample_data/manual.pdf",
        "sample_data/nested/manual.docx",
        "sample_data/surface_questions.txt",
        ".core-compile-debug/run.log",
        ".crosswalk-validation/report.json",
        ".test-naming-review/output.json",
        ".env",
        ".env.local",
    }
    visible = {
        "sample_data/.gitkeep",
        "sample_data/Surface_Troubleshootings/README.md",
        ".env.example",
        "tests/fixtures/csv/sample.csv",
        "tests/fixtures/fabric_graph_schema/snapshot.json",
        "tests/fixtures/fabric_graph_schema/LICENSE",
        "examples/csv/sample.csv",
    }
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT, input="\n".join(sorted(ignored | visible)) + "\n",
        text=True, capture_output=True, check=True,
    )
    assert set(result.stdout.splitlines()) == ignored


def test_no_local_corpora_or_debug_outputs_tracked():
    result = subprocess.run(
        [
            "git", "ls-files", "-z", "--", "sample_data",
            ".core-compile-debug", ".crosswalk-validation", ":(glob).test-*/**",
        ],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    unexpected = [
        path for path in result.stdout.split("\0")
        if path and not (
            path.startswith("sample_data/")
            and Path(path).name in {".gitkeep", "README.md"}
        )
    ]
    assert not unexpected, f"Local source/debug data must not be tracked: {unexpected}"
