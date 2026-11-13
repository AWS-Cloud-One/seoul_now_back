"""Opt-in P4 Bedrock interpretation samples; synthetic context, no database writes."""

import argparse
import asyncio
import json
from uuid import uuid4

from app.core.config import Settings
from app.modules.chat_v2.bedrock import open_bedrock_interpreter
from app.modules.chat_v2.dialogue_context import RecommendationFlow
from app.modules.chat_v2.interpretation import InterpretationFailure
from tests.smoke.chat_v2_dialogue import cases as dialogue_cases


def cases():
    _, request, context, _ = next(c for c in dialogue_cases() if c[0] == "current_facts")
    first = context.references[0]
    context.references.append(
        first.model_copy(
            update={
                "ref_id": first.ref_id + "-2",
                "position": 2,
                "place": first.place.model_copy(
                    update={"place_id": "kakao:sample2", "name": "검증 카페2"}
                ),
            }
        )
    )
    for name, text in [
        ("temporary", "이번만 20분 이내 카페 추천해줘. 원래 여행 조건은 유지해"),
        ("same_flow", "이번 추천과 같은 조건으로 다른 곳 추천해줘"),
        ("compare", "첫 번째와 두 번째 중 지금 출발지에서 차로 덜 걸리는 곳은?"),
        (
            "fallback_after",
            "등록 주차장이 500m 안에 있는 카페 추천해줘. "
            "없으면 주차 조건은 빼고 추천하고 새 추천 첫 장소 주차도 알려줘",
        ),
        (
            "guard",
            "500m 안에 등록 주차장이 있는 카페 후보가 있을 때만 인원을 3명으로 바꾸고 추천해줘",
        ),
    ]:
        current = request.model_copy(deep=True)
        current.input.text = text
        state = context.model_copy(deep=True)
        if name == "same_flow":
            state.active_flow = RecommendationFlow(
                flow_id=uuid4(),
                result_id=uuid4(),
                conditions=state.current_conditions,
            )
        yield name, current, state


def valid(name, decision):
    if name == "compare":
        return (
            decision.kind == "compare"
            and decision.metric == "duration_seconds"
            and decision.origin_query is None
            and {t.position for t in decision.targets} == {1, 2}
        )
    if decision.kind != "recommend":
        return False
    if decision.origin_query or decision.search_area_query:
        return False
    if name != "guard" and decision.headcount is not None:
        return False
    if name != "temporary" and decision.max_travel_seconds is not None:
        return False
    if any(p.keyword for p in decision.purposes or []):
        return False
    if name != "guard" and decision.commit_guard is not None:
        return False
    if name == "temporary":
        return decision.scope == "request_only" and decision.max_travel_seconds == 1200
    if name == "same_flow":
        return decision.use_previous
    parking = decision.constraints and any(
        a.kind == "registered_parking_exists" and a.radius_m == 500
        for a in decision.constraints.atoms()
    )
    if name == "guard":
        return (
            parking
            and decision.headcount == 3
            and decision.commit_guard
            and any(a.outcome == "has_verified" for a in decision.commit_guard.atoms())
        )
    return (
        parking
        and decision.alternative
        and decision.alternative.omit_parking
        and decision.alternative.purposes is None
        and any(a.outcome == "no_matches" for a in decision.alternative.when.atoms())
        and decision.after
        and decision.after.position == 1
        and "parking" in decision.after.topics
    )


async def run(args):
    traces, passed, total = [], 0, 0
    async with open_bedrock_interpreter(
        Settings(_env_file=None, ai_provider="bedrock"),
        profile_name=args.profile,
        mode="dialogue",
        observer=traces.append,
    ) as interpreter:
        for name, request, context in cases():
            if args.case and name not in args.case:
                continue
            total += 1
            try:
                decision = (await interpreter.interpret(request, context)).decision
            except InterpretationFailure as exc:
                print(json.dumps({"case": name, "passed": False, "reason": exc.reason}), flush=True)
                continue
            ok = bool(valid(name, decision)) and decision.evidence_text in request.input.text
            passed += int(ok)
            print(
                json.dumps(
                    {"case": name, "passed": ok, "decision": decision.model_dump(mode="json")},
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
    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--case", action="append", choices=[c[0] for c in cases()])
    raise SystemExit(asyncio.run(run(parser.parse_args())))
