"""Immutable, content-addressed local evidence files with verified reads."""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


class PayloadStorageError(Exception):
    """Safe public error: never expose filenames, provider bodies or credentials."""


@dataclass(frozen=True)
class StoredFile:
    file_ref: str
    sha256: str
    byte_size: int


class PayloadStore:
    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def encode_json(value):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def put_bytes(self, body):
        return self.put_chunks((body,))

    def put_chunks(self, chunks):
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as handle:
                temporary = Path(handle.name)
                for body in chunks:
                    handle.write(body)
                    digest.update(body)
                    size += len(body)
                handle.flush()
                os.fsync(handle.fileno())
            sha = digest.hexdigest()
            key = f"sha256/{sha[:2]}/{sha}.blob"
            target = self.root / key
            if not target.resolve().is_relative_to(self.root.resolve()):
                raise PayloadStorageError("원본 보관 파일의 참조가 올바르지 않습니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            # Verify an existing object too, without holding a whole archive in memory.
            check, actual_size = hashlib.sha256(), 0
            with target.open("rb") as handle:
                while body := handle.read(1024 * 1024):
                    check.update(body)
                    actual_size += len(body)
            if actual_size != size or check.hexdigest() != sha:
                raise PayloadStorageError("원본 보관 파일의 무결성을 확인할 수 없습니다.")
            for directory in (target.parent, target.parent.parent, self.root):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return StoredFile(key, sha, size)
        except OSError, ValueError:
            raise PayloadStorageError("원본 보관 파일을 저장할 수 없습니다.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read_bytes(self, key, sha256, byte_size):
        if not re.fullmatch(r"sha256/[0-9a-f]{2}/[0-9a-f]{64}\.blob", key or ""):
            raise PayloadStorageError("원본 보관 파일의 참조가 올바르지 않습니다.")
        if not isinstance(sha256, str) or key != f"sha256/{sha256[:2]}/{sha256}.blob":
            raise PayloadStorageError("원본 보관 파일의 참조가 일치하지 않습니다.")
        try:
            target = self.root / key
            if not target.resolve().is_relative_to(self.root.resolve()):
                raise PayloadStorageError("원본 보관 파일의 참조가 올바르지 않습니다.")
            body = target.read_bytes()
        except OSError:
            raise PayloadStorageError("원본 보관 파일을 읽을 수 없습니다.") from None
        if len(body) != byte_size or hashlib.sha256(body).hexdigest() != sha256:
            raise PayloadStorageError("원본 보관 파일의 무결성을 확인할 수 없습니다.")
        return body

    def load_json(self, payload):
        body = self.read_bytes(payload.file_ref, payload.sha256, payload.byte_size)
        try:
            return json.loads(body)
        except UnicodeDecodeError, ValueError:
            raise PayloadStorageError("원본 보관 파일을 해석할 수 없습니다.") from None

    def delete(self, key):
        """Called only after committed metadata deletion and a locked reference check."""
        if not re.fullmatch(r"sha256/[0-9a-f]{2}/[0-9a-f]{64}\.blob", key or ""):
            raise PayloadStorageError("원본 보관 파일의 참조가 올바르지 않습니다.")
        path = self.root / key
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise PayloadStorageError("원본 보관 파일의 참조가 올바르지 않습니다.")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise PayloadStorageError("원본 보관 파일을 삭제할 수 없습니다.") from None


def configured_store():
    from app.core.config import Settings

    return PayloadStore(Settings().payload_storage_dir)
