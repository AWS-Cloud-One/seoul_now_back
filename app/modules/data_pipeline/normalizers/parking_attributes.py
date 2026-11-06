"""Selected Seoul parking facts. No database, facility matching or fee estimation."""

from decimal import Decimal

from app.modules.data_pipeline.normalizers.values import flag, missing, number, timestamp

SOURCE = "seoul_getparkinfo"
MUNICIPAL_PARKING_SOURCE = "seoul_municipal_parking"
CITYDATA_PARKING_SOURCE = "seoul_citydata_prk"

# Source fields remain explicit: similarly named codes need not share meanings.
FEE_FIELDS = {
    SOURCE: ("PRK_HM", "PRK_CRG", "ADD_UNIT_TM_MNT", "ADD_CRG", "DLY_MAX_CRG"),
    MUNICIPAL_PARKING_SOURCE: (
        "BSC_PRK_HR",
        "BSC_PRK_CRG",
        "ADD_PRK_HR",
        "ADD_PRK_CRG",
        "DAY_MAX_CRG",
    ),
    CITYDATA_PARKING_SOURCE: ("TIME_RATES", "RATES", "ADD_TIME_RATES", "ADD_RATES", None),
}
CONDITION_FIELDS = {
    SOURCE: {
        "parking_type_code": "PKLT_KND",
        "parking_type_name": "PKLT_KND_NM",
        "operation_code": "OPER_SE",
        "operation_name": "OPER_SE_NM",
        "phone": "TELNO",
        "payment_code": "CHGD_FREE_SE",
        "payment_name": "CHGD_FREE_NM",
        "night_free_open_code": "NGHT_FREE_OPN_YN",
        "night_free_open_name": "NGHT_FREE_OPN_YN_NAME",
        "saturday_payment_code": "SAT_CHGD_FREE_SE",
        "saturday_payment_name": "SAT_CHGD_FREE_NM",
        "holiday_payment_code": "LHLDY_YN",
        "holiday_payment_name": "LHLDY_NM",
        "management_group_id": "CRB_PKLT_MNG_GROUP_NO",
        "realtime_link_code": "PRK_NOW_INFO_PVSN_YN",
        "realtime_link_name": "PRK_NOW_INFO_PVSN_YN_NM",
    },
    MUNICIPAL_PARKING_SOURCE: {
        "parking_type_code": "PKLT_TYPE",
        "parking_type_name": "PRK_TYPE_NM",
        "operation_code": "OPER_SE",
        "operation_name": "OPER_SE_NM",
        "phone": "TELNO",
        "payment_code": "PAY_YN",
        "payment_name": "PAY_YN_NM",
        "night_free_open_code": "NGHT_PAY_YN",
        "night_free_open_name": "NGHT_PAY_YN_NM",
        "saturday_payment_code": "SAT_CHGD_FREE_SE",
        "saturday_payment_name": "SAT_CHGD_FREE_NM",
        "holiday_payment_code": "LHLDY_CHGD_FREE_SE",
        "holiday_payment_name": "LHLDY_CHGD_FREE_SE_NAME",
        "management_group_id": "STRT_PKLT_MNG_NO",
        "sharing_code": "SHRN_PKLT_YN",
        "sharing_notes": "SHRN_PKLT_ETC",
    },
    CITYDATA_PARKING_SOURCE: {"parking_type_name": "PRK_TYPE", "payment_code": "PAY_YN"},
}


def text_value(value):
    return str(value).strip() or None if value is not None else None


def _clock(value, *, end=False):
    """HHMM, including numeric source values which lost leading zeroes."""
    if missing(value):
        return None, "MISSING_VALUE"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        parsed = number(value, integer=True)
        value = str(parsed.value) if parsed.state == "valid" else "invalid"
    if not isinstance(value, str):
        return None, "INVALID_HHMM"
    raw = value.strip()
    if not raw.isascii() or not raw.isdigit() or not 1 <= len(raw) <= 4:
        return None, "INVALID_HHMM"
    raw = raw.zfill(4)
    hour, minute = int(raw[:2]), int(raw[2:])
    if end and (hour, minute) == (24, 0):
        return 1440, None
    if hour > 23 or minute > 59:
        return None, "INVALID_HHMM"
    return hour * 60 + minute, None


def operating_hours(day_type, start, end):
    """24:00 explicitly ends next day; reversed/equal times do not prove overnight/24h."""
    a, a_reason = _clock(start)
    b, b_reason = _clock(end, end=True)
    reasons = {key: value for key, value in (("start", a_reason), ("end", b_reason)) if value}
    state, reason, offset = "valid", None, 0
    if reasons:
        state = "invalid" if "INVALID_HHMM" in reasons.values() else "missing"
        reason = "INVALID_HHMM" if state == "invalid" else "MISSING_HOURS"
        offset = None
    elif a == b:
        state, reason, offset = "unknown", "AMBIGUOUS_EQUAL_TIMES", None
    elif b < a:
        state, reason, offset = "unknown", "UNCONFIRMED_OVERNIGHT", None
    elif b == 1440:
        offset = 1

    def display(minutes):
        if minutes is None:
            return None
        return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"

    return dict(
        day_type=day_type,
        opens_at=display(a),
        closes_at=display(b),
        end_day_offset=offset,
        state=state,
        reason_code=reason,
        field_issues=reasons,
    )


def normalize_attributes(row, collected_at, source):
    if source not in FEE_FIELDS:
        raise ValueError("unsupported_parking_source")
    issues = {}

    def parse(path, parser, value, **kwargs):
        parsed = parser(value, **kwargs)
        if parsed.reason:
            issues[path] = parsed.reason
        return parsed.value

    names = (
        "base_minutes",
        "base_fee_krw",
        "additional_minutes",
        "additional_fee_krw",
        "daily_max_krw",
    )
    fees = {
        name: parse("fees." + name, number, row.get(key)) if key else None
        for name, key in zip(names, FEE_FIELDS[source], strict=True)
    }
    for period in ("base", "additional"):
        minutes, amount = fees[period + "_minutes"], fees[period + "_fee_krw"]
        fees[period + "_usable"] = minutes is not None and minutes > 0 and amount is not None
        if not fees[period + "_usable"] and (minutes is not None or amount is not None):
            issues["fees." + period] = (
                "NONPOSITIVE_BILLING_INTERVAL" if minutes == 0 else "INCOMPLETE_FEE_PAIR"
            )
    # A supplied maximum of zero is preserved, but is not evidence of free parking.
    conditions = {name: text_value(row.get(key)) for name, key in CONDITION_FIELDS[source].items()}
    for code, name in (
        ("payment_code", "is_paid"),
        ("saturday_payment_code", "saturday_is_paid"),
        ("holiday_payment_code", "holiday_is_paid"),
        ("night_free_open_code", "night_free_open"),
        ("sharing_code", "is_shared"),
    ):
        if code in conditions:
            conditions[name] = parse("conditions." + name, flag, conditions[code])

    hours = []
    if source != CITYDATA_PARKING_SOURCE:
        holiday = (
            ("LHLDY_BGNG", "LHLDY")
            if source == SOURCE
            else ("LHLDY_OPER_BGNG_TM", "LHLDY_OPER_END_TM")
        )
        for day, start, end in (
            ("weekday", "WD_OPER_BGNG_TM", "WD_OPER_END_TM"),
            ("weekend", "WE_OPER_BGNG_TM", "WE_OPER_END_TM"),
            ("holiday", *holiday),
        ):
            period = operating_hours(day, row.get(start), row.get(end))
            hours.append(period)
            if period["reason_code"]:
                issues["operating_hours." + day] = period["reason_code"]

    capacity = parse(
        "registered_capacity",
        number,
        row.get("CPCTY" if source == CITYDATA_PARKING_SOURCE else "TPKCT"),
        integer=True,
    )
    updated = (
        parse("source_updated_at", timestamp, row.get("LAST_DATA_SYNC_TM"), not_after=collected_at)
        if source == SOURCE
        else None
    )
    availability = (
        None
        if source == SOURCE
        else _availability(row, source, capacity, collected_at, parse, issues)
    )
    return dict(
        registered_capacity=capacity,
        fees=fees,
        conditions=conditions,
        operating_hours=hours,
        availability=availability,
        source_updated_at=updated,
        field_issues=issues,
    )


def _availability(row, source, total, received_at, parse, issues):
    municipal = source == MUNICIPAL_PARKING_SOURCE
    code = text_value(row.get("PRK_STTS_YN" if municipal else "CUR_PRK_YN"))
    label = text_value(row.get("PRK_STTS_NM")) if municipal else None
    if municipal:
        # The public municipal sample confirms 1/current. Other linkage states are
        # interpreted only with their explicit provider label, not from a Y/N guess.
        state = "provided" if code == "1" else "unknown"
        if code == "0" and label == "미연계중":
            state = "not_provided"
    else:
        provided = parse("availability.provided", flag, code)
        state = (
            "provided" if provided is True else "not_provided" if provided is False else "unknown"
        )
    if municipal and state == "unknown":
        issues["availability.provided"] = (
            "MISSING_VALUE" if code is None else "UNCONFIRMED_PROVISION_CODE"
        )
    stamp = parse(
        "availability.observed_at",
        timestamp,
        row.get("NOW_PRK_VHCL_UPDT_TM" if municipal else "CUR_PRK_TIME"),
        not_after=received_at,
    )
    count = parse(
        "availability.occupied_spaces" if municipal else "availability.reported_available_spaces",
        number,
        row.get("NOW_PRK_VHCL_CNT" if municipal else "CUR_PRK_CNT"),
        integer=True,
    )
    reason = None
    if state != "provided":
        reason = "NOT_PROVIDED" if state == "not_provided" else "UNKNOWN_PROVISION"
    elif total is None or count is None:
        reason = "INVALID_OR_MISSING_COUNT"
    elif count > total:
        reason = "COUNT_EXCEEDS_CAPACITY"
    elif stamp is None:
        reason = issues["availability.observed_at"]
    elif received_at is None:
        reason = "MISSING_RECEIVED_TIME"
    usable = reason is None
    if reason:
        issues["availability"] = reason
    return dict(
        provision_code=code,
        provision_name=label,
        provision_state=state,
        scope="provider_record",
        total_spaces=total,
        occupied_spaces=count if municipal else None,
        reported_available_spaces=None if municipal else count,
        calculated_available_spaces=total - count if municipal and usable else None,
        method="calculated" if municipal else "reported",
        observed_at=stamp,
        received_at=received_at,
        usable=usable,
        reason_code=reason,
    )
