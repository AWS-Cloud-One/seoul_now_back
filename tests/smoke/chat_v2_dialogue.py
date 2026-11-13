"""Opt-in synthetic P2 Bedrock interpretations; no DB or external place APIs."""

import argparse
import asyncio
import json
from uuid import uuid4

from app.core.config import Settings
from app.modules.chat_v2.bedrock import open_bedrock_interpreter
from app.modules.chat_v2.contracts import PendingHeadcountQuestion
from app.modules.chat_v2.dialogue_context import OriginOption, PendingRead, RecommendationDraft
from app.modules.chat_v2.references import recommendation_references
from app.modules.chat_v2.responses import DefaultResponseComposer
from app.modules.chat_v2.results import FinalizationResult
from app.schemas.recommendations import Conditions
from tests.fixtures.chat_v2 import scenario


def cases():
    request, _, prepared, receipt = scenario()
    base = prepared.plan.context
    base.pending_question = None
    request.input.reply_to_clarification_id = None
    stored = DefaultResponseComposer().compose(
        FinalizationResult(prepared=prepared, commit={"state": "committed", "receipt": receipt})
    )
    refs = recommendation_references(stored)
    for name, text, kind in [
        ("first_recommendation", "서울역에서 차로 30분 이내 카페 추천해줘", "recommend"),
        ("regional_only", "성수 카페 추천해줘", "recommend"),
        ("separate_areas", "수원역에서 출발해서 성수 카페 추천해줘", "recommend"),
        ("driving_origin_reply", "서울역", "read"),
        ("confirm_lookup_origin", "맞아 거기에서 출발", "select_origin"),
        ("only_explicit_changes", "20분 이내로 추천해줘", "recommend"),
        ("place_search", "카페 어니언 성수점 주소 찾아줘", "search_places"),
        ("current_facts", "첫 번째 장소 주차랑 지금 얼마나 걸리는지 알려줘", "read"),
        ("historical_reason", "아까 추천한 그곳은 왜 추천했어?", "read"),
        ("confirm_origin", "맞아 거기에서 출발", "select_origin"),
        ("resume_headcount", "셋이요", "change_headcount"),
    ]:
        context = base.model_copy(deep=True)
        current = request.model_copy(deep=True)
        current.input.text = text
        if name in {"first_recommendation", "regional_only", "separate_areas"}:
            context.current_conditions = Conditions(
                origin=None,
                purposes=[],
                headcount=None,
                max_travel_seconds=None,
                group_type=None,
                age_groups=[],
            )
        if kind == "read":
            context.references = refs
            context.focus_ref = refs[0].ref_id
        if kind == "select_origin":
            context.draft = RecommendationDraft(
                base_condition_version=context.condition_version,
                conditions=context.current_conditions,
                origin_options=[
                    OriginOption(
                        option_id=str(uuid4()),
                        position=1,
                        location=context.current_conditions.origin,
                    )
                ],
            )
            context.pending_question = PendingHeadcountQuestion(
                **{
                    "clarification_id": "origin-question",
                    "scope": "persistent",
                    "field": "origin",
                    "question": "서울역에서 출발하시나요?",
                }
            )
        if kind == "change_headcount":
            context.pending_question = PendingHeadcountQuestion(
                **{
                    "clarification_id": "headcount-question",
                    "scope": "persistent",
                    "field": "headcount",
                    "question": "몇 명인가요?",
                }
            )
            context.references = refs
            context.focus_ref = refs[0].ref_id
        if name in {"driving_origin_reply", "confirm_lookup_origin"}:
            context.current_conditions.origin = None
            context.draft = None
            context.pending_question = None
            context.references = refs
            context.focus_ref = refs[0].ref_id
            context.pending_read = PendingRead(
                clarification_id="lookup-origin",
                topics=["driving"],
                target_refs=[refs[0].ref_id],
                question="이 장소까지 이동시간을 확인할 출발지를 알려주세요.",
                origin_options=[
                    OriginOption(
                        option_id="synthetic", position=1, location=base.current_conditions.origin
                    )
                ]
                if name == "confirm_lookup_origin"
                else [],
            )
        yield name, current, type(context).model_validate(context.model_dump()), kind


async def run(args):
    settings = Settings(_env_file=None, ai_provider="bedrock")
    traces, passed, total = [], 0, 0
    async with open_bedrock_interpreter(
        settings, profile_name=args.profile, observer=traces.append, mode="dialogue"
    ) as interpreter:
        for name, request, context, kind in cases():
            if args.case and name not in args.case:
                continue
            total += 1
            proposal = await interpreter.interpret(request, context)
            decision = proposal.decision
            valid = decision.kind == kind
            if valid and name == "first_recommendation":
                valid &= (
                    decision.origin_query == "서울역"
                    and decision.max_travel_seconds == 1800
                    and [p.purpose for p in decision.purposes or []] == ["cafe"]
                    and all(p.keyword is None for p in decision.purposes or [])
                    and decision.headcount is None
                )
            elif valid and name in {"regional_only", "separate_areas"}:
                valid &= (
                    decision.search_area_query == "성수"
                    and decision.origin_query == ("수원역" if name == "separate_areas" else None)
                    and [p.purpose for p in decision.purposes or []] == ["cafe"]
                    and decision.max_travel_seconds is None
                    and decision.headcount is None
                )
            elif valid and name == "driving_origin_reply":
                valid &= decision.origin_query == "서울역" and decision.topics == ["driving"]
            elif valid and name == "confirm_lookup_origin":
                valid &= decision.for_lookup and decision.position == 1
            elif valid and name == "only_explicit_changes":
                valid &= (
                    decision.max_travel_seconds == 1200
                    and decision.origin_query is None
                    and decision.purposes is None
                    and decision.headcount is None
                )
            elif valid and name == "current_facts":
                valid &= (
                    set(decision.topics) == {"parking", "driving"}
                    and decision.targets[0].position == 1
                )
            elif valid and name == "historical_reason":
                valid &= decision.topics == ["recommendation_reason"]
            elif valid and kind == "change_headcount":
                valid &= decision.headcount == 3 and decision.scope == "persistent"
            elif valid and kind == "select_origin":
                valid &= decision.position == 1
            if hasattr(decision, "evidence_text"):
                valid &= decision.evidence_text in request.input.text
            passed += int(valid)
            print(
                json.dumps(
                    {"case": name, "passed": valid, "proposal": proposal.model_dump(mode="json")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(
        json.dumps(
            {
                "passed": passed,
                "total": total,
                "calls": len(traces),
                "input_tokens": sum(t.input_tokens or 0 for t in traces),
                "output_tokens": sum(t.output_tokens or 0 for t in traces),
                "elapsed_ms": [t.elapsed_ms for t in traces],
            }
        ),
        flush=True,
    )
    return 0 if passed == total and total else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--case", action="append", choices=[c[0] for c in cases()])
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
