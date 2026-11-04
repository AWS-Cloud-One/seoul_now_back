"""Select and validate one current TOPIS observation without SQL or response retention."""

from app.modules.data_pipeline.normalizers.storage_fields import road_traffic_fields


def traffic_observation(link, data, received_at):
    details = {"source_time_status": "unknown", "value_status": "not_evaluated"}
    rows = data.get("rows") if isinstance(data, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("link_id") != link
    ):
        return dict(
            values=None, status="partial", error_code="invalid_link_response", processing=details
        )
    values = road_traffic_fields(rows[0])
    details.update(
        {key: values.pop(key) for key in ("value_status", "field_issues", "value_counts")}
    )
    error = (
        values["reason_code"].lower()
        if details["value_status"] in {"missing", "invalid"}
        else "missing_traffic_receipt"
        if received_at is None
        else None
    )
    if error:
        return dict(
            values=None,
            status="partial" if error == "missing_traffic_receipt" else "failed",
            error_code=error,
            processing=details,
        )
    partial = details["value_status"] == "partial"
    return dict(
        values={**values, "received_at": received_at},
        status="partial" if partial else "success",
        error_code="partial_traffic_values" if partial else None,
        processing=details,
    )
