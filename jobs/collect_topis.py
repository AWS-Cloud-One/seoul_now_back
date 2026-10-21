"""Bounded parallel TOPIS collection with per-cycle status and quota backoff."""

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError
from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.repositories.provider import save_snapshot


async def execute(links):
    settings = Settings()
    key = (
        settings.seoul_topis_api_key.get_secret_value() or settings.seoul_api_key.get_secret_value()
    )
    engine = create_engine(settings)
    factory = async_sessionmaker(engine)
    queue = asyncio.Queue()
    unique = list(dict.fromkeys(links))
    for link in unique:
        queue.put_nowait(link)
    counts = Counter()
    stop = asyncio.Event()
    pace = asyncio.Lock()
    progress_lock = asyncio.Lock()
    started = datetime.now(UTC).isoformat()

    async def progress(status):
        async with progress_lock:
            async with factory.begin() as session:
                await save_snapshot(
                    session,
                    "topis_collection_status",
                    {
                        "status": status,
                        "started_at": started,
                        "target": len(unique),
                        "completed": sum(counts.values()),
                        "success": counts["success"],
                        "no_observation": counts["provider_no_observation"],
                        "failed": sum(
                            v
                            for k, v in counts.items()
                            if k not in {"success", "provider_no_observation"}
                        ),
                        "error_counts": dict(counts),
                        "remaining": len(unique) - sum(counts.values()),
                    },
                )

    try:
        await progress("running")
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=8)) as client:

            async def worker():
                while not stop.is_set():
                    try:
                        link = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    async with pace:
                        await asyncio.sleep(0.05)
                    if stop.is_set():
                        return
                    try:
                        data = await collect_topis(client, key, link)
                        async with factory.begin() as session:
                            await save_snapshot(session, "topis:" + link, data)
                            await save_snapshot(
                                session, "topis_attempt:" + link, {"status": "success"}
                            )
                        status = "success"
                    except CollectionError as exc:
                        status = str(exc)
                        async with factory.begin() as session:
                            await save_snapshot(
                                session,
                                "topis_attempt:" + link,
                                {"status": "failed", "error_code": status},
                            )
                        if status in {
                            "provider_quota_exceeded",
                            "provider_auth_error",
                            "missing_topis_api_key",
                        }:
                            stop.set()
                    counts[status] += 1
                    print(
                        json.dumps(
                            {
                                "link_id": link,
                                "status": "success" if status == "success" else "failed",
                                "error_code": None if status == "success" else status,
                            }
                        ),
                        flush=True,
                    )
                    if sum(counts.values()) % 50 == 0:
                        await progress("running")

            await asyncio.gather(*(worker() for _ in range(8)))
        await progress("paused_provider_limit" if stop.is_set() else "complete")
        print(
            json.dumps(
                {"cycle_complete": not stop.is_set(), "target": len(unique), "counts": dict(counts)}
            ),
            flush=True,
        )
    finally:
        await engine.dispose()
    return 4 if stop.is_set() else int(any(k != "success" for k in counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="append", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(execute(args.link)))
