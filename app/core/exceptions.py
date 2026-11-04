class DatabaseUnavailableError(Exception):
    """The database or its required spatial extension is unavailable."""


class ServiceError(Exception):
    """Safe application failure; HTTP status mapping belongs to the API layer."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.retry_after_seconds = retryable, retry_after_seconds
