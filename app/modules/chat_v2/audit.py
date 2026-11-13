"""Allowlisted JSON diagnostics for container logs; no prompts or response bodies."""

import json
import logging
from contextlib import contextmanager


class AuditFormatter(logging.Formatter):
    def format(self, record):
        item = {"event": record.msg, "level": record.levelname}
        if run_id := getattr(record, "run_id", None):
            item["run_id"] = run_id
        trace = getattr(record, "interpretation", {})
        for key in (
            "run_id",
            "request_id",
            "model_id",
            "prompt_version",
            "attempt",
            "outcome",
            "elapsed_ms",
            "input_tokens",
            "output_tokens",
            "provider_request_id",
        ):
            if key in trace:
                item[key] = trace[key]
        return json.dumps(item)


@contextmanager
def audit_logging():
    logger = logging.getLogger("app.modules.chat_v2")
    level, propagate = logger.level, logger.propagate
    handler = logging.StreamHandler()
    handler.setFormatter(AuditFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(level)
        logger.propagate = propagate
