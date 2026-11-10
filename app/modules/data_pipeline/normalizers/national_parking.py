"""National parking fields; billing intervals use minutes confirmed by the project owner."""

from collections import Counter, defaultdict
from datetime import time

from app.modules.data_pipeline.normalizers.parking import parking_identity
from app.modules.data_pipeline.normalizers.parking_attributes import parking_count, text_value
from app.modules.data_pipeline.normalizers.values import number

SOURCE = "national_parking"
METHODS = ("PrkSttusInfo", "PrkOprInfo", "PrkRealtimeInfo")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Holiday")
FIELDS = {
    "PrkSttusInfo": (
        "prk_center_id",
        "prk_plce_adres_sido",
        "prk_plce_nm",
        "prk_plce_adres",
        "prk_plce_entrc_la",
        "prk_plce_entrc_lo",
        "prk_cmprt_co",
        "parkng_chrge_se",
        "parkng_chrge_se_nm",
        "prk_manage_agent",
        "prk_manage_agent_nm",
    ),
    "PrkOprInfo": ("prk_center_id", "basic_info", "fxamt_info", *DAYS),
    "PrkRealtimeInfo": (
        "prk_center_id",
        "pkfc_ParkingLots_total",
        "pkfc_Available_ParkingLots_total",
    ),
}


def selected_fields(method, row):
    """Discard unused provider fields before handing the page to storage."""
    if not isinstance(row, dict):
        return None
    selected = {key: row[key] for key in FIELDS[method] if key in row}
    if method == "PrkOprInfo":

        def keep(value, keys):
            return (
                {key: value[key] for key in keys if key in value}
                if isinstance(value, dict)
                else value
            )

        for group, keys in (
            (
                "basic_info",
                (
                    "parking_chrge_bs_time",
                    "parking_chrge_adit_unit_time",
                    "parking_chrge_bs_chrge",
                    "parking_chrge_adit_unit_chrge",
                ),
            ),
            ("fxamt_info", ("parking_chrge_one_day_chrge",)),
        ):
            if group in selected:
                selected[group] = keep(selected[group], keys)
        for day in DAYS:
            if day in selected:
                value = selected[day]
                keys = ("opertn_start_time", "opertn_end_time")
                selected[day] = (
                    [keep(period, keys) for period in value]
                    if isinstance(value, list)
                    else keep(value, keys)
                )
    return selected


def management_id(row):
    value = row.get("prk_center_id") if isinstance(row, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_management_id")
    return value.strip()


def select_records(candidates):
    """A documented management ID may link methods; conflicting rows cannot be ranked by order."""
    groups, reasons, selected, blocked = defaultdict(list), Counter(), [], set()
    duplicates = 0
    for candidate in candidates:
        try:
            groups[management_id(candidate.record)].append(candidate)
        except ValueError as exc:
            reasons[str(exc)] += 1
    for key, group in sorted(groups.items()):
        unique = []
        for item in sorted(
            group,
            key=lambda c: (c.received_at, c.origin, c.row_index if c.row_index is not None else -1),
            reverse=True,
        ):
            if any(item.record == other.record for other in unique):
                duplicates += 1
            else:
                unique.append(item)
        if len(unique) != 1:
            reasons["conflicting_management_id"] += len(unique)
            blocked.add(key)
        else:
            selected.append(unique[0])
    return selected, duplicates, reasons, blocked


def facility(row):
    code = management_id(row)
    if row.get("prk_plce_adres_sido") != "서울특별시":
        raise ValueError("unconfirmed_seoul_facility")
    name = text_value(row.get("prk_plce_nm"))
    result = parking_identity(
        SOURCE,
        code,
        name,
        text_value(row.get("prk_plce_adres")),
        row.get("prk_plce_entrc_la"),
        row.get("prk_plce_entrc_lo"),
    )
    capacity = parking_count(row.get("prk_cmprt_co"))
    payment = text_value(row.get("parkng_chrge_se"))
    paid = {"1": False, "2": True}.get(payment)
    issues = {}
    if capacity.reason:
        issues["registered_capacity"] = capacity.reason
    if paid is None:
        issues["conditions.is_paid"] = "UNCONFIRMED_PAYMENT_CODE"
    return dict(
        **result,
        registered_capacity=capacity.value,
        source_updated_at=None,
        conditions={
            "payment_code": payment,
            "payment_name": text_value(row.get("parkng_chrge_se_nm")),
            "is_paid": paid,
            "management_type_code": text_value(row.get("prk_manage_agent")),
            "management_type_name": text_value(row.get("prk_manage_agent_nm")),
        },
        # A facility response has no authority to clear separate operational facts.
        fees=None,
        operating_hours=None,
        availability=None,
        field_issues=issues,
    )


def _clock(value, *, end=False):
    if not isinstance(value, str) or len(value.strip()) != 6 or not value.strip().isascii():
        return None, None
    value = value.strip()
    if not value.isdigit():
        return None, None
    if end and value == "240000":
        return time(0), 1
    try:
        return time(int(value[:2]), int(value[2:4]), int(value[4:])), 0
    except ValueError:
        return None, None


def hours(day, value):
    if value is None or value == []:
        return [
            dict(
                day_type=day.lower(),
                period_no=1,
                opens_at=None,
                closes_at=None,
                end_day_offset=None,
                state="missing",
                reason_code="MISSING_HOURS",
                field_issues={},
            )
        ]
    values = value if isinstance(value, list) else [value]
    periods = []
    for index, interval in enumerate(values, 1):
        if not isinstance(interval, dict):
            interval = {}
        start, _ = _clock(interval.get("opertn_start_time"))
        end, offset = _clock(interval.get("opertn_end_time"), end=True)
        reason = None
        if start is None or end is None:
            reason = "INVALID_HHMMSS"
        elif not offset and start == end:
            reason = "AMBIGUOUS_EQUAL_TIMES"
        elif not offset and start > end:
            reason = "UNCONFIRMED_OVERNIGHT"
        periods.append(
            dict(
                day_type=day.lower(),
                period_no=index,
                opens_at=start.isoformat() if start else None,
                closes_at=end.isoformat() if end else None,
                end_day_offset=offset if not reason else None,
                state="valid"
                if reason is None
                else "invalid"
                if reason == "INVALID_HHMMSS"
                else "unknown",
                reason_code=reason,
                field_issues={},
            )
        )
    # Multiple overlapping periods do not prove continuous opening.
    valid = [p for p in periods if p["state"] == "valid"]
    for i, a in enumerate(valid):
        for b in valid[i + 1 :]:
            a_end = "24:00:00" if a["end_day_offset"] else a["closes_at"]
            b_end = "24:00:00" if b["end_day_offset"] else b["closes_at"]
            if a["opens_at"] < b_end and b["opens_at"] < a_end:
                for p in (a, b):
                    p.update(state="unknown", reason_code="OVERLAPPING_HOURS", end_day_offset=None)
    return periods


def operation(row):
    if not any(key in row for key in (*DAYS, "basic_info", "fxamt_info")):
        raise ValueError("missing_operation_fields")
    if any(key in row and not isinstance(row[key], dict) for key in ("basic_info", "fxamt_info")):
        raise ValueError("unconfirmed_fee_structure")
    issues = {}
    basic, flat = row.get("basic_info"), row.get("fxamt_info")
    if not isinstance(basic, dict):
        basic = {}
        issues["fees.basic_info"] = "UNCONFIRMED_FEE_STRUCTURE"
    if not isinstance(flat, dict):
        flat = {}
        issues["fees.fxamt_info"] = "UNCONFIRMED_FEE_STRUCTURE"

    def numeric(group, key, path):
        value = number(group.get(key))
        if value.reason:
            issues["fees." + path] = value.reason
        return value.value

    fees = dict(
        base_minutes=numeric(basic, "parking_chrge_bs_time", "base_minutes"),
        additional_minutes=numeric(basic, "parking_chrge_adit_unit_time", "additional_minutes"),
        base_fee_krw=numeric(basic, "parking_chrge_bs_chrge", "base_fee_krw"),
        additional_fee_krw=numeric(basic, "parking_chrge_adit_unit_chrge", "additional_fee_krw"),
        daily_max_krw=None,
        daily_flat_fee_krw=numeric(flat, "parking_chrge_one_day_chrge", "daily_flat_fee_krw"),
    )
    for period in ("base", "additional"):
        minutes, amount = fees[period + "_minutes"], fees[period + "_fee_krw"]
        fees[period + "_usable"] = minutes is not None and minutes > 0 and amount is not None
        if not fees[period + "_usable"] and (minutes is not None or amount is not None):
            issues["fees." + period] = (
                "NONPOSITIVE_BILLING_INTERVAL" if minutes == 0 else "INCOMPLETE_FEE_PAIR"
            )
    periods = [period for day in DAYS for period in hours(day, row.get(day))]
    for p in periods:
        if p["reason_code"]:
            issues[f"operating_hours.{p['day_type']}.{p['period_no']}"] = p["reason_code"]
    return dict(fees=fees, operating_hours=periods, field_issues=issues, issue_section="operation")


def availability(row, received_at):
    total = parking_count(row.get("pkfc_ParkingLots_total"))
    available = parking_count(row.get("pkfc_Available_ParkingLots_total"))
    reason = (
        "INVALID_OR_MISSING_COUNT"
        if total.value is None or available.value is None
        else "COUNT_EXCEEDS_CAPACITY"
        if available.value > total.value
        else "SOURCE_TIME_UNKNOWN"
    )
    # The official/current response has no observation timestamp. Do not substitute receipt time.
    return dict(
        provision_code=None,
        provision_name=None,
        provision_state="provided",
        scope="provider_record",
        method="reported",
        total_spaces=total.value,
        occupied_spaces=None,
        reported_available_spaces=available.value,
        calculated_available_spaces=None,
        observed_at=None,
        received_at=received_at,
        usable=False,
        reason_code=reason,
    )
