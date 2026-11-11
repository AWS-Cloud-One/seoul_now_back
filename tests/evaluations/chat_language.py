"""Opt-in, read-only model evaluation: python -m tests.evaluations.chat_language --live.

Default execution only lists synthetic cases. --live calls the configured model,
never C01/recommendations or the DB. Output contains case IDs, not credentials.
"""

import argparse
import asyncio
import json
import time
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import interpret
from app.modules.ai.providers.factory import open_ai_provider
from app.schemas.origin import OriginInterpretationContext
from app.schemas.pending import ChatRequest
from tests.fixtures.chat_language import BASE, CASES, SELECTED


def check(case, parsed):
    """Check effects, not wording; missing mandatory inputs are also guarded by C01."""
    failures = []
    patch = parsed.patch().model_dump(mode="json", exclude_unset=True)
    topic = case.get("topic")
    if topic:
        expected = topic if isinstance(topic, list) else [topic]
        topics = [item.topic for item in parsed.information_requests] or [parsed.intent]
        if set(topics) != set(expected):
            failures.append("information_topics")
        if parsed.recommendation_requested or patch or parsed.clarification_fields:
            failures.append("unexpected_condition_work")
        if case.get("target") == "unresolved" and not (
            parsed.target_ambiguous
            or any(item.target == "unresolved" for item in parsed.information_requests)
        ):
            failures.append("unresolved_target")
        if case.get("target") != "unresolved" and case.get("selected_place", SELECTED):
            if parsed.target_ambiguous or any(
                item.target != "selected" for item in parsed.information_requests
            ):
                failures.append("selected_target")
    else:
        if parsed.intent not in {"recommendation", "clarification"}:
            failures.append("recommendation_intent")
        # Explicitly saying cafe may produce cafe again or a no-op when already cafe.
        purposes = patch.pop("purposes", None)
        if purposes is not None and (
            len(purposes) != 1
            or purposes[0]["purpose"] != "cafe"
            or purposes[0].get("keyword") != case.get("keyword")
        ):
            failures.append("purpose")
        if (case["id"] in {"change_purpose", "missing_origin"} or case.get("keyword")) and (
            purposes is None
        ):
            failures.append("missing_purpose_change")
        if patch:
            failures.append("unrequested_condition_changes")
        allowed_questions = {"origin"} if case["id"] == "missing_origin" else set()
        if set(parsed.clarification_fields) - allowed_questions:
            failures.append("unnecessary_condition_question")
    if case["id"] == "change_origin":
        if parsed.origin_name != "강남역":
            failures.append("explicit_origin_change")
    elif parsed.origin_name or parsed.origin_hints:
        failures.append("invented_origin")
    if parsed.origin_reply:
        failures.append("invented_origin_selection")
    if parsed.headcount_hint or parsed.clear_fields or parsed.dialogue_action != "continue":
        failures.append("unrequested_dialogue_change")
    if parsed.request_relation != "independent" or parsed.resume_information:
        failures.append("unrequested_task_control")
    return failures


async def evaluate(cases, repeat):
    results = []
    semaphore = asyncio.Semaphore(2)
    async with open_ai_provider(Settings()) as provider:

        async def one(case, iteration):
            async with semaphore:
                request = ChatRequest(
                    request_id=uuid4(),
                    trip_id=uuid4(),
                    condition_version=0,
                    current_conditions=BASE | case.get("conditions", {}),
                    excluded_place_ids=[],
                    message=case["message"],
                    recent_messages=case.get("recent_messages", []),
                )
                origin = case.get("origin_confirmation")
                start = time.perf_counter()
                parsed = None
                try:
                    parsed = await interpret(
                        provider,
                        request,
                        selected_place=case.get("selected_place", SELECTED),
                        condition_dialogue=case.get("condition_dialogue"),
                        origin_confirmation=OriginInterpretationContext.model_validate(origin)
                        if origin
                        else None,
                    )
                    failures = check(case, parsed)
                except ServiceError as exc:
                    failures = [exc.code]
                result = {
                    "case": case["id"],
                    "iteration": iteration,
                    "failures": failures,
                    "seconds": round(time.perf_counter() - start, 2),
                }
                if failures and parsed is not None:
                    result["parsed"] = parsed.model_dump(mode="json", exclude_none=True)
                results.append(result)
                print(json.dumps(result), flush=True)

        # Repeat sequentially so repeated cases do not overlap their own execution.
        for iteration in range(1, repeat + 1):
            await asyncio.gather(*(one(case, iteration) for case in cases))
    failed = sum(bool(result["failures"]) for result in results)
    print(json.dumps({"total": len(results), "passed": len(results) - failed, "failed": failed}))
    return bool(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable paid model calls")
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--case", action="append", choices=[case["id"] for case in CASES])
    args = parser.parse_args()
    cases = [case for case in CASES if not args.case or case["id"] in args.case]
    if not args.live:
        print(json.dumps({"live": False, "cases": [case["id"] for case in cases]}))
        return 0
    return asyncio.run(evaluate(cases, args.repeat))


if __name__ == "__main__":
    raise SystemExit(main())
