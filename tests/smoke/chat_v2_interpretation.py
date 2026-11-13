"""Opt-in paid Bedrock smoke using synthetic context; never calls a DB or finalizer.

Run explicitly: python -m tests.smoke.chat_v2_interpretation --run-live --profile seoul_now
This module is not collected by pytest and is not part of CI.
"""

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from app.core.config import Settings
from app.modules.chat_v2.bedrock import open_bedrock_interpreter
from app.modules.chat_v2.contracts import ValidatedPlan
from app.modules.chat_v2.interpretation import InterpretationFailure
from app.modules.chat_v2.validation import HeadcountPlanValidator, StageFailure
from tests.fixtures.chat_v2 import scenario


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    pending: bool
    expected_kind: str
    headcount: int | None = None
    scope: str | None = None


CASES = [
    Case("short_reply", "셋이요", True, "change_headcount", 3, "persistent"),
    Case(
        "explicit_change",
        "우리 인원을 네 명으로 바꿔 줘",
        False,
        "change_headcount",
        4,
        "persistent",
    ),
    Case("ambiguous_number", "3", False, "needs_input"),
    Case("vague_change", "인원 좀 더 늘려줘", False, "needs_input"),
    Case("hypothetical", "3명이면 갈 수 있어?", False, "unsupported"),
    Case("temporary", "이번만 다섯 명으로 해줘", False, "change_headcount", 5, "request_only"),
    Case("range", "6명 이상이야", True, "needs_input"),
    Case("correction", "3명 말고 4명으로 바꿔줘", False, "change_headcount", 4, "persistent"),
]


async def run(args):
    settings = Settings(_env_file=None, ai_provider="bedrock")
    traces = []
    selected = [case for case in CASES if not args.case or case.name in args.case]
    print(
        json.dumps(
            {
                "mode": "live_model_only",
                "model": settings.bedrock_model_id,
                "cases": len(selected),
                "max_model_calls": len(selected) * 2,
                "db_writes": False,
            }
        ),
        flush=True,
    )
    passed = 0
    async with open_bedrock_interpreter(
        settings, profile_name=args.profile, observer=traces.append
    ) as interpreter:
        for case in selected:
            request, _, prepared, _ = scenario()
            context = prepared.plan.context
            request.input.text = case.text
            if not case.pending:
                request.input.reply_to_clarification_id = None
                context.pending_question = None
            start = len(traces)
            try:
                proposal = await interpreter.interpret(request, context)
                decision = proposal.decision
                validated = await HeadcountPlanValidator().validate(request, context, proposal)
                matches = (
                    decision.kind == case.expected_kind
                    and getattr(decision, "headcount", None) == case.headcount
                    and getattr(decision, "scope", None) == case.scope
                    and (
                        isinstance(validated, ValidatedPlan)
                        if case.scope == "persistent"
                        else getattr(validated, "kind", None)
                        == ("unsupported" if case.scope == "request_only" else case.expected_kind)
                    )
                )
                passed += int(matches)
                print(
                    json.dumps(
                        {
                            "case": case.name,
                            "input": case.text,
                            "passed": matches,
                            "proposal": proposal.model_dump(),
                            "server_result": "validated_plan"
                            if isinstance(validated, ValidatedPlan)
                            else validated.kind,
                            "calls": [asdict(t) for t in traces[start:]],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except StageFailure as exc:
                print(
                    json.dumps(
                        {
                            "case": case.name,
                            "passed": False,
                            "error": exc.error.model_dump(),
                            "reason": exc.reason
                            if isinstance(exc, InterpretationFailure)
                            else "validation",
                            "calls": [asdict(t) for t in traces[start:]],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return 1
    print(
        json.dumps({"passed": passed, "total": len(selected), "model_calls": len(traces)}),
        flush=True,
    )
    return 0 if passed == len(selected) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-live", action="store_true", help="Explicitly enable paid model calls"
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument("--case", action="append", choices=[case.name for case in CASES])
    args = parser.parse_args()
    if not args.run_live:
        parser.error("--run-live is required; this command invokes Bedrock")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
