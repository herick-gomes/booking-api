import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from redis import Redis, RedisError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import AuthSession, User

passwords = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
DB = Annotated[Session, Depends(get_db)]


def redis_connection():
    return Redis.from_url(get_settings().redis_url, socket_connect_timeout=3, socket_timeout=3)


def limit_auth(request: Request):
    # Trust the direct peer only. Do not accept arbitrary X-Forwarded-For headers.
    peer = request.client.host if request.client else "unknown"
    script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n"
    try:
        count = redis_connection().eval(script, 1, "booking:auth:" + peer)
    except RedisError:
        raise HTTPException(503, "Authentication rate limiter unavailable; try again later.")
    if count > 10:
        raise HTTPException(429, "Too many authentication attempts.", headers={"Retry-After": "60"})


def issue_token(db, user):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    session = AuthSession(user_id=user.id, expires_at=now + timedelta(minutes=settings.access_minutes))
    db.add(session)
    db.flush()
    token = jwt.encode(
        {
            "sub": str(user.id),
            "jti": str(session.id),
            "iat": now,
            "exp": session.expires_at,
            "iss": "booking-api",
            "aud": "booking-api",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": settings.access_minutes * 60}


def current_session(db: DB, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not credentials:
        raise HTTPException(401, "Bearer token required.", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(
            credentials.credentials,
            get_settings().jwt_secret,
            algorithms=["HS256"],
            issuer="booking-api",
            audience="booking-api",
            options={"require": ["sub", "jti", "exp", "iat"]},
        )
        session = db.get(AuthSession, uuid.UUID(payload["jti"]))
        if (
            session is None
            or session.revoked
            or session.expires_at <= datetime.now(timezone.utc)
            or str(session.user_id) != payload["sub"]
        ):
            raise ValueError
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise HTTPException(401, "Invalid or expired session.", headers={"WWW-Authenticate": "Bearer"})
    return session


def current_user(db: DB, session: Annotated[AuthSession, Depends(current_session)]):
    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(401, "Account unavailable.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
