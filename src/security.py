"""Authentication, password hashing, and role-based access control."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db import get_db
from src.dbmodels import User
from src.settings import settings

password_hasher = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)


class Role(StrEnum):
    """Supported application roles."""

    ENGINEER = "network_engineer"
    SUPERVISOR = "noc_supervisor"
    ADMIN = "system_admin"


def hash_password(password: str) -> str:
    """Hash a user password using the configured Argon2 password hasher."""
    if len(password) < 12:
        raise ValueError("Passwords must contain at least 12 characters.")
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification using pwdlib's password hash implementation."""
    return password_hasher.verify(password, password_hash)


def create_access_token(username: str, role: Role) -> str:
    """Create a short-lived signed access token."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": username,
            "role": role.value,
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_minutes),
            "iss": settings.jwt_issuer,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


class CurrentPrincipal:
    """Authenticated identity carried by a verified access token."""

    def __init__(self, username: str, role: Role) -> None:
        self.username = username
        self.role = role


async def get_current_principal(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[Session, Depends(get_db)],
) -> CurrentPrincipal:
    """Verify a bearer token and return its active application principal."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "role", "exp", "iss"]},
        )
        username = str(payload["sub"])
        token_role = Role(payload["role"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.enabled or user.role != token_role.value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is disabled or access has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return CurrentPrincipal(username, token_role)


def require_roles(*allowed_roles: Role):
    """Create a FastAPI dependency that enforces one of the supplied roles."""

    async def role_guard(
        principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    ) -> CurrentPrincipal:
        if principal.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role is not permitted to perform this operation.",
            )
        return principal

    return role_guard
