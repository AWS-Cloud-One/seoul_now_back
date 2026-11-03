from app.storage.redaction import redact


def test_archive_redacts_nested_credentials_without_changing_measurements():
    payload = {
        "serviceKey": "secret",
        "items": [{"count": 3, "access_token": "secret"}],
        "url": "https://provider.example/data?serviceKey=secret&n=3",
        "seoul": "http://openapi.seoul.go.kr:8088/secret/json/citydata/1/5",
    }
    clean = redact(payload)
    assert "secret" not in str(clean)
    assert clean["items"] == [{"count": 3}]
    assert clean["url"].endswith("&n=3")
    assert payload["serviceKey"] == "secret"
