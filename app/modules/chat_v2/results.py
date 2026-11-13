"""Separate preparation, confirmed persistence and unknown persistence outcomes.

Receipts are assertions supplied by the database adapter, not proof created
by Pydantic. No helper in this module manufactures a receipt from prepared data.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.modules.chat_v2.contracts import (
    ConditionFields,
    Nonblank,
    RecommendationPlan,
    RunIdentity,
    ValidatedPlan,
    Version,
)
from app.modules.chat_v2.predicates import EvaluationSummary
from app.modules.chat_v2.region_contracts import RegionSearchScope
from app.schemas.common import ContractModel
from app.schemas.recommendation_result import ContextCard, RankingPolicySnapshot, SearchScope
from app.schemas.recommendations import Conditions, WarningMessage


class StageError(ContractModel):
    code: Literal[
        "INTERPRETATION_FAILED",
        "VALIDATION_FAILED",
        "INTERNAL_ERROR",
        "RECOMMENDATION_FAILED",
        "COMMIT_FAILED",
        "COMMIT_UNCERTAIN",
        "CONFLICT",
        "CANCELLED",
        "INTERRUPTED",
        "DEADLINE_EXCEEDED",
        "UNSUPPORTED",
    ]
    safe_message: Nonblank
    retryable: bool = Field(strict=True)

    @model_validator(mode="after")
    def reconcile_before_retry(self):
        if self.code == "COMMIT_UNCERTAIN" and self.retryable:
            raise ValueError("An uncertain commit must be reconciled before retrying")
        return self


class PreparedRecommendation(ContractModel):
    """Compute-only output. There is intentionally no result_id or applied flag."""

    phase: Literal["prepared"] = "prepared"
    plan: Annotated[ValidatedPlan | RecommendationPlan, Field(discriminator="feature_id")]
    outcome: Literal["recommendation", "no_candidates", "insufficient_evidence", "error"]
    selection_mode: Literal["driving", "regional"] = "driving"
    cards: list[ContextCard] = Field(default_factory=list, max_length=3)
    prepared_at: AwareDatetime
    error: StageError | None = None
    warnings: list[WarningMessage] = Field(default_factory=list)
    ranking_policy: RankingPolicySnapshot | None = None
    search_scope: SearchScope | RegionSearchScope | None = None
    evaluation: EvaluationSummary | None = None

    @model_validator(mode="after")
    def consistent_preparation(self):
        if not self.plan.requires_recommendation:
            raise ValueError("An unchanged headcount alone must skip recommendation preparation")
        if (self.outcome == "recommendation") != bool(self.cards):
            raise ValueError("Only a recommendation has prepared cards")
        if (self.outcome == "error") != (self.error is not None):
            raise ValueError("A preparation error needs a separate error outcome")
        if self.error is not None and self.error.code != "RECOMMENDATION_FAILED":
            raise ValueError("Preparation cannot report a persistence outcome")
        place_ids = []
        for rank, card in enumerate(self.cards, start=1):
            if card.rank != rank or card.place.status != "available" or card.place.value is None:
                raise ValueError(
                    "Prepared recommendations require ordered places and actual routes"
                )
            if self.selection_mode == "driving":
                if card.route.status != "available" or card.route.value is None:
                    raise ValueError("Driving recommendations require verified routes")
            elif (
                self.plan.effective_conditions.origin is not None
                or self.plan.effective_conditions.max_travel_seconds is not None
                or card.route.value is not None
                or card.route.status != "unavailable"
                or card.route.reason_code != "ORIGIN_NOT_SET"
                or card.ranking is not None
                or self.ranking_policy is not None
                or self.search_scope is None
                or (isinstance(self.search_scope, SearchScope) and not self.search_scope.areas)
                or any(e.type == "eta" for e in card.reason.evidence)
            ):
                raise ValueError("Regional recommendations cannot invent routes or driving ranks")
            place_ids.append(card.place.value.place_id)
        if len(place_ids) != len(set(place_ids)):
            raise ValueError("Prepared recommendation places must be unique")
        return self


class CommitReceipt(ContractModel):
    """Only the storage adapter may return this after verifying the actual commit."""

    phase: Literal["committed"] = "committed"
    identity: RunIdentity
    previous_version: Version
    current_version: Version
    conditions: Conditions
    changed_fields: ConditionFields
    result_id: UUID | None
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def matching_version(self):
        if self.current_version != self.previous_version + bool(self.changed_fields):
            raise ValueError("One condition change increments the version exactly once")
        return self


class ConfirmedCommit(ContractModel):
    state: Literal["committed"]
    receipt: CommitReceipt


class UnappliedCommit(ContractModel):
    state: Literal["not_applied"]
    error: StageError

    @model_validator(mode="after")
    def confirmed_failure(self):
        if self.error.code not in {
            "COMMIT_FAILED",
            "CONFLICT",
            "CANCELLED",
            "DEADLINE_EXCEEDED",
        }:
            raise ValueError(
                "Unapplied means a confirmed failure, conflict, cancellation or deadline"
            )
        return self


class UnknownCommit(ContractModel):
    state: Literal["unknown"]
    error: StageError

    @model_validator(mode="after")
    def uncertain_only(self):
        if self.error.code != "COMMIT_UNCERTAIN":
            raise ValueError("Unknown persistence must explicitly report COMMIT_UNCERTAIN")
        return self


class FinalizationResult(ContractModel):
    prepared: PreparedRecommendation
    commit: Annotated[
        ConfirmedCommit | UnappliedCommit | UnknownCommit, Field(discriminator="state")
    ]

    @model_validator(mode="after")
    def receipt_matches_plan(self):
        if not isinstance(self.commit, ConfirmedCommit):
            return self
        receipt = self.commit.receipt
        plan = self.prepared.plan
        if (
            receipt.identity != plan.context.identity
            or receipt.previous_version != plan.context.condition_version
            or receipt.conditions
            != getattr(plan, "persisted_conditions", plan.effective_conditions)
            or receipt.changed_fields != plan.changed_fields
        ):
            raise ValueError("The receipt must describe this run and its exact planned change")
        if (receipt.result_id is not None) != (self.prepared.outcome == "recommendation"):
            raise ValueError("Only a confirmed recommendation may publish a result ID")
        if receipt.committed_at < self.prepared.prepared_at:
            raise ValueError("A commit cannot predate its preparation")
        return self


class RunSnapshot(ContractModel):
    """Internal final F02 hand-off, not the complete public V2 response schema.

    The future response adapter projects a verified finalization into wire fields.
    Questions, accepted/running events and the other V2 features remain separate work.
    """

    schema_version: Literal[1] = 1
    status: Literal["succeeded", "partial", "failed", "cancelled", "reconciling"]
    finalization: FinalizationResult

    @model_validator(mode="after")
    def status_follows_storage(self):
        result = self.finalization
        if isinstance(result.commit, UnknownCommit):
            expected = "reconciling"
        elif isinstance(result.commit, UnappliedCommit):
            expected = "cancelled" if result.commit.error.code == "CANCELLED" else "failed"
        elif result.prepared.outcome in {"error", "insufficient_evidence"}:
            expected = "partial"
        else:
            expected = "succeeded"
        if self.status != expected:
            raise ValueError(f"This finalization requires status={expected}")
        return self
