"""CLI for bounded supplemental collection; processing lives in the pipeline."""

import argparse

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.pipelines.supplemental import SOURCES, run_supplemental
from jobs.runtime import RAW_DATA, REFERENCE_DATA, finish, run_cli


async def execute(target, reuse_probe=False) -> int:
    settings = Settings()
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as http:
            result = await run_supplemental(
                engine,
                http,
                settings,
                target,
                sensor_path=REFERENCE_DATA / "sdot_locations_20251113.xlsx",
                probe_path=RAW_DATA / "its_probe.json" if reuse_probe else None,
            )
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=SOURCES)
    parser.add_argument(
        "--reuse-probe",
        action="store_true",
        help="Use already fetched ITS response without spending quota",
    )
    args = parser.parse_args()
    run_cli(execute, args.target, args.reuse_probe)


if __name__ == "__main__":
    main()
