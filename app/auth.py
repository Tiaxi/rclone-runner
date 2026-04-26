from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import settings

SESSION_KEY = "authenticated"
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


def require_auth(request: Request) -> None:
    if request.session.get(SESSION_KEY) is True:
        return
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


AuthRequired = Annotated[None, Depends(require_auth)]
