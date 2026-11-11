"""Deploy/rollback database compatibility gate (infra/cicd/db_compat.py), no database."""

import ast
import json
from pathlib import Path

import pytest

from infra.cicd.db_compat import Refused, decide, load_graph, main, read_migration

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def write_migrations(directory, *specs):
    """specs: (revision, down_revision, backward_compatible or None)."""
    for revision, down, compatible in specs:
        lines = [f"revision = {revision!r}", f"down_revision = {down!r}"]
        if compatible is not None:
            lines.append(f"backward_compatible = {compatible!r}")
        (directory / f"{revision}_m.py").write_text("\n".join(lines) + "\n")
    return load_graph(directory)


def report(db, head, revisions):
    return {"db_revision": db, "image_head": head, "image_revisions": list(revisions)}


@pytest.fixture
def graph(tmp_path):
    # 0003 is compatible with older images, 0004 is not.
    return write_migrations(
        tmp_path,
        ("0001", None, None),
        ("0002", "0001", None),
        ("0003", "0002", True),
        ("0004", "0003", False),
    )


def test_same_revision_is_allowed_for_both_actions(graph):
    for action in ("release", "rollback"):
        assert decide(action, graph, report("0003", "0003", ["0001", "0002", "0003"]))


def test_release_of_a_newer_image_migrates(graph):
    reason = decide("release", graph, report("0002", "0003", ["0001", "0002", "0003"]))
    assert "0002 -> 0003" in reason


@pytest.mark.parametrize("compatible", [False, None])
def test_release_refuses_incompatible_or_unmarked_pending_migrations(tmp_path, compatible):
    # A compatible last migration must not hide an incompatible intermediate one.
    graph = write_migrations(
        tmp_path,
        ("0001", None, None),
        ("0002", "0001", compatible),
        ("0003", "0002", True),
    )
    with pytest.raises(Refused, match="0002.*not backward_compatible"):
        decide("release", graph, report("0001", "0003", ["0001", "0002", "0003"]))


def test_release_without_pending_migrations_does_not_require_old_flags(graph):
    assert decide("release", graph, report("0004", "0004", list(graph)))


def test_rollback_never_migrates_a_newer_image(graph):
    with pytest.raises(Refused, match="use a normal deploy"):
        decide("rollback", graph, report("0002", "0003", ["0001", "0002", "0003"]))


def test_release_of_an_older_image_is_refused(graph):
    # The reported incident: migrating with an image that does not know the DB revision.
    with pytest.raises(Refused, match="use rollback"):
        decide("release", graph, report("0003", "0002", ["0001", "0002"]))


def test_rollback_keeps_the_database_over_compatible_revisions(graph):
    reason = decide("rollback", graph, report("0003", "0002", ["0001", "0002"]))
    assert "database 0003 stays" in reason


def test_rollback_over_an_incompatible_revision_is_refused(graph):
    with pytest.raises(Refused, match="0004 are not backward_compatible"):
        decide("rollback", graph, report("0004", "0002", ["0001", "0002"]))


def test_unknown_or_mismatched_history_is_refused(graph):
    with pytest.raises(Refused, match="database revision 0099"):
        decide("rollback", graph, report("0099", "0002", ["0001", "0002"]))
    with pytest.raises(Refused, match="image migration head 0099"):
        decide("rollback", graph, report("0002", "0099", ["0099"]))
    with pytest.raises(Refused, match="differ"):
        decide("rollback", graph, report("0002", "0002", ["0002"]))


def test_branched_history_is_refused(tmp_path):
    with pytest.raises(Refused, match="not supported"):
        write_migrations(tmp_path, ("0001", None, None), ("0002", ("0001", "0000"), None))


def test_real_history_allows_rollback_to_the_pre_state_sharing_image():
    graph = load_graph(MIGRATIONS)
    head = max(graph)
    older = [f"{n:04}" for n in range(1, 15)]
    # 0015/0016 only add tables: an image at 0014 runs on the current database.
    assert decide("rollback", graph, report(head, "0014", older))
    # 0014 is not marked, so going further back is refused.
    with pytest.raises(Refused, match="0014"):
        decide("rollback", graph, report(head, "0013", older[:-1]))


def run_cli(action, log):
    return main(["--action", action, "--migrations", str(MIGRATIONS), "--report", str(log)])


def test_cli_reads_the_job_log_and_reports_json(tmp_path, capsys):
    log = tmp_path / "job.log"
    revisions = [f"{n:04}" for n in range(1, 15)]
    line = {"check": "w4_revision", "status": "pass", **report("0016", "0014", revisions)}
    log.write_text("INFO alembic noise\n" + json.dumps(line) + "\n")
    assert run_cli("rollback", log) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    assert run_cli("release", log) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_cli_refuses_without_a_revision_report(tmp_path, capsys):
    log = tmp_path / "job.log"
    log.write_text('{"check": "w4_operation_failed"}\n')
    assert run_cli("rollback", log) == 1
    assert "missing" in json.loads(capsys.readouterr().out)["reason"]


def test_metadata_read_does_not_parse_newer_python_syntax_in_body(tmp_path, monkeypatch):
    # Exercise the older CodeBuild grammar even when pytest runs in the 3.14 app image.
    parse = ast.parse

    def parse_as_codebuild(source, **kwargs):
        return parse(source, feature_version=(3, 11), **kwargs)

    monkeypatch.setattr("infra.cicd.db_compat.ast.parse", parse_as_codebuild)
    migration = tmp_path / "new_syntax.py"
    migration.write_text(
        'revision = "0017"\ndown_revision = "0016"\nbackward_compatible = True\n'
        "def upgrade():\n"
        "    try:\n        pass\n"
        "    except OSError, ValueError:\n        pass\n"
    )
    assert read_migration(migration) == {
        "revision": "0017",
        "down_revision": "0016",
        "backward_compatible": True,
    }


def test_metadata_ignores_strings_and_nested_assignments(tmp_path):
    migration = tmp_path / "metadata.py"
    migration.write_text(
        "'''Example:\nrevision = \"wrong\"\nbackward_compatible = True\n'''\n"
        'revision = "0017"\ndown_revision = (\n    "0016"\n)\n'
        "backward_compatible = False  # deliberate refusal\n"
        "def upgrade():\n    backward_compatible = True\n"
        "SQL = '''\nrevision = \"also-wrong\"\nbackward_compatible = True\n'''\n"
    )
    assert read_migration(migration) == {
        "revision": "0017",
        "down_revision": "0016",
        "backward_compatible": False,
    }


def test_dynamic_metadata_is_rejected_without_executing_it(tmp_path):
    migration = tmp_path / "dynamic.py"
    marker = tmp_path / "must-not-exist"
    migration.write_text(f"revision = __import__('pathlib').Path({str(marker)!r}).touch()\n")
    with pytest.raises(Refused, match="cannot read migration metadata"):
        read_migration(migration)
    assert not marker.exists()
