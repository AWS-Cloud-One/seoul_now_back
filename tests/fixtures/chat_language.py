"""Synthetic contextual language cases; no production conversations or credentials."""

BASE = {
    "origin": {
        "label": "서울역",
        "coordinates": {"latitude": 37.5547, "longitude": 126.9707},
        "address": "서울 중구 한강대로 405",
        "region": "seoul",
        "supported": True,
    },
    "purposes": [{"purpose": "cafe", "keyword": None}],
    "group_type": None,
    "age_groups": None,
    "headcount": 2,
    "max_travel_seconds": 1800,
}
SELECTED = {"name": "해물데이트", "kind": "recommendation"}
DRAFT = {
    "status": "active",
    "unanswered_turns": 1,
    "headcount_hint": {"minimum": 6, "quote": "6명 이상"},
    "clarification": {
        "question": "정확히 몇 명인가요?",
        "required_fields": ["headcount"],
        "pending_patch": {"max_travel_seconds": 1200},
        "choices": [],
    },
}
CASES = [
    {"id": "keep", "message": "이 조건으로 카페 추천해줘"},
    {
        "id": "change_purpose",
        "message": "이 조건으로 카페 추천해줘",
        "conditions": {"purposes": [{"purpose": "food", "keyword": None}]},
    },
    {
        "id": "confirmed",
        "message": "이 조건으로 카페 추천해줘",
        "origin_confirmation": {
            "mode": "confirmed",
            "candidates": [{"name": "서울역", "address": "서울 중구 한강대로 405"}],
        },
        "recent_messages": [
            {"role": "user", "content": "서울역에서 출발할게"},
            {"role": "assistant", "content": "방문 목적은 무엇인가요?"},
        ],
    },
    {"id": "reason", "message": "왜 추천했어?", "topic": "recommendation_reason"},
    {
        "id": "named_reason",
        "message": "해물데이트는 왜 추천했어?",
        "topic": "recommendation_reason",
    },
    {"id": "population", "message": "주변 현재 인구 현황 알려줘", "topic": "population"},
    {"id": "area_population", "message": "이 주변에 지금 사람 얼마나 있어?", "topic": "population"},
    {"id": "indoor", "message": "이 매장 안에 지금 사람 얼마나 있어?", "topic": "scope_notice"},
    {
        "id": "reason_no_target",
        "message": "왜 추천했어?",
        "topic": "recommendation_reason",
        "selected_place": None,
    },
    {
        "id": "historical_reason",
        "message": "왜 추천했어?",
        "topic": "recommendation_reason",
        "conditions": {"origin": None, "purposes": []},
    },
    {
        "id": "pending_population",
        "message": "주변 현재 인구 현황 알려줘",
        "topic": "population",
        "condition_dialogue": DRAFT,
        "recent_messages": [
            {"role": "user", "content": "6명 이상이고 20분 이내로 바꿔줘"},
            {"role": "assistant", "content": "정확히 몇 명인가요?"},
        ],
    },
    {
        "id": "different_reason",
        "message": "다른 카페는 왜 추천했어?",
        "topic": "recommendation_reason",
        "target": "unresolved",
    },
    {
        "id": "missing_origin",
        "message": "이 조건으로 카페 추천해줘",
        "conditions": {"origin": None, "purposes": []},
    },
    {"id": "change_origin", "message": "출발지는 강남역으로 바꾸고 카페 추천해줘"},
    {"id": "destination", "message": "이 조건으로 성수동 카페 추천해줘", "keyword": "성수동"},
    {
        "id": "two_info",
        "message": "여기 추천 이유랑 현재 인구 알려줘",
        "topic": ["recommendation_reason", "population"],
    },
    {"id": "keep_other", "message": "조건 그대로 다른 카페 추천해줘"},
]
