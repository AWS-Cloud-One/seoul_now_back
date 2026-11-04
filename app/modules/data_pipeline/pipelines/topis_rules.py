"""Pure TOPIS target selection and cycle outcome rules; no I/O."""


def scheduled_links(links, attempts, limit):
    return sorted(links, key=lambda link: (attempts.get(link, ""), link))[:limit]


def select_targets(links, max_links, *, scheduled_batch=False, attempts=None):
    if not links:
        raise ValueError("missing_official_geometry")
    links = sorted(set(links))
    total = len(links)
    if scheduled_batch:
        links = scheduled_links(links, attempts or {}, max_links)
    return links, {
        "scope": "scheduled_official_batch" if scheduled_batch else "all_official_geometry",
        "total_links": total,
        "selected_links": len(links),
        "truncated": len(links) < total,
    }


def cycle_status(counts, outcomes, stopped):
    if stopped:
        return "paused_provider_limit"
    if outcomes.get("failed", 0) > counts.get("provider_no_observation", 0):
        return "partial" if outcomes.get("success", 0) or outcomes.get("partial", 0) else "failed"
    if outcomes.get("partial", 0) or counts.get("provider_no_observation", 0):
        return "partial"
    return "success"
