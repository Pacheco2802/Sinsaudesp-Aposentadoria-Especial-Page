import os
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import HTTPException, Request
from jose import JWTError, jwt

JWT_SECRET = os.environ.get("JWT_SECRET", "")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 8


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])


async def get_current_admin(request: Request) -> str:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    try:
        payload = decode_token(token)
        email: str = payload.get("sub", "")
        if not email:
            raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
        return email
    except JWTError:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
