"""Decide whether an image may be deployed against the current database revision.

Run by deploy.sh in CodeBuild (python3 standard library only) after the read-only
`w4_database.py revision` Job reported, from the image being deployed:
  db_revision      alembic_version in RDS
  image_head       newest migration shipped in the image
  image_revisions  every migration shipped in the image

The migration graph comes from the bundle's migrations/versions, which is always the
newest pipeline commit, so it also knows revisions newer than an old image.

release  the image must be at or ahead of the database. Every pending migration
         must declare backward_compatible = True before deploy.sh may migrate:
         the old Pods keep serving and must also remain usable for automatic rollback.
rollback the database is never changed. The image may be behind the database only
         if every revision it does not know declares `backward_compatible = True`
         (an image built before that revision keeps working with it).

Usage: db_compat.py --action release|rollback --migrations DIR --report JOB_LOG
Prints one JSON line; exit 0 = proceed, 1 = refuse.
"""

import argparse
import ast
import io
import json
import sys
import tokenize
from pathlib import Path

FIELDS = ("revision", "down_revision", "backward_compatible")


class Refused(Exception):
    pass


def read_migration(path):
    """Read literal module metadata without parsing/importing the migration body.

    CodeBuild's Python can be older than the app image's (3.11 vs 3.14). Tokenize
    to distinguish actual top-level assignments from SQL/docstrings/nested code,
    then parse only those assignments, whose values must be Python literals.
    """
    values = {}
    statement, indent = [], 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(Path(path).read_text()).readline):
            if token.type == tokenize.INDENT:
                indent += 1
            elif token.type == tokenize.DEDENT:
                indent -= 1
            elif token.type == tokenize.NEWLINE:
                if statement and statement[0] in [(tokenize.NAME, name) for name in FIELDS]:
                    node = ast.parse(tokenize.untokenize(statement), filename=str(path)).body[0]
                    if not (
                        isinstance(node, ast.Assign)
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                    ):
                        raise ValueError("metadata must be a literal assignment")
                    values[node.targets[0].id] = ast.literal_eval(node.value)
                statement = []
            elif indent == 0 and token.type not in (
                tokenize.NL,
                tokenize.COMMENT,
                tokenize.ENDMARKER,
            ):
                statement.append((token.type, token.string))
    except (SyntaxError, ValueError, tokenize.TokenError) as error:
        raise Refused(f"cannot read migration metadata in {Path(path).name}: {error}") from error
    return values


def load_graph(directory):
    """Return {revision: (down_revision, backward_compatible)} for a linear history."""
    graph = {}
    for path in sorted(Path(directory).glob("*.py")):
        values = read_migration(path)
        if "revision" not in values:
            continue
        down = values.get("down_revision")
        if not (down is None or isinstance(down, str)):
            raise Refused(f"merge/branch migration {values['revision']} is not supported")
        if values["revision"] in graph:
            raise Refused(f"duplicate revision {values['revision']}")
        graph[values["revision"]] = (down, values.get("backward_compatible") is True)
    if not graph:
        raise Refused("no migrations found in bundle")
    return graph


def lineage(graph, revision):
    """Revisions from `revision` down to the base, newest first."""
    chain = []
    while revision is not None:
        if revision not in graph or revision in chain:
            raise Refused(f"broken migration chain at {revision}")
        chain.append(revision)
        revision = graph[revision][0]
    return chain


def read_report(path):
    report = None
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("check") == "w4_revision":
            report = item
    if report is None or report.get("status") != "pass":
        raise Refused("revision report missing from the Job log")
    return report


def decide(action, graph, report):
    """Return a human-readable reason to proceed, or raise Refused."""
    if action not in ("release", "rollback"):
        raise Refused(f"unknown action {action}")
    db, head = report["db_revision"], report["image_head"]
    if db not in graph:
        raise Refused(f"database revision {db} is unknown to this pipeline")
    if head not in graph:
        raise Refused(f"image migration head {head} is unknown to this pipeline")
    image_chain = lineage(graph, head)
    if set(report["image_revisions"]) != set(image_chain):
        raise Refused("migrations in the image differ from this pipeline's history")
    if head == db:
        return f"image and database are both at {db}"
    if db in image_chain:
        if action == "release":
            pending = image_chain[: image_chain.index(db)]
            blocked = [rev for rev in pending if not graph[rev][1]]
            if blocked:
                raise Refused(
                    f"pending revisions {', '.join(reversed(blocked))} are not "
                    "backward_compatible; release would break running Pods or automatic "
                    "rollback. Use compatible migrations or a separately planned maintenance"
                )
            return f"release migrates the database {db} -> {head}"
        raise Refused(
            f"image needs migrations {db} -> {head}; rollback never migrates, "
            "use a normal deploy (release) for this image"
        )
    db_chain = lineage(graph, db)
    if head not in db_chain:
        raise Refused(f"image head {head} and database {db} are on different histories")
    newer = db_chain[: db_chain.index(head)]
    if action == "release":
        raise Refused(
            f"image ({head}) is older than the database ({db}); "
            "release would fail to migrate, use rollback"
        )
    blocked = [rev for rev in newer if not graph[rev][1]]
    if blocked:
        raise Refused(
            f"revisions {', '.join(reversed(blocked))} are not backward_compatible "
            f"with an image at {head}"
        )
    return f"database {db} stays; image at {head} is compatible with {', '.join(reversed(newer))}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--action", required=True, choices=("release", "rollback"))
    parser.add_argument("--migrations", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        report = read_report(args.report)
        reason = decide(args.action, load_graph(args.migrations), report)
    except Refused as error:
        print(json.dumps({"check": "db_compat", "status": "refused", "reason": str(error)}))
        return 1
    print(
        json.dumps(
            {
                "check": "db_compat",
                "status": "pass",
                "action": args.action,
                "db_revision": report["db_revision"],
                "image_head": report["image_head"],
                "reason": reason,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
