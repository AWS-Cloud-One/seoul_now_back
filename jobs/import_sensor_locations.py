"""Validate and replace S-DoT installation reference independently of observation collection."""

import argparse
import asyncio
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.official_files import read_sensor_file
from app.modules.data_pipeline.pipelines.official_files import import_sensor_locations
from jobs.runtime import REFERENCE_DATA, finish, run_cli


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, default=REFERENCE_DATA / "sdot_locations_20251113.xlsx"
    )
    parser.add_argument("--reference-date", type=date.fromisoformat, help="Confirmed source date")
    return parser.parse_args(argv)


async def execute(options=None):
    options = options or arguments([])
    engine = create_engine(Settings())
    try:
        started_at = datetime.now(UTC)
        file = await asyncio.to_thread(read_sensor_file, options.file)
        return finish(
            await import_sensor_locations(
                engine,
                file,
                reference_date=options.reference_date.isoformat()
                if options.reference_date
                else None,
                started_at=started_at,
            )
        )
    finally:
        await engine.dispose()


def main():
    run_cli(partial(execute, arguments()))


if __name__ == "__main__":
    main()
