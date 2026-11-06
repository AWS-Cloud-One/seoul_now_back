"""CLI for TOPIS collection over all official roads or a rotating batch."""

import argparse
import asyncio

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.modules.data_pipeline.pipelines.topis import CONCURRENCY, run_topis_network
from jobs.runtime import finish, run_cli


async def execute(max_links, interval=0, scheduled_batch=False) -> int:
    settings = Settings()
    key = (
        settings.seoul_topis_api_key.get_secret_value() or settings.seoul_api_key.get_secret_value()
    )
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=CONCURRENCY)) as client:
            while True:
                result = await run_topis_network(
                    engine,
                    lambda link: collect_topis(client, key, link),
                    max_links,
                    scheduled_batch,
                )
                code = finish(result)
                if not interval:
                    return code
                await asyncio.sleep(max(interval, 3600) if code == 4 else interval)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all", action="store_true", required=True, help="Use the validated official road list"
    )
    parser.add_argument(
        "--max-links", type=int, default=20, help="Limit for --scheduled-batch only"
    )
    parser.add_argument(
        "--scheduled-batch",
        action="store_true",
        help="Rotate oldest/uncollected links, bounded by max-links",
    )
    parser.add_argument(
        "--interval", type=int, default=0, help="Optional seconds between cycles; default is once"
    )
    args = parser.parse_args()
    if not 1 <= args.max_links <= 200 or args.interval and args.interval < 300:
        parser.error("max-links must be 1..200; interval must be 0 or at least 300")
    run_cli(execute, args.max_links, args.interval, args.scheduled_batch)


if __name__ == "__main__":
    main()
