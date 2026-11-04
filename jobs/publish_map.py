"""CLI for publishing normalized data; application rules live in MapPublicationService."""

import argparse

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.services.map_publication import MapPublicationService
from jobs.runtime import finish, run_cli


async def execute(layer) -> int:
    engine = create_engine(Settings())
    try:
        async with async_sessionmaker(engine).begin() as session:
            count = await MapPublicationService(session).publish(layer)
        return finish({"status": "success", "layer": layer, "features": count})
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=["roads", "population", "parking"])
    args = parser.parse_args()
    run_cli(execute, args.layer)


if __name__ == "__main__":
    main()
