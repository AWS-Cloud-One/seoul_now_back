"""Verify public HTTPS, CORS and CDN cache isolation without invoking AI providers."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


def run(base, results):
    origin = "http://localhost:5173"
    query = {"bbox": "126.97,37.55,127.01,37.58", "level": 6}

    def request(case, path, params=None, *, headers=None, method="GET", payload=None):
        url = base.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        started = time.monotonic()
        request_headers = {"Origin": origin, **(headers or {})}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        try:
            response = urlopen(
                Request(url, headers=request_headers, method=method, data=data), timeout=30
            )
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            values = {key.lower(): value for key, value in response.headers.items()}
            raw = response.read()
        body = json.loads(raw) if raw.startswith(b"{") else None
        result = {
            "case": case,
            "status": status,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "bytes": len(raw),
            "headers": {
                key: values[key]
                for key in (
                    "x-cache",
                    "age",
                    "cache-control",
                    "access-control-allow-origin",
                    "access-control-expose-headers",
                    "x-amz-cf-pop",
                )
                if key in values
            },
        }
        if body and isinstance(body.get("data"), dict):
            collection = body["data"].get("feature_collection")
            if collection:
                result["features"] = len(collection["features"])
        results.append(result)
        print(json.dumps(result), flush=True)
        return status, values, body

    def uncached(case, path, params=None, *, expected=200, **kwargs):
        first = request(case + "_first", path, params, **kwargs)
        second = request(case + "_repeat", path, params, **kwargs)
        for status, headers, _ in (first, second):
            assert status == expected, case
            assert "Hit" not in headers.get("x-cache", ""), case
            assert "age" not in headers, case
        if first[2] and first[2].get("request_id"):
            assert first[2]["request_id"] != second[2]["request_id"], case
        return first

    for health in ("live", "ready"):
        status, headers, body = uncached("health_" + health, "/api/v1/health/" + health)
        assert body == {"status": "ok"}
        assert headers["access-control-allow-origin"] == origin
        assert "age" in headers["access-control-expose-headers"].lower()
        assert "retry-after" in headers["access-control-expose-headers"].lower()

    geometries = {}
    for layer in ("roads", "parking", "population"):
        path = "/api/v1/map/layers/" + layer + "/geometry"
        first = request(layer + "_geometry_first", path, query)
        time.sleep(1.1)
        second = request(layer + "_geometry_hit", path, query)
        assert first[0] == second[0] == 200, layer
        assert first[2] == second[2], layer
        assert "Hit" in second[1]["x-cache"], layer
        assert int(second[1]["age"]) >= 1, layer
        assert "s-maxage=300" in second[1]["cache-control"], layer
        data = second[2]["data"]
        assert data["feature_collection"]["features"], layer
        geometries[layer] = second[2]
        if layer != "parking":
            status_path = "/api/v1/map/layers/" + layer + "/status"
            state = uncached(
                layer + "_status",
                status_path,
                {**query, "geometry_version": data["geometry_version"]},
            )
            assert state[1]["cache-control"] == "no-store"
            assert state[2]["data"]["geometry_version"] == data["geometry_version"]

    path = "/api/v1/map/layers/roads/geometry"
    for label, changed in (
        ("bbox", {"bbox": "126.97213,37.55123,127.00234,37.57234"}),
        ("level", {"level": 7}),
    ):
        status, _, body = request("isolated_" + label, path, {**query, **changed})
        assert status == 200
        assert body["request_id"] != geometries["roads"]["request_id"]
        if label == "bbox":
            assert body["data"]["bbox"] == list(map(float, changed["bbox"].split(",")))
        else:
            assert body["data"]["level"] == 7

    for alternate in ("http://127.0.0.1:5173", "https://unapproved.invalid"):
        status, headers, body = request(
            "cors_separate_origin", path, query, headers={"Origin": alternate}
        )
        assert status == 200
        expected = alternate if alternate.startswith("http://127.0.0.1:") else None
        assert headers.get("access-control-allow-origin") == expected
        assert body["request_id"] != geometries["roads"]["request_id"]
        repeat = request("cors_separate_origin_hit", path, query, headers={"Origin": alternate})
        assert repeat[1].get("access-control-allow-origin") == expected
        assert repeat[2] == body and "Hit" in repeat[1]["x-cache"]

    refresh = uncached("forced_refresh", path, {**query, "cache_refresh": str(uuid4())})
    assert refresh[1]["cache-control"] == "no-store"
    invalid = uncached("invalid_geometry", path, {**query, "bbox": "invalid"}, expected=422)
    assert invalid[1]["cache-control"] == "no-store"
    mismatch = uncached(
        "geometry_version_mismatch",
        "/api/v1/map/layers/roads/status",
        {**query, "geometry_version": "edge-probe-old-version"},
        expected=409,
    )
    assert mismatch[2]["error"]["code"] == "GEOMETRY_VERSION_MISMATCH"
    assert request("geometry_after_error", path, query)[0] == 200

    for preflight_path, requested_method in (
        (path, "GET"),
        ("/api/v1/chat/messages", "POST"),
    ):
        status, headers, _ = request(
            "cors_preflight",
            preflight_path,
            method="OPTIONS",
            headers={
                "Access-Control-Request-Method": requested_method,
                "Access-Control-Request-Headers": "Content-Type,Result-Context",
            },
        )
        assert status == 200
        assert headers["access-control-allow-origin"] == origin
        assert "result-context" in headers["access-control-allow-headers"].lower()
        assert "Hit" not in headers["x-cache"]
    denied = request(
        "cors_denied_preflight",
        "/api/v1/chat/messages",
        method="OPTIONS",
        headers={"Origin": "https://unapproved.invalid", "Access-Control-Request-Method": "POST"},
    )
    assert denied[0] == 400 and "access-control-allow-origin" not in denied[1]
    # Invalid bodies stop at request validation and never call an AI provider.
    invalid_post = uncached(
        "invalid_chat_post",
        "/api/v1/chat/messages",
        method="POST",
        payload={},
        expected=422,
    )
    assert invalid_post[1]["cache-control"] == "no-store"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "https" or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        parser.error("base-url must be an HTTPS origin without an API path or query")
    report = {"checked_at": datetime.now(UTC).isoformat(), "base_url": args.base_url, "results": []}
    try:
        run(args.base_url, report["results"])
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
