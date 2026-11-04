"""Import a complete official OA-15060 road / OA-15061 mapping reference."""

import argparse
import asyncio
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.citydata import collect_road_reference_inputs
from app.modules.data_pipeline.collectors.official_files import read_topis_files
from app.modules.data_pipeline.pipelines.official_files import import_topis_geometry
from app.repositories.reference import ReferenceRepository
from jobs.runtime import REFERENCE_DATA, finish, run_cli


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vertices-file", type=Path, default=REFERENCE_DATA / "topis_vertices_2025.xlsx"
    )
    parser.add_argument(
        "--mapping-file", type=Path, default=REFERENCE_DATA / "topis_standard_mapping_20250306.xlsx"
    )
    parser.add_argument("--vertices-version")
    parser.add_argument("--mapping-version")
    parser.add_argument("--vertices-date", help="Confirmed source reference date (YYYY-MM-DD)")
    parser.add_argument("--mapping-date", help="Confirmed source reference date (YYYY-MM-DD)")
    parser.add_argument(
        "--partial",
        action="store_true",
        help="Validate a partial candidate without replacing the active reference",
    )
    parser.add_argument(
        "--allow-excluded-link",
        action="append",
        default=[],
        help="Reviewed sequence-gap link to exclude; must exactly match the detected list",
    )
    args = parser.parse_args(argv)
    args.expected_hashes = {}
    bundled_hashes = {
        "vertices": "c6aab7a85cf06ab9c9a68ca41e34ab4e89ecadcd09536e1ad545616283e90b5e",
        "mapping": "3ecabd782ac7ebec64fed32e4f61c792de70b38594c9e966736d283ceea3baad",
    }
    for name, filename, version in (
        ("vertices", "topis_vertices_2025.xlsx", "2025"),
        ("mapping", "topis_standard_mapping_20250306.xlsx", "2025-03-06"),
    ):
        if getattr(args, name + "_version") is None:
            if getattr(args, name + "_file").resolve() != (REFERENCE_DATA / filename).resolve():
                parser.error(f"--{name}-version is required for a custom file")
            setattr(args, name + "_version", version)
            args.expected_hashes[name] = bundled_hashes[name]
    if "mapping" in args.expected_hashes and args.mapping_date is None:
        args.mapping_date = "2025-03-06"
    return args


async def execute(options=None) -> int:
    options = options or arguments([])
    settings = Settings()
    engine = create_engine(settings)
    try:
        started_at = datetime.now(UTC)
        data = await asyncio.to_thread(
            read_topis_files, options.vertices_file, options.mapping_file
        )
        async with async_sessionmaker(engine)() as session:
            targets = await ReferenceRepository(session).citydata_areas()
        async with httpx.AsyncClient() as http:
            road_inputs = await collect_road_reference_inputs(
                http,
                settings.seoul_citydata_api_key.get_secret_value(),
                targets,
            )
        result = await import_topis_geometry(
            engine,
            data,
            road_inputs=road_inputs,
            started_at=started_at,
            vertices_version=options.vertices_version,
            mapping_version=options.mapping_version,
            vertices_date=options.vertices_date,
            mapping_date=options.mapping_date,
            complete=not options.partial,
            allowed_exclusions=options.allow_excluded_link,
            expected_hashes=options.expected_hashes,
        )
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    run_cli(partial(execute, arguments()))


if __name__ == "__main__":
    main()
