"""Keyset history over canonical runs; no second copy of messages or results."""

import base64
import binascii
import json
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, select

from app.models import ChatRun
from app.modules.chat_v2.presentation_contracts import UserMessage
from app.modules.chat_v2.storage_contracts import HistoryPage, HistoryTurn, StorageRejected
from app.schemas.common import ContractModel


class HistoryCursor(ContractModel):
    conversation_id: UUID
    snapshot: int = Field(strict=True, ge=1)
    before: int = Field(strict=True, ge=1)

    def encode(self):
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token):
        try:
            if not token or len(token) > 512:
                raise ValueError
            data = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            value = cls.model_validate(json.loads(data))
            if value.before > value.snapshot:
                raise ValueError
            return value
        except ValueError, TypeError, binascii.Error, UnicodeError:
            raise StorageRejected("INVALID_CURSOR") from None


def user_message(run):
    if run.user_message:
        return UserMessage.model_validate(run.user_message)
    # Read pre-P5 records without requiring a backfill of original user text.
    value = run.request.get("input", {})
    kind = value.get("type", "message")
    return UserMessage(
        message_id=f"{run.run_id}:user",
        input_type=kind,
        text=value.get("text", "실패 작업 재시도" if kind == "retry" else "선택 실행"),
        source_run_id=value.get("source_run_id"),
    )


class HistoryStore:
    async def history(self, credential, conversation_id, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise StorageRejected("INVALID_CURSOR")
        async with self.writers.begin() as db:
            conversation = await self._owned(db, credential, conversation_id, lock=True)
            query = select(ChatRun).where(ChatRun.conversation_id == conversation_id)
            if cursor:
                page = HistoryCursor.decode(cursor)
                if page.conversation_id != conversation_id:
                    raise StorageRejected("INVALID_CURSOR")
                anchor = await db.scalar(
                    select(ChatRun.run_id).where(
                        ChatRun.conversation_id == conversation_id,
                        ChatRun.sequence == page.snapshot,
                    )
                )
                if anchor is None:
                    raise StorageRejected("INVALID_CURSOR")
                snapshot, before = page.snapshot, page.before
            else:
                snapshot = await db.scalar(
                    select(func.coalesce(func.max(ChatRun.sequence), 0)).where(
                        ChatRun.conversation_id == conversation_id,
                    )
                )
                before = snapshot + 1
            rows = list(
                await db.scalars(
                    query.where(
                        ChatRun.sequence <= snapshot,
                        ChatRun.sequence < before,
                    )
                    .order_by(ChatRun.sequence.desc())
                    .limit(limit + 1)
                )
            )
            more, rows = len(rows) > limit, rows[:limit]
            next_cursor = (
                HistoryCursor(
                    conversation_id=conversation_id,
                    snapshot=snapshot,
                    before=rows[-1].sequence,
                ).encode()
                if more
                else None
            )
            items = [
                HistoryTurn(
                    sequence=run.sequence,
                    created_at=run.created_at,
                    user_message=user_message(run),
                    assistant_message_id=f"{run.run_id}:assistant",
                    run=await self.view(db, run, conversation),
                )
                for run in reversed(rows)
            ]
            return HistoryPage(
                conversation_id=conversation_id,
                state_version=conversation.state_version,
                snapshot_sequence=snapshot,
                items=items,
                next_cursor=next_cursor,
                active_run_id=conversation.active_run_id,
            )
