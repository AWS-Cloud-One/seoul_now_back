"""Export public V2 JSON schemas without credentials, network or database access."""

import argparse
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from app.modules.chat_v2.contracts import RunRequest
from app.modules.chat_v2.http_contracts import (
    CancelRequest,
    ErrorEnvelope,
    RunPoll,
    SessionCreated,
    SubmitResult,
)
from app.modules.chat_v2.storage_contracts import ConversationSnapshot, ExecutionView, HistoryPage

MODELS = (
    RunRequest,
    CancelRequest,
    SessionCreated,
    ConversationSnapshot,
    ExecutionView,
    SubmitResult,
    RunPoll,
    HistoryPage,
    ErrorEnvelope,
)


def export():
    refs, schema = models_json_schema([(m, "validation") for m in MODELS])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Outside Dictionary Chat HTTP v2",
        **schema,
        "contracts": {m.__name__: refs[(m, "validation")] for m in MODELS},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("contracts/chat-v2/schema.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(export(), ensure_ascii=False, indent=2) + "\n")
