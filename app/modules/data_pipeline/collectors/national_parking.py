"""Bounded national parking batches with independently resumable static scans."""

from datetime import UTC, datetime
from urllib.parse import unquote
from uuid import uuid4

import httpx

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.normalizers.national_parking import selected_fields
from app.modules.data_pipeline.normalizers.values import number

METHODS = ("PrkSttusInfo", "PrkOprInfo", "PrkRealtimeInfo")
STATIC_METHODS = METHODS[:2]


def new_scan(page_size):
    return {
        "scan_id": str(uuid4()),
        "next_page": 1,
        "page_size": page_size,
        "provider_total": None,
        "consistent_total": True,
        "page_hashes": {},
        "pending_end": None,
    }


async def collect_national_parking(http, settings, *, page_size=1000, max_pages=5, progress=None):
    if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 5:
        raise ValueError("parking_request_limit")
    progress = progress or {}
    datasets = {}
    for method in METHODS:
        resumable = method in STATIC_METHODS
        before = progress.get(method) if resumable else None
        state = dict(before) if before and before["page_size"] == page_size else new_scan(page_size)
        state["page_hashes"] = dict(state["page_hashes"])
        start_page = state["next_page"]
        rows, pages, ids = [], [], set()
        total, error, complete = None, None, False
        scan_complete = end_observed = terminal_confirmed = False
        warnings = []
        if before and before["page_size"] != page_size:
            warnings.append("parking_page_size_changed_restart")
        for page in range(start_page, start_page + max_pages):
            try:
                response = await http.get(
                    f"https://apis.data.go.kr/B553881/Parking_v1/{method}_v1",
                    params={
                        "serviceKey": unquote(settings.data_go_kr_api_key.get_secret_value()),
                        "numOfRows": page_size,
                        "pageNo": page,
                        "format": 2,
                        "sidoCd": "11",
                    },
                )
                received = datetime.now(UTC).isoformat()
                evidence = dict(page=page, received_at=received, rows=[], usable=False)
                pages.append(evidence)
                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError:
                    raise CollectionError("parking_invalid_response") from None
                if not isinstance(body, dict) or str(body.get("resultCode")) not in {"0", "00"}:
                    raise CollectionError("parking_provider_error")
                count = number(body.get("totalCount"), integer=True)
                batch = body.get(method)
                if count.state != "valid" or not isinstance(batch, list) or len(batch) > page_size:
                    raise CollectionError("parking_invalid_page")
                if total is not None and count.value != total:
                    raise CollectionError("parking_total_changed")
                if "pageNo" in body and number(body["pageNo"], integer=True).value != page:
                    raise CollectionError("parking_page_mismatch")
                total = count.value
                if state["provider_total"] is not None and total != state["provider_total"]:
                    # A multi-run scan is not an immutable snapshot. Continue visiting pages,
                    # but never claim a consistent full scan when the provider count moves.
                    state["consistent_total"] = False
                    warnings.append("parking_total_changed_between_batches")
                state["provider_total"] = total
                batch = [selected_fields(method, row) for row in batch]
                digest = fingerprint(batch)
                if batch and digest in state["page_hashes"].values():
                    raise CollectionError("parking_repeated_page")
                evidence["usable"] = True
                evidence["rows"] = batch
                rows.extend(batch)
                ids.update(
                    str(row["prk_center_id"]).strip()
                    for row in batch
                    if isinstance(row, dict) and row.get("prk_center_id")
                )
                expected = min(page_size, max(0, total - (page - 1) * page_size))
                if len(batch) != expected:
                    # An early short/empty page may be transient. Retry it next run. Only
                    # the same terminal response in two committed batches ends this scan.
                    pending = {"page": page, "digest": digest, "total": total}
                    if resumable and len(batch) < page_size and state["pending_end"] == pending:
                        terminal_confirmed = end_observed = True
                        warnings.append("parking_reported_total_mismatch")
                        state = new_scan(page_size)
                    else:
                        state["pending_end"] = pending
                        state["next_page"] = page
                    error = (
                        "parking_premature_end"
                        if len(batch) < expected
                        else "parking_count_or_id_mismatch"
                    )
                    break
                state["pending_end"] = None
                state["page_hashes"][str(page)] = digest
                state["next_page"] = page + 1
                if (page - 1) * page_size + len(batch) >= total:
                    end_observed = terminal_confirmed = True
                    scan_complete = state["consistent_total"]
                    complete = start_page == 1 and len(rows) == total and len(ids) == total
                    if start_page == 1 and not complete:
                        error = "parking_count_or_id_mismatch"
                    state = new_scan(page_size)
                    break
            except (httpx.HTTPError, CollectionError) as exc:
                error = str(exc) if isinstance(exc, CollectionError) else "parking_http_error"
                if pages and pages[-1]["page"] == page:
                    pages[-1]["error_code"] = error
                break
        if not end_observed and error is None:
            error = "parking_page_limit"
        pagination = {
            "resumable": resumable,
            "before": before,
            "after": state,
            "start_page": start_page,
            "page_size": page_size,
            "batch_complete": error in {None, "parking_page_limit"},
            "scan_complete": scan_complete,
            "end_observed": end_observed,
            "terminal_confirmed": terminal_confirmed,
            "snapshot_consistent": False,
            "warnings": warnings,
        }
        datasets[method] = dict(
            rows=rows,
            pages=pages,
            provider_total=total,
            complete=complete,
            status="success" if complete else "partial" if rows or end_observed else "failed",
            error_code=error,
            pagination=pagination,
        )
    return {
        "source": "한국교통안전공단",
        "datasets": datasets,
        "scope": "서울 필터·기능별 최대 5000행; 시설/운영은 저장된 다음 페이지부터",
    }
