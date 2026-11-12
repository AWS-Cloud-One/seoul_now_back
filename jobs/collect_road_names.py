"""Collect TOPIS road names in explicit, bounded batches; no scheduled AWS execution yet."""

import argparse
import re
from functools import partial

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.road_names import collect_road_name
from app.modules.data_pipeline.pipelines.road_names import run_road_names
from jobs.runtime import finish, run_cli


def link_id(value):
    if not re.fullmatch(r"[0-9]{10}", value):
        raise argparse.ArgumentTypeError("link ID must contain 10 digits")
    return value


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="append", type=link_id, default=[])
    parser.add_argument("--max-links", type=int, default=20, help="Batch limit (1..200)")
    parser.add_argument("--after-link", type=link_id, help="Continue after a previous next_after")
    parser.add_argument("--refresh", action="store_true", help="Also query roads already named")
    args = parser.parse_args(argv)
    if not 1 <= args.max_links <= 200 or len(set(args.link)) > args.max_links:
        parser.error("max-links must be 1..200 and cover all explicit links")
    if args.link and args.after_link:
        parser.error("--link and --after-link cannot be combined")
    return args


async def execute(options=None):
    options = options or arguments([])
    settings = Settings()
    key = (
        settings.seoul_topis_api_key.get_secret_value() or settings.seoul_api_key.get_secret_value()
    )
    if not key:
        return finish({"status": "failed", "error_code": "missing_topis_api_key"})
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as client:
            result = await run_road_names(
                engine,
                lambda link: collect_road_name(client, key, link),
                limit=options.max_links,
                links=options.link,
                after=options.after_link,
                refresh=options.refresh,
            )
        return finish(result)
    finally:
        await engine.dispose()


def main():
    run_cli(partial(execute, arguments()))


if __name__ == "__main__":
    main()
