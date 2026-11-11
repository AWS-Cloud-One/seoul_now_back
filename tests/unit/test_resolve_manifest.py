"""Rollback must identify one release's configuration, not just an image."""

import subprocess
import sys
from pathlib import Path

import pytest

from infra.cicd.resolve_manifest import manifest_commit

SCRIPT = Path(__file__).resolve().parents[2] / "infra/cicd/resolve_manifest.py"
A, B = "a" * 40, "b" * 40


def test_one_commit_with_other_tags_is_unambiguous():
    assert manifest_commit(["latest", f"git-{A}", "state-wip"]) == A


@pytest.mark.parametrize("tags", [[], ["None"], ["state-wip"], ["git-abc1234"]])
def test_non_pipeline_tags_cannot_choose_a_manifest(tags):
    with pytest.raises(ValueError, match="no git-"):
        manifest_commit(tags)


@pytest.mark.parametrize("commits", [(A, B), (B, A)])
def test_same_digest_with_multiple_commit_tags_is_refused_regardless_of_order(commits):
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="\t".join(f"git-{sha}" for sha in commits) + "\n",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert result.stdout == ""  # nothing may be used as MANIFEST_SHA
    assert "ambiguous" in result.stderr


def test_cli_prints_only_the_unique_commit():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=f"latest\tgit-{A}\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == A + "\n"
    assert result.stderr == ""
