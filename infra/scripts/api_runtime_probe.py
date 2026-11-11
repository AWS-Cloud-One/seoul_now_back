"""Read-only HTTP checks against an API Service; no AWS or DB credentials needed."""

import argparse
import json
import sys
from collections import Counter
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def emit(**values):
    print(json.dumps({"check": "api_runtime_probe", **values}), flush=True)


def request(base, path, query, expected=200, *, headers=None, method="GET"):
    url = base.rstrip("/") + path + "?" + urlencode(query)
    try:
        response = urlopen(Request(url, headers=headers or {}, method=method), timeout=15)
    except HTTPError as error:
        response = error
    with response:
        status = response.status
        response_headers = response.headers
        body = response.read()
    if status != expected:
        emit(status="failed", path=path, http_status=status, expected=expected)
        raise RuntimeError("unexpected_http_status")
    return response_headers, json.loads(body) if body.startswith(b"{") else None


def run(base, probe_id):
    marker = {"probe_id": probe_id}
    for path in ("/api/v1/health/live", "/api/v1/health/ready"):
        _, body = request(base, path, marker)
        assert body == {"status": "ok"}
        emit(status="pass", path=path, http_status=200)
    for path in ("/docs", "/openapi.json"):
        request(base, path, marker, expected=404)
        emit(status="pass", path=path, http_status=404)
    query = {**marker, "bbox": "126.97,37.55,127.01,37.58", "level": 6}
    for layer in ("parking", "population", "roads"):
        path = f"/api/v1/map/layers/{layer}/geometry"
        _, response = request(base, path, query)
        assert response["error"] is None and response["request_id"]
        data = response["data"]
        features = data["feature_collection"]["features"]
        assert data["layer"] == layer and features and data["geometry_version"]
        assert len(features) == len({str(f["id"]) for f in features})
        emit(
            status="pass",
            path=path,
            features=len(features),
            geometry_version=data["geometry_version"],
        )
        if layer == "parking":
            continue
        path = f"/api/v1/map/layers/{layer}/status"
        _, state = request(base, path, {**query, "geometry_version": data["geometry_version"]})
        assert state["error"] is None
        state = state["data"]
        assert state["geometry_version"] == data["geometry_version"]
        assert len(state["items"]) == len(features)
        field = "population" if layer == "population" else "traffic"
        emit(
            status="pass",
            path=path,
            items=len(state["items"]),
            observation_states=dict(Counter(item[field]["status"] for item in state["items"])),
        )
        _, mismatch = request(base, path, {**query, "geometry_version": "w5-old-version"}, 409)
        assert mismatch["error"]["code"] == "GEOMETRY_VERSION_MISMATCH"
        emit(status="pass", path=path, version_mismatch_http=409)
    path = "/api/v1/map/layers/parking/geometry"
    _, invalid = request(base, path, {**marker, "bbox": "invalid", "level": 6}, 422)
    assert invalid["error"]["code"] == "INVALID_MAP_QUERY"
    emit(status="pass", path=path, invalid_input_http=422)
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        headers, _ = request(base, "/api/v1/health/live", marker, headers={"Origin": origin})
        assert headers.get("Access-Control-Allow-Origin") == origin
    headers, _ = request(
        base, "/api/v1/health/live", marker, headers={"Origin": "https://unapproved.invalid"}
    )
    assert headers.get("Access-Control-Allow-Origin") is None
    emit(status="pass", cors_allowed_origins=2, other_origin_not_allowed=True)
    emit(status="complete", probe_id=probe_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://seoul-now-api.seoul-now.svc.cluster.local")
    parser.add_argument("--probe-id", required=True)
    args = parser.parse_args()
    try:
        run(args.base_url, args.probe_id)
        return 0
    except Exception as error:
        emit(status="failed", error_type=type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
