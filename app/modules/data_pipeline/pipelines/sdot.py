"""Store current sensor observations using imported DB locations; no files or response archives."""

from app.modules.data_pipeline.normalizers.sdot import observation_summary, sdot_snapshot
from app.repositories.collection import CollectionRepository
from app.repositories.sensors import SensorWriter


async def save_sdot(session, reference, collected, *, started_at=None, error_code=None):
    repo = CollectionRepository(session)
    run = await repo.start("sdot_people", "all", started_at=started_at)
    if collected is None:
        collected = dict(
            rows=[],
            row_contexts=[],
            pages=[],
            issues=[error_code or "sdot_collection_failed"],
            provider_total=None,
            query={},
            query_complete=False,
        )
    data = sdot_snapshot(reference, collected)
    try:
        async with session.begin_nested():
            result = await session.run_sync(
                lambda sync: SensorWriter(sync).store(data, run.started_at)
            )
    except ValueError:
        result = dict(observations_written=0, sensors_checked=0, sensor_results=[])
        error_code = "sensor_reference_changed_during_collection"
    states = [item["status"] for item in result["sensor_results"]]
    failed = bool(error_code or (not collected["rows"] and collected["issues"]))
    partial = bool(
        collected["issues"]
        or data["observation_quality"]["rejected"]
        or any(state not in {"valid", "unobserved"} for state in states)
    )
    status = "failed" if failed else "partial" if partial else "success"
    error_code = error_code or (
        "sdot_collection_failed" if failed else "partial_sdot_storage" if partial else None
    )
    accepted = sum(state in {"valid", "unobserved"} for state in states)
    count = len(data["items"])
    await repo.finish(
        run,
        status=status,
        error_code=error_code,
        received=count,
        accepted=accepted,
        rejected=count - accepted,
        completeness="unknown",
    )
    return dict(
        source="sdot_people",
        status=status,
        error_code=error_code,
        records=count,
        storage=result,
        observation_summary=observation_summary(data["items"]),
        observation_quality=data["observation_quality"],
        query_complete=collected["query_complete"],
    )
