"""One atomic business write, then writer-DB reconciliation before publication."""

import hashlib
import secrets
from uuid import uuid4

from app.models import ChatResult, ChatRunCommit
from app.modules.chat_v2.continuations import continuation_response
from app.modules.chat_v2.references import recommendation_references
from app.modules.chat_v2.region_contracts import RegionSearchScope
from app.modules.chat_v2.responses import DefaultResponseComposer
from app.modules.chat_v2.results import (
    CommitReceipt,
    FinalizationResult,
    PreparedRecommendation,
    StageError,
)
from app.modules.chat_v2.storage_contracts import StorageRejected
from app.repositories.recommendation_conditions import (
    ConditionVersionMismatch,
    RecommendationConditionsRepository,
)
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.recommendation_result import ContextResult
from app.schemas.recommendations import RecommendationResponse
from app.services.recommendation_context_payload import prepare_snapshot


def failed_finalization(prepared, *, code="COMMIT_FAILED"):
    unknown = code == "COMMIT_UNCERTAIN"
    message = {
        "COMMIT_FAILED": "저장을 완료하지 못했어요.",
        "COMMIT_UNCERTAIN": "변경의 실제 반영 여부를 확인해야 해요.",
        "CONFLICT": "현재 대화 조건을 다시 확인해 주세요.",
        "DEADLINE_EXCEEDED": "대화 또는 실행의 유효 시간이 지났어요.",
    }[code]
    return FinalizationResult(
        prepared=prepared,
        commit={
            "state": "unknown" if unknown else "not_applied",
            "error": StageError(code=code, safe_message=message, retryable=code == "COMMIT_FAILED"),
        },
    )


async def save_result(db, prepared, *, result_id, expires_at):
    """Use the existing validated historical-card repository, without issuing a V1 token."""
    plan = prepared.plan
    db.add(
        ChatResult(
            result_id=result_id,
            run_id=plan.context.identity.run_id,
            trip_id=plan.context.identity.trip_id,
            created_at=prepared.prepared_at,
        )
    )
    await db.flush()
    if (
        prepared.selection_mode == "regional"
        or plan.scope == "request_only"
        or isinstance(prepared.search_scope, RegionSearchScope)
    ):
        # No fake origin, route failure, or ETA in the legacy driving-only tables.
        # The same transaction persists cards/criteria in run.response and its receipt.
        return
    payload = ContextResult(
        result_id=result_id,
        created_at=prepared.prepared_at,
        conditions=plan.effective_conditions,
        context_token=None,
        context_expires_at=None,
        cards=prepared.cards,
        ranking_policy=prepared.ranking_policy,
        search_scope=prepared.search_scope,
    )
    response = RecommendationResponse(
        trip_id=plan.context.identity.trip_id,
        base_condition_version=plan.context.condition_version,
        kind="recommendation",
        message="추천 준비 결과",
        condition_update={
            "applied": True,
            "conditions": plan.effective_conditions,
            "version": plan.context.condition_version + bool(plan.changed_fields),
            "changes": [
                {
                    "field": key,
                    "before": plan.context.current_conditions.model_dump(mode="json")[key],
                    "after": plan.effective_conditions.model_dump(mode="json")[key],
                }
                for key in plan.changed_fields
            ],
        },
        stages={
            "routes": "partial"
            if any(w.code == "ROUTE_PARTIAL_FAILURE" for w in prepared.warnings)
            else "ok",
            "explanation": "fallback",
        },
        result=payload.model_dump(mode="json"),
        clarification=None,
        warnings=prepared.warnings,
    )
    # A random, unrecoverable legacy token digest satisfies existing storage.
    # V2 retrieval uses the conversation owner; no raw result token is published.
    snapshot = prepare_snapshot(
        response,
        plan.context.identity.request_id,
        token_digest=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        expires_at=expires_at,
    )
    await RecommendationContextRepository(db).save(snapshot)


class DatabaseFinalizer:
    def __init__(self, store, claim):
        self.store, self.claim = store, claim

    async def finalize(self, prepared):
        prepared = PreparedRecommendation.model_validate(prepared.model_dump())
        if prepared.plan.context != self.claim.context:
            raise ValueError("Prepared result belongs to another execution")
        try:
            # Persist the plan before attempting the business write. A lost process
            # leaves an occupied run; no other request may repeat it automatically.
            async with self.store.writers.begin() as db:
                conversation, run = await self.store.locked_claim(db, self.claim)
                if run.response is not None:
                    return self._stored(run, prepared)
                await self.store.check_current(db, conversation, run, self.claim)
                if (
                    run.prepared is not None
                    and PreparedRecommendation.model_validate(run.prepared) != prepared
                ):
                    raise StorageRejected("OWNER_CONFLICT")
                run.prepared = prepared.model_dump(mode="json")
                run.phase = "preparing_commit"
                run.stage = "saving"
                self.store.event(conversation, run, "stage_started", stage="saving")
            await self._commit(prepared)
        except (StorageRejected, ConditionVersionMismatch) as exc:
            code = (
                "DEADLINE_EXCEEDED"
                if getattr(exc, "code", None) == "DEADLINE_EXCEEDED"
                else "CONFLICT"
            )
            return await self.reconcile(prepared, failure_code=code)
        except Exception:
            # The writer may have committed before acknowledgement was lost.
            return await self.reconcile(prepared)
        # Even a successful return is projected from the saved receipt/response.
        return await self.reconcile(prepared)

    async def _commit(self, prepared):
        async with self.store.writers.begin() as db:
            conversation, run = await self.store.locked_claim(db, self.claim)
            if run.response is not None:
                self._stored(run, prepared)
                return
            await self.store.check_current(db, conversation, run, self.claim)
            if PreparedRecommendation.model_validate(run.prepared) != prepared:
                raise StorageRejected("OWNER_CONFLICT")
            plan = prepared.plan
            await RecommendationConditionsRepository(db).accept(
                conversation.trip_id,
                plan.context.condition_version,
                plan.context.current_conditions,
                getattr(plan, "persisted_conditions", plan.effective_conditions),
                expected_existing=True,
            )
            result_id = uuid4() if prepared.outcome == "recommendation" else None
            if result_id is not None:
                await save_result(
                    db, prepared, result_id=result_id, expires_at=conversation.expires_at
                )
            receipt = CommitReceipt(
                identity=plan.context.identity,
                previous_version=plan.context.condition_version,
                current_version=plan.context.condition_version + bool(plan.changed_fields),
                conditions=getattr(plan, "persisted_conditions", plan.effective_conditions),
                changed_fields=plan.changed_fields,
                result_id=result_id,
                committed_at=self.store.clock(),
            )
            db.add(
                ChatRunCommit(
                    run_id=receipt.identity.run_id,
                    trip_id=receipt.identity.trip_id,
                    previous_version=receipt.previous_version,
                    current_version=receipt.current_version,
                    result_id=result_id,
                    receipt=receipt.model_dump(mode="json"),
                    committed_at=receipt.committed_at,
                )
            )
            result = FinalizationResult(
                prepared=prepared, commit={"state": "committed", "receipt": receipt}
            )
            response = DefaultResponseComposer().compose(result)
            after = getattr(plan, "after", None)
            keep_active = False
            if after:
                refs = recommendation_references(response) if response.recommendation else []
                targets = [r for r in refs if r.position == after.position]
                if targets:
                    response = continuation_response(response, "pending")
                    run.operation = {
                        "schema_version": 1,
                        "kind": "lookup",
                        "topics": after.topics,
                        "targets": [t.model_dump(mode="json") for t in targets],
                        "origin": plan.effective_conditions.origin.model_dump(mode="json")
                        if plan.effective_conditions.origin
                        else None,
                        "items": [],
                        "comparison_metric": None,
                    }
                    keep_active = True
                else:
                    response = continuation_response(
                        response, "blocked", reason="DEPENDENCY_FAILED"
                    )
            self.store.finish_locked(conversation, run, response, keep_active=keep_active)
            await db.flush()

    @staticmethod
    def _stored(run, prepared):
        from app.modules.chat_v2.responses import ChatResponse

        response = ChatResponse.model_validate(run.response)
        if response.snapshot is None or response.snapshot.finalization.prepared != prepared:
            raise ValueError("Saved response does not match this finalization")
        return response.snapshot.finalization

    async def reconcile(self, prepared, *, failure_code="COMMIT_FAILED"):
        """Lock the same writer rows: absence AFTER locking proves the old write ended.

        Lock timeout/database failure leaves the execution unknown and occupied.
        This method never invokes a model, provider, or business write again.
        """
        try:
            prepared = PreparedRecommendation.model_validate(prepared.model_dump())
            if prepared.plan.context != self.claim.context:
                raise ValueError("Reconciliation belongs to another execution")
            async with self.store.writers.begin() as db:
                conversation, run = await self.store.locked_claim(db, self.claim)
                if (
                    run.prepared is not None
                    and PreparedRecommendation.model_validate(run.prepared) != prepared
                ):
                    raise ValueError("Reconciliation differs from the persisted preparation")
                receipt_row = await db.get(ChatRunCommit, self.claim.context.identity.run_id)
                if run.response is not None:
                    result = self._stored(run, prepared)
                    if result.commit.state == "committed":
                        if (
                            receipt_row is None
                            or CommitReceipt.model_validate(receipt_row.receipt)
                            != result.commit.receipt
                        ):
                            raise ValueError("Missing or inconsistent receipt")
                    elif receipt_row is not None:
                        raise ValueError("Receipt conflicts with failure")
                else:
                    if receipt_row is not None:
                        raise ValueError("Receipt without its atomic response")
                    result = failed_finalization(prepared, code=failure_code)
                    self.store.finish_locked(
                        conversation, run, DefaultResponseComposer().compose(result)
                    )
            return result
        except Exception:
            return failed_finalization(prepared, code="COMMIT_UNCERTAIN")
