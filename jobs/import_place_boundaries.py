"""Import the official 121-place boundaries, preserving inner rings."""

import argparse

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.pipelines.official_files import import_place_boundaries
from jobs.runtime import REFERENCE_DATA, finish, run_cli


async def execute() -> int:
    engine = create_engine(Settings())
    try:
        result = await import_place_boundaries(
            engine, REFERENCE_DATA / "citydata_places_official.zip"
        )
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run_cli(execute)


if __name__ == "__main__":
    main()
