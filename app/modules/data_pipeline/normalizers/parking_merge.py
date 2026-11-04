"""Merge complementary CITYDATA registration/live rows within one exact source identity."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE, normalize_parking
from app.modules.data_pipeline.normalizers.parking_attributes import text_value


def _key(candidate):
    return (
        candidate.received_at or datetime.min.replace(tzinfo=UTC),
        candidate.origin,
        str(candidate.payload_id),
        candidate.row_index if candidate.row_index is not None else -1,
        fingerprint(candidate.record.get("input_fields")),
    )


def _known(items, value):
    known = [item for item in items if value(item) is not None]
    if known and any(value(item) != value(known[0]) for item in known[1:]):
        raise ValueError("conflicting_parking_registration")
    return min(known or items, key=_key)


def merge_citydata_candidates(items):
    """Return (merged, error); None/None means the ordinary strict policy applies.

    In a mixed N/Y group, prefer a non-live row for the registration name and a
    providing row for the observation. This project selection policy applies only
    to already equal source/code/coordinates, never fuzzy identity.
    """
    if any(
        c.record["source"] != CITYDATA_PARKING_SOURCE or "input_fields" not in c.record
        for c in items
    ):
        return None, None
    if len({(c.record["source_id"], c.record["position"]) for c in items}) != 1:
        return None, None
    static = [c for c in items if c.record["availability"]["provision_state"] == "not_provided"]
    live = [c for c in items if c.record["availability"]["provision_state"] == "provided"]
    if not static or not live or len(static) + len(live) != len(items):
        return None, None
    try:
        return _merge(items, static, live), None
    except ValueError as exc:
        return None, str(exc)


def _merge(items, static, live):
    ordered = sorted(items, key=_key)
    raw, donors = {}, {}

    def take(fields, candidate):
        for field in fields:
            raw[field] = candidate.record["input_fields"].get(field)
            donors[field] = ordered.index(candidate)

    def scalar(field, normalized, issue_path=None):
        if issue_path and any(
            c.record["field_issues"].get(issue_path) not in {None, "MISSING_VALUE"} for c in items
        ):
            raise ValueError("conflicting_parking_registration")
        take((field,), _known(items, normalized))

    name = _known(static, lambda c: c.record["name"])
    take(("PRK_CD", "PRK_NM", "LAT", "LNG", "LON"), name)
    for field in ("ROAD_ADDR", "ADDRESS"):
        scalar(field, lambda c, field=field: text_value(c.record["input_fields"].get(field)))
    scalar("CPCTY", lambda c: c.record["registered_capacity"], "registered_capacity")
    scalar("PRK_TYPE", lambda c: c.record["conditions"].get("parking_type_name"))
    scalar("PAY_YN", lambda c: c.record["conditions"].get("payment_code"), "conditions.is_paid")
    for period, fields in (
        ("base", ("TIME_RATES", "RATES")),
        ("additional", ("ADD_TIME_RATES", "ADD_RATES")),
    ):
        paths = (period + "_minutes", period + "_fee_krw")
        for path in paths:
            if any(
                c.record["field_issues"].get("fees." + path) not in {None, "MISSING_VALUE"}
                for c in items
            ):
                raise ValueError("conflicting_parking_registration")
            _known(items, lambda c, path=path: c.record["fees"][path])
        complete = [c for c in items if c.record["fees"][period + "_usable"]]
        # A minutes-only row and an amount-only row do not prove a usable price pair.
        known_paths = {
            path for path in paths if any(c.record["fees"][path] is not None for c in items)
        }
        candidates = complete or [
            c for c in items if all(c.record["fees"][path] is not None for path in known_paths)
        ]
        if not candidates:
            raise ValueError("conflicting_parking_registration")
        take(fields, min(candidates, key=_key))

    def observation_facts(candidate):
        return {k: v for k, v in candidate.record["availability"].items() if k != "received_at"}

    live_facts = [observation_facts(c) for c in live]
    if all(value == live_facts[0] for value in live_facts):
        observation = min(live, key=_key)
    else:
        if any(not c.record["availability"]["usable"] for c in live):
            raise ValueError("conflicting_parking_observation")
        times = [c.record["availability"]["observed_at"] for c in live]
        for stamp in set(times):
            same = [
                observation_facts(c)
                for c in live
                if c.record["availability"]["observed_at"] == stamp
            ]
            if any(value != same[0] for value in same[1:]):
                raise ValueError("conflicting_parking_observation")
        observation = min(
            (c for c in live if c.record["availability"]["observed_at"] == max(times)), key=_key
        )
    static_donors = set(donors.values())
    receipts = [ordered[i].received_at for i in static_donors]
    received = min(receipts) if all(receipts) else None
    take(("CUR_PRK_YN", "CUR_PRK_CNT", "CUR_PRK_TIME"), observation)
    record = normalize_parking(raw, received, CITYDATA_PARKING_SOURCE)
    # A newer registration receipt must never refresh a live observation's receipt.
    # Keep the original observation tuple, including its own capacity and quality.
    record["availability"] = deepcopy(observation.record["availability"])
    record["field_issues"] = {
        **{k: v for k, v in record["field_issues"].items() if not k.startswith("availability")},
        **{
            k: v
            for k, v in observation.record["field_issues"].items()
            if k.startswith("availability")
        },
    }
    selection = {
        "contract": "citydata-parking-merged-v1",
        "rule": (
            "same source/code/exact coordinates; non-live registration name; "
            "road address preferred; "
            "fee pairs from one input; observation selected independently"
        ),
        "inputs": [
            {
                "payload_id": str(c.payload_id),
                "row_index": c.row_index,
                "origin": c.origin,
                "received_at": c.received_at.isoformat() if c.received_at else None,
                "input_sha256": fingerprint(c.record["input_fields"]),
                "input_fields": c.record["input_fields"],
            }
            for c in ordered
        ],
        "field_sources": donors,
        "observation_source": ordered.index(observation),
        "registration_time_basis": (
            "earliest contributing registration receipt; unknown if any missing"
        ),
    }
    return replace(
        name, record=record, received_at=received, selection=selection, observation=observation
    )
