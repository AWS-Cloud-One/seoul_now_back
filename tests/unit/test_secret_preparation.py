import json
import stat

import pytest
from sqlalchemy.engine import make_url

from app.core.secrets import DB_KEY, ROLE_KEYS
from infra.scripts import prepare_app_secrets as preparation


@pytest.fixture
def inputs():
    config = {
        "account": "111122223333",
        "region": "ap-northeast-2",
        "db_host": "db.example.test",
        "db_port": 5432,
        "db_name": "seoul_now",
    }
    mapping = {
        "secrets": {
            role: {
                "arn": f"arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:"
                f"seoul-now/lab/{role}-abcdef"
            }
            for role in preparation.ROLES
        }
    }
    values = {key: "synthetic-${PRESERVE_LITERAL}" for keys in ROLE_KEYS.values() for key in keys}
    values[DB_KEY] = "never-copy-the-local-database-url"
    values["UNRELATED_PRIVATE_KEY"] = "must-not-be-copied"
    return config, mapping, values


def test_private_files_role_boundaries_and_distinct_passwords(tmp_path, inputs):
    config, mapping, values = inputs
    directory = preparation.prepare(config, mapping, values, parent=tmp_path)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    passwords = set()
    for role in preparation.ROLES:
        path = directory / f"{role}.request.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        request = json.loads(path.read_text())
        assert request["SecretId"] == mapping["secrets"][role]["arn"]
        assert len(request["ClientRequestToken"]) == 36
        payload = json.loads(request["SecretString"])
        assert payload.keys() == ROLE_KEYS[role]
        url = make_url(payload[DB_KEY])
        assert url.drivername == "postgresql+psycopg"
        assert (url.host, url.port, url.database) == ("db.example.test", 5432, "seoul_now")
        assert url.username == f"seoul_now_{role}"
        assert len(url.password) >= 40
        passwords.add(url.password)
        for key in ROLE_KEYS[role] - {DB_KEY}:
            assert payload[key] == values[key]
    assert len(passwords) == 4


def test_bad_account_or_missing_key_creates_no_payload_files(tmp_path, inputs):
    config, mapping, values = inputs
    config["account"] = "999900001111"
    with pytest.raises(preparation.PreparationError, match="secret_target"):
        preparation.prepare(config, mapping, values, parent=tmp_path)
    assert not list(tmp_path.iterdir())
    config["account"] = "111122223333"
    del values["APP_KAKAO_REST_API_KEY"]
    with pytest.raises(preparation.PreparationError, match="APP_KAKAO_REST_API_KEY"):
        preparation.prepare(config, mapping, values, parent=tmp_path)
    assert not list(tmp_path.iterdir())


def test_cli_prints_only_path_and_preserves_literal_env_value(
    tmp_path, inputs, monkeypatch, capsys
):
    config, mapping, values = inputs
    for key in preparation.OPTIONAL_KEYS:
        del values[key]
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()))
    config_file, mapping_file = tmp_path / "config.json", tmp_path / "mapping.json"
    config_file.write_text(json.dumps(config))
    mapping_file.write_text(json.dumps(mapping))
    monkeypatch.setenv("PRESERVE_LITERAL", "must-not-expand")
    monkeypatch.setattr(
        "sys.argv",
        [
            "prepare_app_secrets",
            "--env-file",
            str(env_file),
            "--config",
            str(config_file),
            "--mapping",
            str(mapping_file),
        ],
    )
    monkeypatch.setattr(preparation.tempfile, "tempdir", str(tmp_path))
    assert preparation.main() == 0
    captured = capsys.readouterr()
    directory = preparation.Path(captured.out.strip())
    assert directory.is_dir() and directory.parent == tmp_path
    assert captured.err == ""
    assert "synthetic" not in captured.out
    request = json.loads((directory / "api.request.json").read_text())
    payload = json.loads(request["SecretString"])
    assert payload["APP_KAKAO_REST_API_KEY"] == "synthetic-${PRESERVE_LITERAL}"
    assert "APP_OPENAI_API_KEY" not in payload
