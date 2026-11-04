from dataclasses import dataclass
from datetime import UTC, datetime

from app.clients.seoul import CollectionError, SeoulClient


@dataclass(frozen=True)
class ParkingSnapshot:
    rows: list[dict]
    total: int
    received_at: datetime
    complete: bool
    error_code: str | None = None


async def collect_parking(client: SeoulClient, page_size: int = 1000) -> ParkingSnapshot:
    """Retain successful pages and explicitly prevent partial input from retiring records."""
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    total, first = await client.fetch_page(1, page_size)
    if total == 0:
        raise CollectionError("empty_snapshot")
    if total > 100_000:
        raise CollectionError("snapshot_budget_exceeded")
    rows = list(first)
    received = datetime.now(UTC)
    error = "snapshot_incomplete" if len(first) != min(total, page_size) else None
    for start in range(page_size + 1, total + 1, page_size):
        try:
            current_total, page = await client.fetch_page(start, start + page_size - 1)
        except CollectionError as exc:
            error = error or str(exc)
            if str(exc) in {"provider_auth_error", "provider_quota_exceeded"}:
                error = str(exc)
                break
            continue
        received = datetime.now(UTC)
        if current_total != total or len(page) != min(page_size, total - start + 1):
            error = error or "snapshot_changed_or_incomplete"
        rows.extend(page)
    if len(rows) != total:
        error = error or "snapshot_incomplete"
    return ParkingSnapshot(rows, total, received, error is None, error)
