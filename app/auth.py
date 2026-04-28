from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request, status

from app.config import settings

SESSION_KEY = "authenticated"
CSRF_SESSION_KEY = "csrf_token"
HASH_PREFIX = "pbkdf2_sha256"
ITERATIONS = 600_000


def verify_password(password: str) -> bool:
    if not settings.admin_password_hash:
        return password == "admin"
    return verify_password_hash(password, settings.admin_password_hash)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "$".join(
        [
            HASH_PREFIX,
            str(ITERATIONS),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        ]
    )


def verify_password_hash(password: str, password_hash: str) -> bool:
    try:
        prefix, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if prefix != HASH_PREFIX:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
    except ValueError, TypeError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_field(request: Request) -> str:
    token = csrf_token(request)
    return f'<input type="hidden" name="csrf_token" value="{token}">'


async def require_csrf(request: Request, csrf_token: str = Form("")) -> None:
    expected = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not hmac.compare_digest(expected, csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token",
        )


def require_auth(request: Request) -> None:
    if request.session.get(SESSION_KEY) is True:
        return
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


AuthRequired = Annotated[None, Depends(require_auth)]
