"""Refresh S-DoT only, ten minutes after each collection completes."""

import asyncio
import json
from datetime import UTC, datetime

from jobs.collect_supplemental import execute


async def main():
    while True:
        try:
            result = await execute("sdot")
        except Exception:
            result = {"source": "sdot_people", "status": "failed", "error_code": "collection_error"}
        print(json.dumps({"time": datetime.now(UTC).isoformat(), **result}), flush=True)
        await asyncio.sleep(600)


if __name__ == "__main__":
    asyncio.run(main())