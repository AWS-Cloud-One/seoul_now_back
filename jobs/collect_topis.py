"""CLI for bounded TOPIS collection; processing lives in the pipeline."""

import argparse

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.modules.data_pipeline.pipelines.topis import CONCURRENCY, run_topis
from jobs.runtime import finish, report, run_cli


async def execute(links) -> int:
    settings = Settings()
    key = (
        settings.seoul_topis_api_key.get_secret_value() or settings.seoul_api_key.get_secret_value()
    )
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=CONCURRENCY)) as client:
            result = await run_topis(
                engine, lambda link: collect_topis(client, key, link), links, report=report
            )
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="append", required=True)
    args = parser.parse_args()
    run_cli(execute, args.link)


if __name__ == "__main__":
    main()
