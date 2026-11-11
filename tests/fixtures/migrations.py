"""Migration facts for tests, derived from the scripts instead of hard-coded revisions."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]


def current_head():
    """The single head revision shipped in `migrations/` (e.g. "0016")."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def remove_versions_after(versions_dir, revision):
    """Delete every numbered version file newer than `revision` (e.g. "0007")."""
    for path in Path(versions_dir).glob("[0-9][0-9][0-9][0-9]_*.py"):
        if path.name[:4] > revision:
            path.unlink()
