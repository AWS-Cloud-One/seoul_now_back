"""Origin search facts and selection contracts; these values do not authorize a choice."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.schemas.common import ContractModel
from app.schemas.location import Location

OriginText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CandidateId = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^\S+$")]
OriginToken = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
HintKind = Literal["region", "address", "station_line", "branch", "place_type"]


class OriginHint(ContractModel):
    """A literal user-origin clue, with a source that the interpreter can verify."""

    kind: HintKind
    text: OriginText
    source: Literal["message", "recent_user_message", "current_origin"]
    quote: str = Field(min_length=1, max_length=500)
    message_index: int | None = Field(default=None, strict=True, ge=0, le=9)

    @model_validator(mode="after")
    def source_fields(self):
        if (self.source == "recent_user_message") != (self.message_index is not None):
            raise ValueError("이전 사용자 발언의 단서에만 message_index가 필요합니다.")
        if self.text not in self.quote:
            raise ValueError("출발지 단서는 인용한 원문에 포함되어야 합니다.")
        return self


class OriginMatch(ContractModel):
    """Semantic evidence, separate from provider order; no fabricated confidence score."""

    name: Literal["exact", "normalized", "partial", "unknown"] = "unknown"
    place_type: Literal["match", "unknown", "conflict"] = "unknown"
    explicit_matches: list[HintKind] = Field(default_factory=list, max_length=5)
    context_matches: list[HintKind] = Field(default_factory=list, max_length=5)
    conflicts: list[HintKind] = Field(default_factory=list, max_length=5)


class OriginCandidate(ContractModel):
    """Internal search candidate: keep the provider's identity, category and order."""

    candidate_id: CandidateId
    source: Literal["kakao_keyword", "kakao_address"]
    provider_id: OriginText | None = None
    location: Location
    category_code: OriginText | None = None
    category_name: OriginText | None = None
    land_address: OriginText | None = None
    road_address: OriginText | None = None
    region_addresses: list[OriginText] = Field(default_factory=list, max_length=2)
    provider_order: int = Field(strict=True, ge=0)
    match: OriginMatch = Field(default_factory=OriginMatch)


class OriginChoiceValue(ContractModel):
    """Display data only. Resolve selections from the server's stored candidate."""

    candidate_id: CandidateId
    location: Location
    match_reasons: list[
        Literal[
            "name_exact",
            "name_normalized",
            "place_type",
            "explicit_region",
            "explicit_address",
            "station_line",
            "branch",
            "origin_context",
        ]
    ] = Field(default_factory=list, max_length=8)


class OriginClarificationContext(ContractModel):
    """A separate, expiring context from recommendation-result context tokens."""

    context_token: OriginToken
    mode: Literal["confirm", "choose", "refine", "confirmed"]
    preferred_candidate_id: CandidateId | None = None
    expires_at: AwareDatetime
    search_limited: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def preferred_candidate(self):
        if (self.mode == "confirm") != (self.preferred_candidate_id is not None):
            raise ValueError("confirm 모드에만 우선 확인할 후보가 필요합니다.")
        return self


class OriginAction(ContractModel):
    action: Literal["select", "show_alternatives", "restart"]
    context_token: OriginToken
    candidate_id: CandidateId | None = None

    @model_validator(mode="after")
    def selected_candidate(self):
        if (self.action == "select") != (self.candidate_id is not None):
            raise ValueError("select 동작에만 candidate_id가 필요합니다.")
        return self


class OriginReply(ContractModel):
    """Natural-language reply only; candidate numbers are checked against a stored question."""

    action: Literal["confirm", "select", "show_alternatives", "restart"]
    candidate_index: int | None = Field(default=None, strict=True, ge=1, le=5)

    @model_validator(mode="after")
    def selected_index(self):
        if (self.action == "select") != (self.candidate_index is not None):
            raise ValueError("select 응답에만 1~5의 후보 번호가 필요합니다.")
        return self


class OriginDisplayCandidate(ContractModel):
    name: OriginText
    address: OriginText | None


class OriginInterpretationContext(ContractModel):
    """Server-verified display context for AI; deliberately no IDs, tokens or coordinates."""

    mode: Literal["confirm", "choose", "refine", "confirmed"]
    candidates: list[OriginDisplayCandidate] = Field(max_length=5)

    @model_validator(mode="after")
    def matching_candidates(self):
        count = len(self.candidates)
        if (self.mode == "refine" and count) or (self.mode == "confirm" and not count):
            raise ValueError("확인 화면과 후보 개수가 맞지 않습니다.")
        if self.mode == "choose" and count < 2:
            raise ValueError("후보 비교에는 두 개 이상의 후보가 필요합니다.")
        if self.mode == "confirmed" and count != 1:
            raise ValueError("확인된 출발지 표시에는 선택한 장소 하나만 필요합니다.")
        return self
