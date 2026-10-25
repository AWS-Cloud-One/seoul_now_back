"""Explicit scheduled collectors. ITS is not in the automatic schedule."""

import asyncio
import json
from datetime import UTC, datetime

from jobs.collect_supplemental import execute


async def main():
    due = {"sdot": 0.0, "parking": 0.0, "areas": asyncio.get_running_loop().time() + 86400}
    intervals = {"sdot": 600, "parking": 86400, "areas": 604800}
    while True:
        for target in due:
            now = asyncio.get_running_loop().time()
            if now >= due[target]:
                result = await execute(target)
                print(json.dumps({"time": datetime.now(UTC).isoformat(), **result}), flush=True)
                due[target] = asyncio.get_running_loop().time() + intervals[target]
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
