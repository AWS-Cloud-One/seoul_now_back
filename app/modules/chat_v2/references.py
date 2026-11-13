"""Bind UI/model references to owned, stored results before reading facts."""

from uuid import UUID

from sqlalchemy import select

from app.models import ChatRun, ChatRunCommit
from app.modules.chat_v2.dialogue_context import PlaceReference
from app.modules.chat_v2.responses import ChatResponse
from app.schemas.query_services import FoundPlace


def recommendation_references(response):
    recommendation = response.recommendation
    if recommendation is None:
        return []
    refs = []
    for card in recommendation.cards:
        place, info = card.place.value, card.place
        refs.append(
            PlaceReference(
                ref_id=f"result:{recommendation.result_id}:{card.rank}",
                collection_id=recommendation.result_id,
                result_id=recommendation.result_id,
                position=card.rank,
                place=FoundPlace(
                    **place.model_dump(exclude={"purpose"}),
                    distance_meters=None,
                    collected_at=info.collected_at
                    or response.snapshot.finalization.prepared.prepared_at,
                ),
            )
        )
    return refs


def merge_references(previous, incoming, limit=30):
    replaced = {r.collection_id for r in incoming}
    values = [r for r in previous if r.collection_id not in replaced] + list(incoming)
    while len(values) > limit:
        oldest = values[0].collection_id
        values = [r for r in values if r.collection_id != oldest]
    return values


async def owned_recommendation(db, conversation_id: UUID, result_id: UUID):
    row = await db.scalar(
        select(ChatRun)
        .join(ChatRunCommit, ChatRun.run_id == ChatRunCommit.run_id)
        .where(ChatRun.conversation_id == conversation_id, ChatRunCommit.result_id == result_id)
    )
    if row is None or row.response is None:
        return None
    response = ChatResponse.model_validate(row.response)
    if response.recommendation is None or response.recommendation.result_id != result_id:
        raise ValueError("Stored recommendation ownership mismatch")
    return response


def resolve_targets(selectors, context, viewed):
    """Never interpret the same position against a different result or collection."""
    references = context.references
    collection = (viewed.result_id or viewed.collection_id) if viewed else None
    if collection:
        references = [r for r in references if r.collection_id == collection]
    chosen = []
    for selector in selectors:
        if selector.kind == "reference":
            matches = [r for r in references if r.ref_id == selector.ref_id]
        elif selector.kind == "position":
            matches = [r for r in references if r.position == selector.position]
        elif selector.kind == "name":
            matches = [r for r in references if r.place.name == selector.name]
        else:
            focus = viewed.place_ref if viewed and viewed.place_ref else context.focus_ref
            matches = [r for r in references if r.ref_id == focus] if focus else references
        if len(matches) != 1:
            return None
        if matches[0] not in chosen:
            chosen.append(matches[0])
    return chosen
