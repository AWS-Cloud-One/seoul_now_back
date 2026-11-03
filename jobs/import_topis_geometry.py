"""Import the official OA-15060 roads and OA-15061 ID mapping."""

import argparse

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.pipelines.official_files import import_topis_geometry
from jobs.runtime import REFERENCE_DATA, finish, run_cli


async def execute() -> int:
    engine = create_engine(Settings())
    try:
        result = await import_topis_geometry(
            engine,
            REFERENCE_DATA / "topis_vertices_2025.xlsx",
            REFERENCE_DATA / "topis_standard_mapping_20250306.xlsx",
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
