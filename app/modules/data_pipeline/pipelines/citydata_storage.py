"""Select current CITYDATA fields, store population and pass parking candidates in memory."""

from collections import Counter

from app.models import PopulationLatest
from app.modules.data_pipeline.normalizers.citydata import CityDataError
from app.modules.data_pipeline.normalizers.citydata_sections import select_citydata, unwrap_citydata
from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE, normalize_parking
from app.modules.data_pipeline.normalizers.parking_attributes import ParkingCountOutOfRange
from app.modules.data_pipeline.normalizers.parking_resolution import ParkingCandidate
from app.modules.data_pipeline.normalizers.storage_fields import population
from app.modules.data_pipeline.pipelines.parking import CityDataParkingBatch
from app.modules.data_pipeline.results import NormalizationOutcome, ProcessingCounts
from app.repositories.collection import CollectionRepository
from app.repositories.population import PopulationWriter


def normalize_population(session, area, data, received_at):
    writer = PopulationWriter(session)
    details, updated = {"section_status": "invalid", "field_issues": {}}, False
    try:
        values = population(data, received_at=received_at)
        details = values.pop("quality")
        error = details.pop("error_code")
    except CityDataError as exc:
        error = str(exc)
        details["section_status"] = {
            "missing_population_section": "missing",
            "empty_population_section": "empty",
            "conflicting_population_rows": "conflicting",
        }.get(error, "invalid")
    if not error:
        if received_at is None:
            error = "missing_population_receipt"
        elif not writer.has_area(area):
            error = "missing_population_geometry"
        else:
            updated = writer.observation(area, values, received_at)
            error = (
                "population_time_guard"
                if not updated
                else "partial_population_values"
                if details["field_issues"]
                else None
            )
    return NormalizationOutcome(
        "partial" if error else "success",
        error,
        ProcessingCounts(
            unit="snapshot",
            scope="population_normalization",
            received=1,
            accepted=int(updated),
            rejected=int(not updated),
            conflicts=int(error == "conflicting_population_rows"),
            rejection_reasons={error: 1} if error and not updated else {},
        ),
        {**details, "updated": updated},
    )


async def store_citydata(
    session,
    run,
    body,
    received_at,
    *,
    expected_code,
    expected_name,
    require_envelope=False,
    attempted_at=None,
):
    repo = CollectionRepository(session)
    sections, quality, parking = {}, {}, CityDataParkingBatch(sections=1)
    error, selection = None, None
    try:
        city = unwrap_citydata(
            body,
            expected_code=expected_code,
            expected_name=expected_name,
            require_envelope=require_envelope,
        )
        selection = select_citydata(city, received_at, include_road_geometry=False)
        sections = {name: dict(state) for name, state in selection["sections"].items()}
        data = selection["CITYDATA"]
        parking_state = sections["parking"]
        parking.section_issues = int(parking_state["state"] in {"missing", "invalid"})
        rows = data.get("PRK_STTS")
        if isinstance(rows, list):
            parking.received = len(rows)
            for index, row in enumerate(rows):
                try:
                    record = normalize_parking(row, received_at, CITYDATA_PARKING_SOURCE)
                except ParkingCountOutOfRange as exc:
                    parking.reasons[str(exc)] += 1
                    continue
                except ValueError, TypeError, KeyError, AttributeError:
                    parking.reasons["invalid_parking_row"] += 1
                    continue
                parking.candidates.append(
                    ParkingCandidate(record, received_at, expected_code, index)
                )
        parking_state["processing"] = "pending_storage"
        try:
            async with session.begin_nested():
                outcome = await session.run_sync(
                    lambda sync: normalize_population(sync, expected_code, data, received_at)
                )
            quality = {**outcome.counts.as_dict(), **outcome.details}
            sections["population"].update(
                processing=outcome.status,
                error_code=outcome.error_code,
                **quality,
            )
        except Exception:
            sections["population"].update(
                processing="failed",
                error_code="population_normalization_failed",
                updated=False,
            )
        population_state = sections["population"]
        await session.run_sync(
            lambda sync: PopulationWriter(sync).attempt(
                expected_code,
                attempted_at or run.started_at,
                population_state["processing"],
                population_state.get("error_code"),
                population_state.get("updated", False),
            )
        )
        current = await session.get(PopulationLatest, ("seoul_citydata", expected_code))
        population_state["stored_observation"] = (
            {
                "observed_at": current.observed_at.isoformat() if current.observed_at else None,
                "received_at": current.received_at.isoformat() if current.received_at else None,
                "reused": not population_state.get("updated", False),
            }
            if current
            else None
        )
    except CityDataError as exc:
        error = str(exc)
        parking.section_issues = 1
        await session.run_sync(
            lambda sync: PopulationWriter(sync).attempt(
                expected_code,
                attempted_at or run.started_at,
                "failed",
                error,
            )
        )
    if error:
        status, reasons = "failed", Counter({error: 2})
    else:
        errors = [
            value.get("error_code")
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
        ]
        reasons = Counter(value for value in errors if value)
        status = "partial" if reasons else "success"
        error = next(iter(reasons), None)
    counts = ProcessingCounts(
        unit="section",
        scope="citydata_section_processing",
        received=2,
        accepted=2 - sum(reasons.values()),
        rejected=sum(reasons.values()),
        rejection_reasons=dict(reasons),
    )
    # The cycle summary counts actual records: one population observation per response,
    # plus parking rows when their final batch is committed. Section diagnostics stay local.
    updated = bool(sections.get("population", {}).get("updated"))
    population_error = error if not sections else sections["population"].get("error_code")
    await repo.add_counts(
        run.id,
        received=1,
        accepted=int(updated),
        rejected=int(not updated),
        rejection_reasons={population_error: 1} if population_error else {},
    )
    return {
        "status": status,
        "error_code": error,
        **counts.as_dict(),
        "received_at": received_at.isoformat() if received_at else None,
        "sections": sections,
        "population_quality": quality,
        "unknown_keys": selection["unknown_keys"] if selection else [],
    }, parking
