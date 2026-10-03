from dataclasses import dataclass

from fragancia_api.shared.kernel import (
    ConflictError,
    InvalidValueError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
)


@dataclass(frozen=True)
class EmailInvalid(InvalidValueError):
    code: str = "IDENTITY_EMAIL_INVALID"
    message: str = "Enter a valid email address"


@dataclass(frozen=True)
class NameInvalid(InvalidValueError):
    code: str = "IDENTITY_NAME_INVALID"
    message: str = "A name needs 2 to 80 characters"


@dataclass(frozen=True)
class PasswordTooWeak(InvalidValueError):
    code: str = "IDENTITY_PASSWORD_TOO_WEAK"
    message: str = "A password needs 12 to 128 characters"


@dataclass(frozen=True)
class CurrentPasswordWrong(InvalidValueError):
    code: str = "IDENTITY_CURRENT_PASSWORD_WRONG"
    message: str = "The current password is not correct"


@dataclass(frozen=True)
class EmailTaken(ConflictError):
    code: str = "IDENTITY_EMAIL_TAKEN"
    message: str = "A user with this email already exists"


@dataclass(frozen=True)
class InvalidCredentials(UnauthenticatedError):
    code: str = "IDENTITY_INVALID_CREDENTIALS"
    message: str = "Email or password is incorrect"


@dataclass(frozen=True)
class TooManyAttempts(RateLimitedError):
    code: str = "IDENTITY_TOO_MANY_ATTEMPTS"
    message: str = "Too many sign-in attempts; try again later"


@dataclass(frozen=True)
class SessionNotFound(NotFoundError):
    code: str = "IDENTITY_SESSION_NOT_FOUND"
    message: str = "Session not found"
