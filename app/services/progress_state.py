"""JSON codec for trip state and cached outcomes shared through PostgreSQL.

Every shape is explicit. An unknown draft or outcome type raises instead of being
dropped or pickled, so a new state field cannot silently disappear after a restart.
A stored `schema` that this code does not know is reported as `StateSchemaError`
and the caller expires that trip (the user starts a new trip, as after a TTL expiry).
"""

from pydantic import TypeAdapter

from app.core.exceptions import ServiceError
from app.modules.recommendation.areas import SearchArea
from app.modules.recommendation.progress import SearchProgress, TripProgress
from app.modules.recommendation.types import Candidate
from app.schemas.pending import ChatData, ChatTarget
from app.schemas.recommendations import Conditions, RecommendationResponse
from app.services.chat_conditions import ConditionDraft
from app.services.chat_tasks import ConversationDraft
from app.services.pending import ConversationOutcome
from app.services.recommendations import RecommendationOutcome
from app.services.state_codec import to_json

SCHEMA = 1

CONDITION_DRAFT = TypeAdapter(ConditionDraft)
CONVERSATION_DRAFT = TypeAdapter(ConversationDraft)


class StateSchemaError(Exception):
    """Stored state was written by an incompatible version of the code."""


def encode_search(search):
    return {
        "fingerprint": search.fingerprint,
        "pages": [[purpose, area, page] for (purpose, area), page in search.pages.items()],
        "ended": [list(key) for key in sorted(search.ended)],
        "remaining": [candidate.model_dump(mode="json") for candidate in search.remaining],
        "seen": sorted(search.seen),
        "areas": None
        if search.areas is None
        else [area.model_dump(mode="json") for area in search.areas],
        "area_warnings": to_json(search.area_warnings),
    }


def decode_search(data):
    return SearchProgress(
        fingerprint=data["fingerprint"],
        pages={(purpose, area): page for purpose, area, page in data["pages"]},
        ended={tuple(key) for key in data["ended"]},
        remaining=[Candidate.model_validate(item) for item in data["remaining"]],
        seen=set(data["seen"]),
        areas=None
        if data["areas"] is None
        else [SearchArea.model_validate(item) for item in data["areas"]],
        area_warnings=data["area_warnings"],
    )


def encode_draft(draft):
    if draft is None:
        return None
    if isinstance(draft, ConditionDraft):
        return {"kind": "condition", "data": CONDITION_DRAFT.dump_python(draft, mode="json")}
    if isinstance(draft, ConversationDraft):
        return {"kind": "conversation", "data": CONVERSATION_DRAFT.dump_python(draft, mode="json")}
    raise TypeError(f"Unsupported draft type: {type(draft).__name__}")


def decode_draft(data):
    if data is None:
        return None
    if data["kind"] == "condition":
        return CONDITION_DRAFT.validate_python(data["data"])
    if data["kind"] == "conversation":
        return CONVERSATION_DRAFT.validate_python(data["data"])
    raise StateSchemaError("unknown draft kind")


def encode_trip(entry):
    """Everything the published trip carries between requests (not the request cache)."""
    return {
        "schema": SCHEMA,
        "conditions": (
            None if entry.conditions is None else entry.conditions.model_dump(mode="json")
        ),
        "version": entry.version,
        "excluded": sorted(entry.excluded),
        "search": encode_search(entry.search),
        "condition_draft": encode_draft(entry.condition_draft),
        "conversation_draft": encode_draft(entry.conversation_draft),
    }


def decode_trip(data, *, expired):
    """Rebuild a TripProgress; `expires_at` is owned by the database column, not this value."""
    if data.get("schema") != SCHEMA:
        raise StateSchemaError("unknown trip state schema")
    entry = TripProgress(expires_at=0.0)
    entry.conditions = (
        None if data["conditions"] is None else Conditions.model_validate(data["conditions"])
    )
    entry.version = data["version"]
    entry.excluded = set(data["excluded"])
    entry.search = decode_search(data["search"])
    entry.condition_draft = decode_draft(data["condition_draft"])
    entry.conversation_draft = decode_draft(data["conversation_draft"])
    entry.expired = expired
    return entry


def encode_failure(failure):
    if failure is None:
        return None
    return {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
        "retry_after_seconds": failure.retry_after_seconds,
    }


def decode_failure(data):
    if data is None:
        return None
    return ServiceError(
        data["code"],
        data["message"],
        retryable=data["retryable"],
        retry_after_seconds=data["retry_after_seconds"],
    )


def encode_outcome(outcome):
    if isinstance(outcome, ConversationOutcome):
        kind, targets = "conversation", [t.model_dump(mode="json") for t in outcome.targets]
    elif isinstance(outcome, RecommendationOutcome):
        kind, targets = "recommendation", []
    else:
        raise TypeError(f"Unsupported outcome type: {type(outcome).__name__}")
    response = outcome.response
    if isinstance(response, ChatData):
        response_type = "chat"
    elif isinstance(response, RecommendationResponse):
        response_type = "recommendation"
    else:
        raise TypeError(f"Unsupported response type: {type(response).__name__}")
    return {
        "kind": kind,
        "response_type": response_type,
        "response": response.model_dump(mode="json"),
        "failure": encode_failure(outcome.failure),
        "targets": targets,
    }


def decode_outcome(data):
    model = {"chat": ChatData, "recommendation": RecommendationResponse}[data["response_type"]]
    response = model.model_validate(data["response"])
    failure = decode_failure(data["failure"])
    if data["kind"] == "conversation":
        targets = tuple(ChatTarget.model_validate(item) for item in data["targets"])
        return ConversationOutcome(response, failure, targets)
    if data["kind"] == "recommendation":
        return RecommendationOutcome(response, failure)
    raise StateSchemaError("unknown outcome kind")
