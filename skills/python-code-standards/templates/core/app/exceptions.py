class AppError(Exception):
    """Base for domain errors. Carries no HTTP knowledge: error_handlers.py maps type -> status."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BadRequestError(AppError): ...


class UnauthorizedError(AppError): ...


class ForbiddenError(AppError): ...


class NotFoundError(AppError): ...


class ConflictError(AppError): ...


class UnprocessableError(AppError): ...


class UpstreamError(AppError):
    """An external service failed after retries."""


def require[T](value: T | None, what: str) -> T:
    if value is None:
        raise NotFoundError(f"{what} not found")
    return value
