import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, status

from app.core.config import settings

password_hasher = PasswordHasher()
ALGORITHM = "HS256"


def utcnow():
    return datetime.now(timezone.utc)


def generate_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(value):
    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def verify_otp_hash(value, digest):
    return hmac.compare_digest(hash_otp(value), digest)


def hash_password(value):
    return password_hasher.hash(value)


def verify_password(value, digest):
    try:
        return password_hasher.verify(digest, value)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def create_token(subject, token_type, minutes, **claims):
    now = utcnow()
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        **claims,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(subject, role, actor_type):
    return create_token(
        subject,
        "access",
        settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        role=role,
        actor_type=actor_type,
    )


def create_qr_token(registration_id):
    return create_token(registration_id, "registration_qr", 60 * 24 * 180)


def create_file_token(bucket, path, minutes=30):
    return create_token(path, "file", minutes, bucket=bucket)


def decode_token(token, expected_type=None):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    if expected_type and payload.get("type") != expected_type:
        raise HTTPException(status_code=401, detail="Invalid token type")
    return payload
