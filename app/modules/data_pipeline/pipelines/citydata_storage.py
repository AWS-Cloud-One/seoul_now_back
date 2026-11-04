"""Archive full/selected CITYDATA and report independent section outcomes."""

from app.models import PopulationLatest
from app.modules.data_pipeline.normalizers.citydata import CityDataError
from app.modules.data_pipeline.normalizers.citydata_sections import select_citydata, unwrap_citydata
from app.modules.data_pipeline.results import ProcessingCounts
from app.repositories.collection import CollectionRepository


async def store_citydata(
    session,
    run,
    body,
    received_at,
    normalize,
    *,
    expected_code=None,
    expected_name=None,
    original=None,
    require_envelope=False,
):
    repo = CollectionRepository(session)
    original = original or await repo.archive(run.id, body, received_at)
    error, selected_payload, selection, quality = None, None, None, {}
    sections = {}
    try:
        city = unwrap_citydata(
            body,
            expected_code=expected_code,
            expected_name=expected_name,
            require_envelope=require_envelope,
        )
        run.target_key = "area:" + city["AREA_CD"].strip()
        selection = select_citydata(city, received_at)
        selection.update(
            input_payload_id=str(original.payload_id),
            input_received_at=received_at.isoformat() if received_at else None,
        )
        selected_payload = await repo.archive(
            run.id,
            selection,
            None,
            kind="derived_snapshot",
            part="selected",
            legacy={"parent_payload_id": original.payload_id},
        )
        sections = {
            name: dict(
                state, processing="pending_projection" if name == "parking" else "reference_only"
            )
            for name, state in selection["sections"].items()
        }
        try:
            async with session.begin_nested():
                outcome = await session.run_sync(
                    lambda sync: normalize(
                        sync,
                        "seoul_citydata",
                        selection["CITYDATA"],
                        original.payload_id,
                        received_at=received_at,
                    )
                )
            quality = {**outcome.counts.as_dict(), **outcome.details}
            sections["population"].update(
                processing=outcome.status, error_code=outcome.error_code, **quality
            )
        except Exception:
            sections["population"].update(
                processing="failed", error_code="population_normalization_failed", updated=False
            )
        current = await session.get(PopulationLatest, ("seoul_citydata", city["AREA_CD"].strip()))
        sections["population"]["stored_observation"] = (
            {
                "payload_id": str(current.payload_id),
                "observed_at": current.observed_at.isoformat() if current.observed_at else None,
                "reused": current.payload_id != original.payload_id,
            }
            if current
            else None
        )
    except CityDataError as exc:
        error = str(exc)
    if not error:
        errors = {
            name: value.get("error_code")
            or (
                f"partial_{name}_rows"
                if value.get("row_validation", {}).get("rejected") and value["state"] == "usable"
                else None
            )
            or (
                f"{value['state']}_{name}_section"
                if value["state"] in {"missing", "invalid"}
                else None
            )
            for name, value in sections.items()
        }
        errors = {k: v for k, v in errors.items() if v}
        accepted = 3 - len(errors)
        status = "partial" if errors else "success"
        error = next(iter(errors.values()), None)
        reasons = {v: list(errors.values()).count(v) for v in errors.values()}
    else:
        accepted, status, reasons = 0, "failed", {error: 3}
    counts = ProcessingCounts(
        unit="section",
        scope="citydata_section_processing",
        received=3,
        accepted=accepted,
        rejected=3 - accepted,
        rejection_reasons=reasons,
    )
    report = {
        "status": status,
        "error_code": error,
        **counts.as_dict(),
        "input_payload_id": str(original.payload_id),
        "selected_payload_id": str(selected_payload.payload_id) if selected_payload else None,
        "received_at": received_at.isoformat() if received_at else None,
        "sections": sections,
        "population_quality": quality,
        "unknown_keys": selection["unknown_keys"] if selection else [],
        "accepted_scope": (
            "population processing and parking/road input availability; "
            "parking writes are a separate projection"
        ),
    }
    await repo.archive(
        run.id,
        report,
        None,
        kind="processing_report",
        part="sections",
        legacy={"parent_payload_id": original.payload_id},
    )
    await repo.finish(run, status=status, error_code=error, **counts.database_counts())
    return report
