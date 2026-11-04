"""Conservative parking identity and duplicate decisions, without HTTP or SQL."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from app.modules.data_pipeline.normalizers.parking_merge import merge_citydata_candidates


@dataclass(frozen=True)
class ParkingCandidate:
    record: dict
    payload_id: object
    received_at: datetime | None
    origin: str
    row_index: int | None = None
    selection: dict | None = None
    observation: ParkingCandidate | None = None


def _facts(record):
    static = {
        key: record[key]
        for key in (
            "source",
            "source_id",
            "name",
            "address",
            "position",
            "registered_capacity",
            "fees",
            "conditions",
            "operating_hours",
            "source_updated_at",
        )
    }
    static["issues"] = {
        k: v for k, v in record["field_issues"].items() if not k.startswith("availability")
    }
    live = record["availability"]
    if live is not None:
        live = {k: v for k, v in live.items() if k != "received_at"}
        live["issues"] = {
            k: v for k, v in record["field_issues"].items() if k.startswith("availability")
        }
    return static, live


def select_parking_candidates(candidates, *, conflicts=None):
    """Deduplicate equal selected facts; a live timestamp cannot rank static fees."""
    groups = defaultdict(list)
    for candidate in candidates:
        row = candidate.record
        groups[(row["source"], row["source_id"], row["position"])].append(candidate)
    selected, reasons, duplicates = [], Counter(), 0
    for key in sorted(groups):
        # Stable provenance for equal facts, independent of response/area iteration order.
        ordered = sorted(
            groups[key],
            key=lambda c: (
                c.received_at or datetime.min.replace(tzinfo=UTC),
                c.origin,
                str(c.payload_id),
            ),
            reverse=True,
        )
        unique = []
        facts = []
        for candidate in ordered:
            value = _facts(candidate.record)
            if value in facts:
                duplicates += 1
            else:
                unique.append(candidate)
                facts.append(value)
        if len(unique) == 1:
            selected.append(unique[0])
            continue
        merged, merge_error = merge_citydata_candidates(unique)
        if merged is not None:
            selected.append(merged)
            # The existing row accounting includes consolidated inputs in duplicates.
            # The projection report separates exact duplicates from complementary inputs.
            duplicates += len(unique) - 1
            continue
        if merge_error:
            reasons[merge_error] += len(unique)
            if conflicts is not None:
                conflicts.extend((c, merge_error) for c in unique)
            continue
        if any(static != facts[0][0] for static, _ in facts[1:]):
            reasons["conflicting_parking_registration"] += len(unique)
            if conflicts is not None:
                conflicts.extend((c, "conflicting_parking_registration") for c in unique)
            continue
        lives = [live for _, live in facts]
        times = [live.get("observed_at") if live else None for live in lives]
        if (
            any(live is None or not live["usable"] for live in lives)
            or any(stamp is None for stamp in times)
            or len(set(times)) != len(times)
        ):
            reasons["conflicting_parking_observation"] += len(unique)
            if conflicts is not None:
                conflicts.extend((c, "conflicting_parking_observation") for c in unique)
            continue
        # Only the live values differ, and every distinct observation has a valid time.
        chosen = max(range(len(unique)), key=lambda i: times[i])
        selected.append(unique[chosen])
        reasons["older_parking_observation"] += len(unique) - 1
    return selected, duplicates, reasons


def resolve_parking_identities(rows, existing):
    """Match within one source/code; never infer a facility from code or proximity alone.

    Existing dictionaries include a WGS84 `position` tuple supplied by the repository.
    The old hash is a retained migration anchor, not a newly minted persistent ID.
    Resolve the whole batch before any write so two rows cannot race for the same UUID.
    """
    by_code = defaultdict(list)
    for old in existing:
        by_code[(old["source"], old["provider_record_id"])].append(old)
    resolved, reasons = [], Counter()
    for row in rows:
        known = by_code[(row["source"], row["source_id"])]
        if not known:
            resolved.append((row, None))
            continue
        legacy = [old for old in known if old["legacy_record_id"] == row["identity_key"]]
        position = [old for old in known if old["position"] == row["position"]]
        named = [
            old
            for old in known
            if row["name"]
            and row["address"]
            and old["name"] == row["name"]
            and old["address"] == row["address"]
        ]
        evidence = {
            matches[0]["source_record_id"]: matches[0]
            for matches in (legacy, position, named)
            if len(matches) == 1
        }
        if len(evidence) == 1:
            resolved.append((row, next(iter(evidence.values()))))
        else:
            reason = (
                "ambiguous_parking_identity"
                if evidence or position or named
                else "identity_change_requires_review"
            )
            reasons[reason] += 1
    claims = Counter(old["source_record_id"] for _, old in resolved if old is not None)
    unique = []
    for row, old in resolved:
        if old is not None and claims[old["source_record_id"]] > 1:
            reasons["ambiguous_parking_identity"] += 1
        else:
            unique.append((row, old))
    return unique, reasons
