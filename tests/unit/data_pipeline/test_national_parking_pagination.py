import asyncio

import httpx

from app.core.config import Settings
from app.modules.data_pipeline.collectors.national_parking import (
    METHODS,
    collect_national_parking,
    new_scan,
)


def collect(progress=None, *, failure=None, total=20, max_pages=2, page_size=2):
    calls = []

    def respond(request):
        method = request.url.path.rsplit("/", 1)[1].removesuffix("_v1")
        page = int(request.url.params["pageNo"])
        calls.append((method, page))
        if failure and (response := failure(method, page)) is not None:
            return response
        rows = [
            {"prk_center_id": str(i)}
            for i in range((page - 1) * page_size, min(page * page_size, total))
        ]
        return httpx.Response(200, json={"resultCode": "0", "totalCount": total, method: rows})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            return await collect_national_parking(
                http,
                Settings(_env_file=None),
                progress=progress,
                page_size=page_size,
                max_pages=max_pages,
            )

    return asyncio.run(run()), calls


def after(data):
    return {method: data["datasets"][method]["pagination"]["after"] for method in METHODS[:2]}


def test_static_methods_resume_independently_while_live_always_starts_at_one():
    progress = {
        METHODS[0]: {**new_scan(2), "next_page": 3},
        METHODS[1]: {**new_scan(2), "next_page": 2},
    }
    data, calls = collect(progress)
    assert calls == [
        (METHODS[0], 3),
        (METHODS[0], 4),
        (METHODS[1], 2),
        (METHODS[1], 3),
        (METHODS[2], 1),
        (METHODS[2], 2),
    ]
    assert after(data)[METHODS[0]]["next_page"] == 5
    assert after(data)[METHODS[1]]["next_page"] == 4
    assert all(d["pagination"]["batch_complete"] for d in data["datasets"].values())
    assert not any(
        d["complete"] or d["pagination"]["scan_complete"] for d in data["datasets"].values()
    )


def test_failed_page_is_the_next_page_and_successful_prefix_is_not_re_requested():
    first, _ = collect(
        failure=lambda method, page: (
            httpx.Response(503) if method == METHODS[0] and page == 2 else None
        )
    )
    assert after(first)[METHODS[0]]["next_page"] == 2
    assert after(first)[METHODS[1]]["next_page"] == 3
    second, calls = collect(after(first))
    assert calls[:2] == [(METHODS[0], 2), (METHODS[0], 3)]
    assert after(second)[METHODS[0]]["next_page"] == 4


def test_finished_scan_restarts_but_last_batch_is_not_a_complete_inventory():
    first, _ = collect(total=6)
    second, calls = collect(after(first), total=6)
    assert calls[:1] == [(METHODS[0], 3)]
    for method in METHODS[:2]:
        data = second["datasets"][method]
        assert not data["complete"] and data["pagination"]["scan_complete"]
        assert data["pagination"]["end_observed"]
        assert data["pagination"]["after"]["next_page"] == 1
    _, calls = collect(after(second), total=6)
    assert calls[0] == (METHODS[0], 1)


def test_cross_batch_total_change_continues_without_claiming_a_consistent_scan():
    first, _ = collect(total=6)
    second, calls = collect(after(first), total=5)
    assert calls[0] == (METHODS[0], 3)
    for method in METHODS[:2]:
        pagination = second["datasets"][method]["pagination"]
        assert pagination["end_observed"] and not pagination["scan_complete"]
        assert "parking_total_changed_between_batches" in pagination["warnings"]
        assert pagination["after"]["next_page"] == 1


def test_reported_total_mismatch_requires_two_committed_terminal_responses():
    first, _ = collect(total=20)

    def empty(method, page):
        if method in METHODS[:2]:
            return httpx.Response(200, json={"resultCode": "0", "totalCount": 20, method: []})

    second, _ = collect(after(first), failure=empty)
    for method in METHODS[:2]:
        assert after(second)[method]["next_page"] == 3
        assert not second["datasets"][method]["pagination"]["end_observed"]
    third, _ = collect(after(second), failure=empty)
    for method in METHODS[:2]:
        page = third["datasets"][method]["pagination"]
        assert page["end_observed"] and page["terminal_confirmed"]
        assert not page["scan_complete"] and not third["datasets"][method]["complete"]
        assert page["after"]["next_page"] == 1
        assert "parking_reported_total_mismatch" in page["warnings"]


def test_repeated_page_across_batches_is_not_advanced():
    first, _ = collect(total=20)

    def repeat(method, page):
        if method == METHODS[0]:
            return httpx.Response(
                200,
                json={
                    "resultCode": "0",
                    "totalCount": 20,
                    method: [{"prk_center_id": "0"}, {"prk_center_id": "1"}],
                },
            )

    second, _ = collect(after(first), failure=repeat)
    assert second["datasets"][METHODS[0]]["error_code"] == "parking_repeated_page"
    assert after(second)[METHODS[0]]["next_page"] == 3


def test_page_size_change_restarts_instead_of_skipping_rows():
    first, _ = collect(total=20)
    second, calls = collect(after(first), total=20, page_size=1)
    assert calls[0] == (METHODS[0], 1)
    assert (
        "parking_page_size_changed_restart"
        in second["datasets"][METHODS[0]]["pagination"]["warnings"]
    )
