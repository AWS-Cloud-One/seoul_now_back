"""Signed, expiring continuation token. This is integrity protection, not encryption."""

import base64
import hashlib
import hmac
import time

from app.schemas.recommendation import SearchState


class StateCodec:
    def __init__(self, secret: str):
        self.secret = secret.encode()

    def encode(self, state: SearchState) -> str:
        payload = base64.urlsafe_b64encode(state.model_dump_json().encode()).decode()
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def decode(self, token: str, now: float | None = None) -> SearchState:
        try:
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            state = SearchState.model_validate_json(
                base64.b64decode(payload, altchars=b"-_", validate=True)
            )
            if state.expires_at <= (time.time() if now is None else now):
                raise ValueError("expired")
            return state
        except ValueError, TypeError, UnicodeError:
            raise ValueError(
                "추천 상태가 만료되었거나 변경되었습니다. 새 검색을 시작해 주세요."
            ) from None
