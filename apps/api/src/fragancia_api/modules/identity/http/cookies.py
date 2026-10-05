from dataclasses import dataclass

from fastapi import Response

from fragancia_api.shared.http import SESSION_COOKIE

COOKIE_PATH = "/api/v1"


@dataclass(frozen=True, slots=True)
class SessionCookie:
    """The session token's only transport: httpOnly, SameSite=Strict, Secure in production."""

    secure: bool
    max_age_seconds: int

    def set(self, response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=self.max_age_seconds,
            path=COOKIE_PATH,
            httponly=True,
            samesite="strict",
            secure=self.secure,
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(
            SESSION_COOKIE,
            path=COOKIE_PATH,
            httponly=True,
            samesite="strict",
            secure=self.secure,
        )
