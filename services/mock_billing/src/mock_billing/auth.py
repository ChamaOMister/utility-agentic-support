import hmac

from fastapi import Header, Request


class ApiError(Exception):
    def __init__(self, status_code: int, error: str) -> None:
        self.status_code = status_code
        self.error = error


def _key_matches(supplied: str | None, expected: str) -> bool:
    # Encode so non-ASCII input cannot raise TypeError in compare_digest.
    return bool(expected) and hmac.compare_digest(
        (supplied or "").encode("utf-8"), expected.encode("utf-8")
    )


async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    if not _key_matches(x_api_key, request.app.state.settings.billing_api_key):
        raise ApiError(401, "unauthorized")
