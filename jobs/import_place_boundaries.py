"""Import the official 121-place boundaries, preserving inner rings."""

import argparse
import asyncio
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.official_files import read_citydata_areas, read_shapes
from app.modules.data_pipeline.pipelines.official_files import import_place_boundaries
from jobs.runtime import REFERENCE_DATA, finish, run_cli


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--areas-file", type=Path, default=REFERENCE_DATA / "seoul_areas.xlsx")
    parser.add_argument(
        "--boundaries-file", type=Path, default=REFERENCE_DATA / "citydata_places_official.zip"
    )
    return parser.parse_args(argv)


async def execute(options=None) -> int:
    options = options or arguments([])
    engine = create_engine(Settings())
    try:
        started_at = datetime.now(UTC)
        targets, shapes = await asyncio.gather(
            asyncio.to_thread(read_citydata_areas, options.areas_file),
            asyncio.to_thread(read_shapes, options.boundaries_file),
        )
        result = await import_place_boundaries(
            engine,
            targets,
            shapes,
            started_at=started_at,
        )
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    run_cli(partial(execute, arguments()))


if __name__ == "__main__":
    main()
