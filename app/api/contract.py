"""Common response and error handling for the API_SPEC contract routes."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.schemas.common import ApiError, ErrorResponse


class ContractError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after_seconds: int | None = None,
    ):
        self.status, self.code, self.message = status, code, message
        self.retryable = status in {429, 502, 503, 504} if retryable is None else retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


def envelope(data=None, error=None, *, request_id: UUID | str | None = None):
    return {
        "request_id": str(request_id if request_id is not None else uuid4()),
        "server_time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "data": data,
        "error": error,
    }


class ContractRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            details = []
            retry_after = None
            body = None
            try:
                return await original(request)
            except RequestValidationError as exc:
                body = exc.body
                malformed = any(e["type"] == "json_invalid" for e in exc.errors())
                status = 400 if malformed else 422
                code = "MALFORMED_REQUEST" if malformed else "VALIDATION_ERROR"
                message = (
                    "JSON 형식을 확인해 주세요." if malformed else "입력 조건을 확인해 주세요."
                )
                retryable = False
                details = [
                    {"field": ".".join(map(str, e["loc"])), "code": e["type"], "message": e["msg"]}
                    for e in exc.errors()
                ]
            except ContractError as exc:
                status, code, message = exc.status, exc.code, exc.message
                retryable, retry_after = exc.retryable, exc.retry_after_seconds
            except SQLAlchemyError:
                status, code, message = 503, "DATA_UNAVAILABLE", "저장 자료를 조회할 수 없습니다."
                retryable = True
            except HTTPException as exc:
                status = exc.status_code
                code = {404: "NOT_FOUND", 429: "RATE_LIMITED", 503: "SERVICE_UNAVAILABLE"}.get(
                    status,
                    "INTERNAL_ERROR" if status >= 500 else "REQUEST_ERROR",
                )
                message = "요청을 처리할 수 없습니다."
                retryable = status in {429, 502, 503, 504}
            except Exception:
                status, code, message = 500, "INTERNAL_ERROR", "서버 처리 중 오류가 발생했습니다."
                retryable = False

            request_id = None
            if request.method == "POST":
                if body is None:
                    try:
                        body = await request.json()
                    except ValueError, UnicodeError:
                        body = None
                if isinstance(body, dict):
                    try:
                        request_id = UUID(str(body.get("request_id")))
                    except ValueError:
                        pass
            headers = {"Cache-Control": "no-store"} if request.method == "POST" else {}
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
            response = ErrorResponse.model_validate(
                envelope(
                    error=ApiError(
                        code=code,
                        message=message,
                        retryable=retryable,
                        retry_after_seconds=retry_after,
                        details=details,
                    ),
                    request_id=request_id,
                )
            )
            return JSONResponse(
                status_code=status,
                content=response.model_dump(mode="json"),
                headers=headers,
            )

        return handler
