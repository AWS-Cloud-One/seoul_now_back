"""Mounted as sitecustomize only by the isolated W2 integration check."""

import asyncio
import json
import os
import sys
from functools import partial

import boto3
import httpx
from botocore.stub import Stubber


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected AWS call after bootstrap")


async def parking_response(request):
    from jobs.runtime import report

    assert request.url.host == "openapi.seoul.go.kr"
    assert "/synthetic-seoul/json/GetParkInfo/" in request.url.path
    if os.environ.get("TEST_BLOCK_COLLECTION") == "1":
        report({"status": "test_collection_waiting"})
        await asyncio.Event().wait()
    return httpx.Response(
        200,
        json={
            "GetParkInfo": {
                "RESULT": {"CODE": "INFO-000"},
                "list_total_count": 1,
                "row": [
                    {
                        "PKLT_CD": "w2d-parking",
                        "PKLT_NM": "synthetic parking",
                        "LAT": "37.56",
                        "LOT": "126.97",
                        "TPKCT": "50",
                    }
                ],
            }
        },
    )


def install():
    role = os.environ["APP_RUNTIME_ROLE"]
    if os.environ.get("APP_SETTINGS_LOADED") == "1":
        boto3.Session = forbidden
        if role == "collector":
            httpx.AsyncClient = partial(
                httpx.AsyncClient, transport=httpx.MockTransport(parking_response)
            )
        return

    values = {
        "APP_DATABASE_URL": (
            f"postgresql+psycopg://test:synthetic-password@db-tls/test?application_name=w2d-{role}"
        )
    }
    if role == "api":
        values.update(
            APP_KAKAO_REST_API_KEY="synthetic-kakao",
            APP_SEOUL_CITYDATA_API_KEY="synthetic-citydata",
        )
    elif role == "collector":
        values["APP_SEOUL_API_KEY"] = "synthetic-seoul"
    client = boto3.client(
        "secretsmanager",
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    stub = Stubber(client)
    expected = {"SecretId": os.environ["APP_SETTINGS_SECRET_ID"], "VersionStage": "AWSCURRENT"}
    if os.environ.get("TEST_SECRET_FAILURE") == "1":
        stub.add_client_error(
            "get_secret_value", service_error_code="AccessDeniedException", expected_params=expected
        )
    else:
        stub.add_response("get_secret_value", {"SecretString": json.dumps(values)}, expected)
    stub.activate()
    original = client.get_secret_value

    def get(**kwargs):
        print("w2d-secret-read", file=sys.stderr, flush=True)
        return original(**kwargs)

    client.get_secret_value = get

    class Session:
        def client(self, service, **kwargs):
            assert service == "secretsmanager"
            return client

    boto3.Session = Session


install()
