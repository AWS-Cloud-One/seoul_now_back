"""Translate application failures at the HTTP boundary."""

from app.core.exceptions import ServiceError
from app.schemas.common import ApiError

ERROR_STATUS = {
    "MALFORMED_REQUEST": 400,
    "NOT_FOUND": 404,
    "PARKING_NOT_FOUND": 404,
    "PLACE_NOT_FOUND": 404,
    "GEOMETRY_VERSION_MISMATCH": 409,
    "REQUEST_ID_CONFLICT": 409,
    "REQUEST_IN_PROGRESS": 409,
    "CONTEXT_CONDITIONS_MISMATCH": 409,
    "CONTEXT_EXPIRED": 410,
    "CURSOR_EXPIRED": 410,
    "REQUEST_TOO_LARGE": 413,
    "VALIDATION_ERROR": 422,
    "ORIGIN_OUT_OF_SERVICE_AREA": 422,
    "INVALID_CONTEXT": 422,
    "INVALID_CURSOR": 422,
    "INVALID_MAP_QUERY": 422,
    "MAP_RANGE_TOO_LARGE": 422,
    "RATE_LIMITED": 429,
    "PROVIDER_ERROR": 502,
    "AI_INTERPRETATION_FAILED": 502,
    "DATA_UNAVAILABLE": 503,
    "SERVICE_UNAVAILABLE": 503,
    "PROVIDER_TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}


def error_status(error: ServiceError) -> int:
    return ERROR_STATUS.get(error.code, 500)


def api_error(error: ServiceError) -> ApiError:
    return ApiError(
        code=error.code,
        message=error.message,
        retryable=error.retryable
        if error.retryable is not None
        else error_status(error) in {429, 502, 503, 504},
        retry_after_seconds=error.retry_after_seconds,
    )
