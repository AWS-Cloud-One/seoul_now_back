from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.storage.payloads import PayloadStorageError, PayloadStore


def test_roundtrip_dedup_and_chunked_write(tmp_path):
    store = PayloadStore(tmp_path)
    value = {"서울": [0, False, None, ""], "nested": {"key": "value"}}
    body = store.encode_json(value)
    first = store.put_bytes(body)
    second = store.put_chunks([body[:5], body[5:]])
    assert first == second
    assert store.load_json(SimpleNamespace(**asdict(first))) == value
    assert len(list(tmp_path.rglob("*.blob"))) == 1
    assert (tmp_path / first.file_ref).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_missing_or_corrupted_files_fail_without_silent_replacement(tmp_path, damage):
    store = PayloadStore(tmp_path)
    saved = store.put_bytes(b'{"a":1}')
    target = tmp_path / saved.file_ref
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b'{"a":2}')
        with pytest.raises(PayloadStorageError, match="무결성"):
            store.put_bytes(b'{"a":1}')
        assert target.read_bytes() == b'{"a":2}'
    with pytest.raises(PayloadStorageError) as error:
        store.load_json(SimpleNamespace(**asdict(saved)))
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("key", ["../../secret", "/etc/passwd", "https://example.com/raw"])
def test_external_or_traversal_references_are_not_followed(tmp_path, key):
    with pytest.raises(PayloadStorageError):
        PayloadStore(tmp_path).read_bytes(key, "0" * 64, 0)


def test_read_rejects_symlink_outside_store(tmp_path):
    root = tmp_path / "store"
    store = PayloadStore(root)
    saved = store.put_bytes(b"evidence")
    outside = tmp_path / "outside"
    outside.write_bytes(b"evidence")
    target = root / saved.file_ref
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(PayloadStorageError, match="참조"):
        store.read_bytes(saved.file_ref, saved.sha256, saved.byte_size)
