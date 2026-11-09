"""Synthetic origin choices; no live place lookup or provider credentials."""

ORIGIN_TOKEN = "o" * 43


def origin_choice(identifier="origin-1"):
    return {
        "label": "시청역 · 서울 중구",
        "value": {
            "candidate_id": identifier,
            "location": {
                "label": "시청역",
                "address": "서울 중구",
                "coordinates": {"latitude": 37.565, "longitude": 126.977},
                "region": "seoul",
                "supported": True,
            },
            "match_reasons": ["name_exact", "place_type"],
        },
    }


def origin_clarification(mode="confirm", count=1):
    return {
        "question": "이곳에서 출발하시나요?",
        "required_fields": ["origin"],
        "pending_patch": {"max_travel_seconds": None},
        "choices": [origin_choice(f"origin-{i + 1}") for i in range(count)],
        "origin_context": {
            "context_token": ORIGIN_TOKEN,
            "mode": mode,
            "preferred_candidate_id": "origin-1" if mode == "confirm" else None,
            "expires_at": "2030-01-01T00:05:00Z",
            "search_limited": False,
        },
    }
