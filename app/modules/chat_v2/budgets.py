"""Shared, fixed-window limits. Reservations survive restarts and are never refunded.

Input bytes are a conservative capacity reservation, NOT a tokenizer/billing estimate.
At a minute/day boundary two windows may be consumed back to back.
"""

import hashlib
import ipaddress
from datetime import timedelta

import httpx
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.models.chat_v2 import ChatUsageBucket
from app.modules.chat_v2.control import active_control
from app.modules.chat_v2.interpretation import InterpretationFailure
from app.modules.chat_v2.storage_contracts import StorageRejected


def peer_address(request, *, aws):
    # CloudFront generates this header; only trust it on the private VPC-origin path.
    # Never use a viewer-controlled X-Forwarded-For entry as a quota identity.
    value = request.headers.get("cloudfront-viewer-address", "") if aws else request.client.host
    if aws:
        try:
            value = value.rsplit(":", 1)[0].strip("[]")
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise StorageRejected("PEER_UNAVAILABLE") from None
    return value


class ChatBudgets:
    def __init__(self, writers, config, clock):
        self.writers, self.config, self.clock = writers, config, clock

    def peer_key(self, peer):
        # No IP in database/logs; rotate the pseudonymous key daily.
        return hashlib.sha256(f"{self.clock().date()}:{peer}".encode()).hexdigest()

    async def reserve(self, db, rules):
        now = self.clock()
        for name, subject, window, amount, limit in sorted(rules):
            start = now.replace(second=0, microsecond=0)
            if window == "day":
                start = start.replace(hour=0, minute=0)
            if amount > limit:
                raise StorageRejected("RATE_LIMITED")
            stmt = insert(ChatUsageBucket).values(
                bucket=f"{name}:{subject}",
                window_start=start,
                used=amount,
                expires_at=start + timedelta(days=2),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[ChatUsageBucket.bucket, ChatUsageBucket.window_start],
                set_={"used": ChatUsageBucket.used + amount},
                where=ChatUsageBucket.used + amount <= limit,
            ).returning(ChatUsageBucket.used)
            if await db.scalar(stmt) is None:
                raise StorageRejected("RATE_LIMITED")

    async def session(self, db, peer):
        await self.reserve(
            db,
            [
                ("sessions", "global", "minute", 1, 30),
                ("sessions", "global", "day", 1, self.config.chat_sessions_per_day),
                ("sessions", self.peer_key(peer), "minute", 1, 5),
            ],
        )

    async def conversation(self, db, session_id):
        await self.reserve(db, [("conversations", str(session_id), "day", 1, 50)])

    async def run(self, db, session_id, peer):
        await self.reserve(
            db,
            [
                ("runs", "global", "minute", 1, 30),
                ("runs", str(session_id), "minute", 1, 6),
                ("runs", self.peer_key(peer), "minute", 1, 20),
            ],
        )

    async def model(self, input_bytes):
        # Both the initial attempt and the one allowed schema-repair attempt.
        async with self.writers.begin() as db:
            await self.reserve(
                db,
                [
                    ("model_calls", "global", "day", 2, self.config.chat_model_calls_per_day),
                    (
                        "model_input_bytes",
                        "global",
                        "day",
                        2 * (input_bytes + 512),
                        self.config.chat_model_input_bytes_per_day,
                    ),
                    (
                        "model_output_tokens",
                        "global",
                        "day",
                        2 * min(1024, self.config.bedrock_max_tokens),
                        self.config.chat_model_output_tokens_per_day,
                    ),
                ],
            )

    async def http_request(self, request):
        control = active_control.get()
        if control is None:
            return
        try:
            async with self.writers.begin() as db:
                await self.reserve(
                    db,
                    [
                        ("http_calls", "global", "day", 1, self.config.chat_http_calls_per_day),
                        (
                            "http_calls",
                            str(control.claim.context.identity.run_id),
                            "day",
                            1,
                            self.config.chat_http_calls_per_run,
                        ),
                    ],
                )
        except StorageRejected:
            raise httpx.RequestError(
                "Chat provider request budget exhausted", request=request
            ) from None

    async def cleanup(self):
        async with self.writers.begin() as db:
            await db.execute(
                delete(ChatUsageBucket).where(ChatUsageBucket.expires_at <= self.clock())
            )


class BudgetedInterpreter:
    def __init__(self, interpreter, budgets):
        self.interpreter, self.budgets = interpreter, budgets

    async def interpret(self, request, context):
        try:
            await self.budgets.model(self.interpreter.input_limit_bytes)
        except StorageRejected:
            raise InterpretationFailure(
                "budget", "오늘의 AI 이용 한도에 도달했어요. 잠시 후 또는 내일 다시 이용해 주세요."
            ) from None
        return await self.interpreter.interpret(request, context)
