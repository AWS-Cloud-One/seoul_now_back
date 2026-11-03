"""CLI for projecting archived citydata parking into typed records."""

import argparse

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.pipelines.parking import project_citydata_parking
from jobs.runtime import finish, run_cli


async def execute() -> int:
    engine = create_engine(Settings())
    try:
        result = await project_citydata_parking(engine)
        return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run_cli(execute)


if __name__ == "__main__":
    main()
