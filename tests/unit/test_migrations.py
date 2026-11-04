"""Revision graph regressions do not need a running database."""

import warnings

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migration_revisions_are_unique_with_one_head():
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        revisions = list(scripts.walk_revisions())
        assert scripts.get_heads() == ["0012"]
        assert {item.revision for item in revisions} == {f"{n:04}" for n in range(1, 13)}
        assert len({item.revision for item in revisions}) == len(revisions)
        assert scripts.get_revision("0009").down_revision == "0008"
        assert scripts.get_revision("0010").down_revision == "0009"
        assert scripts.get_revision("0011").down_revision == "0010"
        assert scripts.get_revision("0012").down_revision == "0011"
