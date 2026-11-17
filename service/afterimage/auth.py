"""Single-owner authentication.

Browsers log in with a password and get a signed, HttpOnly, SameSite=Strict cookie.
Scripts and Hermes send `Authorization: Bearer <AFTERIMAGE_API_TOKEN>`.
Reading the public collection never needs either.

Make a password hash with:  python -m afterimage.auth
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .config import Settings

COOKIE = "ai_session"
SESSION_DAYS = 30
_N, _R, _P = 2**14, 8, 1
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def make_session(secret: str, now: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int((now or time.time()) + SESSION_DAYS * 86400)}).encode())
    return f"{payload.decode()}.{_sign(secret, payload)}"


def check_session(secret: str, cookie: str | None, now: float | None = None) -> bool:
    if not cookie or not secret or "." not in cookie:
        return False
    payload, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload.encode())):
        return False
    try:
        return json.loads(base64.urlsafe_b64decode(payload))["exp"] > (now or time.time())
    except (ValueError, KeyError, TypeError):
        return False


class LoginLimiter:
    """Per-address failed-login counter, in memory. Enough for a single-owner site."""

    def __init__(self) -> None:
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, who: str, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._fails[who]
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS

    def fail(self, who: str, now: float | None = None) -> None:
        self._fails[who].append(now or time.time())

    def clear(self, who: str) -> None:
        self._fails.pop(who, None)


def is_admin(settings: Settings, request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if settings.api_token and header.lower().startswith("bearer "):
        return hmac.compare_digest(header[7:].strip(), settings.api_token)
    return check_session(settings.session_secret, request.cookies.get(COOKIE))


def require_admin(request: Request) -> None:
    if not is_admin(request.app.state.settings, request):
        raise HTTPException(status_code=401, detail="login required")


def client_id(request: Request) -> str:
    # behind Traefik the real address is the first X-Forwarded-For entry
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


if __name__ == "__main__":
    pw = getpass.getpass("New admin password: ")
    if pw != getpass.getpass("Again: "):
        sys.exit("passwords differ")
    if len(pw) < 10:
        sys.exit("use at least 10 characters")
    print(hash_password(pw))
