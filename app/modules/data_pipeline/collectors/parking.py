from app.clients.seoul import CollectionError, SeoulClient


async def collect_parking(client: SeoulClient, page_size: int = 1000) -> list[dict]:
    """Fetch a complete snapshot; never silently accept truncated pages."""
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    total, first = await client.parking_page(1, page_size)
    if total == 0:
        raise CollectionError("empty_snapshot")
    if total > 100_000:
        raise CollectionError("snapshot_budget_exceeded")
    rows = list(first)
    for start in range(page_size + 1, total + 1, page_size):
        current_total, page = await client.parking_page(start, start + page_size - 1)
        if current_total != total or len(rows) != start - 1:
            raise CollectionError("snapshot_changed_or_incomplete")
        rows.extend(page)
    if len(rows) != total:
        raise CollectionError("snapshot_incomplete")
    return rows
