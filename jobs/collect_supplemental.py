"""CLI for bounded supplemental collection; processing lives in the pipeline."""

import argparse

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.pipelines.supplemental import SOURCES, run_supplemental
from jobs.runtime import finish, run_cli


async def execute(target) -> int:
    settings = Settings()
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as http:
            result = await run_supplemental(
                engine,
                http,
                settings,
                target,
            )
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=SOURCES)
    args = parser.parse_args()
    run_cli(execute, args.target)


if __name__ == "__main__":
    main()
