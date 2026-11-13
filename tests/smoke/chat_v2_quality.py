"""Small opt-in P6 paraphrase evaluation; synthetic context, no service DB writes."""

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import Settings
from app.modules.chat_v2.bedrock import open_bedrock_interpreter
from app.modules.chat_v2.interpretation import InterpretationFailure
from tests.smoke.chat_v2_compound import cases as compound_cases
from tests.smoke.chat_v2_compound import valid as compound_valid
from tests.smoke.chat_v2_dialogue import cases as dialogue_cases


def cases():
    dialogue = {name: (request, context) for name, request, context, _ in dialogue_cases()}
    phrases = [
        ("regional_only", "이번엔 홍대 쪽에서 커피 마실 데 좀 골라줄래?", "F01"),
        ("resume_headcount", "3명", "F02"),
        ("only_explicit_changes", "차로 이십 분 안에 닿는 곳이면 좋겠어", "F02"),
        ("current_facts", "맨 앞 장소 주변 주차랑 차로 걸리는 시간 같이 볼래", "F04"),
        ("historical_reason", "전에 보여준 1번은 어떤 기준으로 골랐어?", "F05"),
        ("place_search", "어니언 성수점 어디에 있어?", "F09"),
    ]
    for name, phrase, feature in phrases:
        req, ctx = dialogue[name]
        req.input.text = phrase
        yield name, feature, req, ctx
    phrases = {
        "temporary": "기본 조건은 그대로 두고 이번에는 차로 20분 안쪽 카페만 보여줘",
        "same_flow": "방금 그 조건 유지해서 아직 안 나온 곳 보여줘",
        "compare": "첫 번째랑 두 번째 중 지금 출발지에서 더 빨리 닿는 데는?",
        "fallback_after": (
            "등록 주차장이 직선 500m 안에 있는 카페를 골라줘. "
            "그런 곳이 없으면 주차 조건은 빼도 돼. 새로 고른 첫 장소 주변 주차도 보고 싶어"
        ),
        "guard": "등록 주차장이 직선 500m 안에 있는 카페가 나올 때만 3명으로 바꿔서 추천해",
    }
    for name, req, ctx in compound_cases():
        req.input.text = phrases[name]
        yield (
            name,
            {
                "temporary": "F03",
                "same_flow": "F03",
                "compare": "F06",
                "fallback_after": "F07/F08",
                "guard": "F07",
            }[name],
            req,
            ctx,
        )
    req, ctx = dialogue["regional_only"]
    req = req.model_copy(deep=True)
    req.input.text = "식당 자리 예약하고 카드로 결제해줘"
    yield "unsupported", "F10", req, ctx


def valid(name, d):
    if name in {"temporary", "same_flow", "compare", "fallback_after", "guard"}:
        return compound_valid(name, d)
    if name == "regional_only":
        return (
            d.kind == "recommend"
            and d.search_area_query
            and "홍대" in d.search_area_query
            and d.origin_query is None
            and any(p.purpose == "cafe" for p in d.purposes or [])
        )
    if name == "resume_headcount":
        return d.kind == "change_headcount" and d.headcount == 3
    if name == "only_explicit_changes":
        return (
            d.kind == "recommend"
            and d.max_travel_seconds == 1200
            and d.origin_query is None
            and d.headcount is None
            and d.purposes is None
        )
    if name == "current_facts":
        return (
            d.kind == "read"
            and set(d.topics) == {"parking", "driving"}
            and d.targets[0].position == 1
        )
    if name == "historical_reason":
        return d.kind == "read" and d.topics == ["recommendation_reason"]
    if name == "place_search":
        return d.kind == "search_places" and "어니언" in d.query
    return d.kind == "unsupported"


async def run(args):
    traces, results = [], []
    settings = Settings(_env_file=None, ai_provider="bedrock", bedrock_model_id=args.model)
    async with open_bedrock_interpreter(
        settings, profile_name=args.profile, mode="dialogue", observer=traces.append
    ) as interpreter:
        for name, feature, req, ctx in cases():
            if args.case and name not in args.case:
                continue
            start = len(traces)
            try:
                d = (await interpreter.interpret(req, ctx)).decision
                result = {
                    "case": name,
                    "feature": feature,
                    "passed": bool(valid(name, d)),
                    "decision": d.model_dump(mode="json"),
                }
            except InterpretationFailure as exc:
                result = {"case": name, "feature": feature, "passed": False, "failure": exc.reason}
            calls = traces[start:]
            result.update(
                calls=len(calls),
                elapsed_ms=sum(t.elapsed_ms for t in calls),
                input_tokens=sum(t.input_tokens or 0 for t in calls),
                output_tokens=sum(t.output_tokens or 0 for t in calls),
                usage_complete=all(
                    t.input_tokens is not None and t.output_tokens is not None for t in calls
                ),
            )
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    report = {
        "model": args.model,
        "prompt": traces[-1].prompt_version if traces else None,
        "cases": results,
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "calls": len(traces),
        "input_tokens": sum(t.input_tokens or 0 for t in traces),
        "output_tokens": sum(t.output_tokens or 0 for t in traces),
        "usage_complete": all(
            t.input_tokens is not None and t.output_tokens is not None for t in traces
        ),
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-live", action="store_true", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--profile")
    p.add_argument("--case", action="append")
    p.add_argument("--output", type=Path)
    raise SystemExit(asyncio.run(run(p.parse_args())))
