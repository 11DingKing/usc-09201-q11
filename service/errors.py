"""统一领域错误：API 层据此生成一致的错误响应。"""

from __future__ import annotations


class DomainError(Exception):
    """领域错误基类，携带 HTTP 状态码与机器可读错误码。"""

    status = 400
    code = "validation_error"

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class UnauthorizedError(DomainError):
    status = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status = 403
    code = "forbidden"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"
