import hashlib
import hmac
import secrets

from datetime import (
    datetime,
    timedelta,
    timezone,
)

import jwt

from argon2 import PasswordHasher

from argon2.exceptions import (
    InvalidHashError,
    VerifyMismatchError,
)

from fastapi import (
    HTTPException,
    status,
)

from app.core.config import settings


password_hasher = PasswordHasher()

ALGORITHM = "HS256"


COORDINATOR_QR_ROLES = {
    "PED",
    "COACH",
    "MANAGER",
}


# ============================================================
# TIME
# ============================================================

def utcnow():
    return datetime.now(
        timezone.utc
    )


# ============================================================
# OTP
# ============================================================

def generate_otp():
    return (
        f"{secrets.randbelow(1_000_000):06d}"
    )


def hash_otp(value):
    return hmac.new(
        settings.SECRET_KEY.encode(),
        str(value).encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_otp_hash(
    value,
    digest,
):
    return hmac.compare_digest(
        hash_otp(value),
        str(digest),
    )


# ============================================================
# PASSWORD
# ============================================================

def hash_password(value):
    return password_hasher.hash(
        value
    )


def verify_password(
    value,
    digest,
):
    try:
        return password_hasher.verify(
            digest,
            value,
        )

    except (
        VerifyMismatchError,
        InvalidHashError,
        Exception,
    ):
        return False


# ============================================================
# GENERIC TOKEN
# ============================================================

def create_token(
    subject,
    token_type,
    minutes,
    **claims,
):
    """
    Create a signed JWT.

    JWT subject is always converted to string because the
    standard 'sub' claim should be represented as a string.
    """

    now = utcnow()

    payload = {
        "sub":
            str(subject),

        "type":
            str(token_type),

        "iat":
            int(
                now.timestamp()
            ),

        "exp":
            int(
                (
                    now
                    + timedelta(
                        minutes=minutes
                    )
                ).timestamp()
            ),

        **claims,
    }

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


# ============================================================
# ACCESS TOKEN
# ============================================================

def create_access_token(
    subject,
    role,
    actor_type,
):
    return create_token(
        subject,

        "access",

        settings
        .ACCESS_TOKEN_EXPIRE_MINUTES,

        role=role,

        actor_type=actor_type,
    )


# ============================================================
# MASTER REGISTRATION QR
# ============================================================

def create_qr_token(
    registration_id,
):
    """
    Master registration QR token.

    Stored directly in:

        Registration.qr_token

    It serves two purposes:

    1. Legacy/generic registration QR.
    2. Revocation/version source for PED/Coach/Manager QRs.

    If this token changes or becomes None, all role-specific
    QRs created from the old token become invalid.
    """

    return create_token(
        registration_id,

        "registration_qr",

        60 * 24 * 180,
    )


# ============================================================
# MASTER REGISTRATION QR FINGERPRINT
# ============================================================

def registration_qr_fingerprint(
    token,
):
    """
    Secure fingerprint of Registration.qr_token.

    The raw master token is NOT copied into coordinator JWTs.

    Instead coordinator JWTs contain:

        qr_version = HMAC(master registration QR)

    This means:

    APPROVED
        master A
        ↓
        PED/Coach/Manager QR linked to A

    REOPEN / REJECT
        qr_token = None
        ↓
        all role QRs invalid

    REAPPROVE
        master B
        ↓
        old role QRs from A remain invalid
    """

    if not token:
        return None

    return hmac.new(
        settings.SECRET_KEY.encode(),
        str(token).encode(),
        hashlib.sha256,
    ).hexdigest()


# ============================================================
# NORMALIZE COORDINATOR ROLE
# ============================================================

def normalize_coordinator_role(
    coordinator_role,
):
    role = (
        str(
            coordinator_role
            or ''
        )
        .strip()
        .upper()
    )

    if role not in COORDINATOR_QR_ROLES:
        raise ValueError(
            "Invalid coordinator QR role"
        )

    return role


# ============================================================
# NORMALIZE COORDINATOR IDENTITY
# ============================================================

def normalize_coordinator_identity(
    coordinator_identity,
):
    """
    Coordinator identity is currently the coordinator's
    registered login email.

    Examples:

        PED
            ped@college.edu

        COACH
            coach@gmail.com

        MANAGER
            manager@gmail.com

    Email is normalized before fingerprinting so differences
    in casing/whitespace do not produce different identities.
    """

    identity = (
        str(
            coordinator_identity
            or ''
        )
        .strip()
        .lower()
    )

    if not identity:
        raise ValueError(
            "Coordinator identity is required"
        )

    return identity


# ============================================================
# COORDINATOR IDENTITY FINGERPRINT
# ============================================================

def coordinator_identity_fingerprint(
    coordinator_role,
    coordinator_identity,
):
    """
    Securely bind a QR to the person who received it.

    We do NOT put the coordinator's email directly into the
    QR JWT.

    Instead the JWT stores an HMAC fingerprint generated from:

        role + normalized registered email

    Example:

        COACH
        coach1@gmail.com

            ↓

        identity_version =
            HMAC(
                SECRET_KEY,
                "coordinator:v1:COACH:coach1@gmail.com"
            )

    If the registered Coach is later changed to:

        coach2@gmail.com

    the old Coach QR no longer matches.
    """

    role = normalize_coordinator_role(
        coordinator_role
    )

    identity = normalize_coordinator_identity(
        coordinator_identity
    )

    message = (
        f"coordinator:v1:"
        f"{role}:"
        f"{identity}"
    )

    return hmac.new(
        settings.SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


# ============================================================
# CREATE COORDINATOR-SPECIFIC QR
# ============================================================

def create_coordinator_qr_token(
    registration_id,
    coordinator_role,
    registration_qr_token,
    coordinator_identity,
):
    """
    Generate a role-specific coordinator QR.

    coordinator_identity must be the coordinator's registered
    login email.

    Example:

        create_coordinator_qr_token(
            registration.id,
            "COACH",
            registration.qr_token,
            registration.ped.coach_email,
        )

    JWT contains:

        sub
            Registration ID

        type
            coordinator_qr

        coordinator_role
            PED / COACH / MANAGER

        qr_version
            Fingerprint of current master registration QR

        identity_version
            Fingerprint of the specific coordinator identity

    The coordinator email itself is never exposed in the QR.
    """

    role = normalize_coordinator_role(
        coordinator_role
    )

    if not registration_qr_token:
        raise ValueError(
            "Registration QR token is required"
        )

    identity = normalize_coordinator_identity(
        coordinator_identity
    )


    # --------------------------------------------------------
    # MASTER REGISTRATION VERSION
    # --------------------------------------------------------

    qr_version = (
        registration_qr_fingerprint(
            registration_qr_token
        )
    )


    if not qr_version:
        raise ValueError(
            "Unable to fingerprint registration QR"
        )


    # --------------------------------------------------------
    # SPECIFIC COORDINATOR IDENTITY VERSION
    # --------------------------------------------------------

    identity_version = (
        coordinator_identity_fingerprint(
            role,
            identity,
        )
    )


    return create_token(
        registration_id,

        "coordinator_qr",

        60 * 24 * 180,

        coordinator_role=
            role,

        qr_version=
            qr_version,

        identity_version=
            identity_version,
    )


# ============================================================
# CHECK COORDINATOR QR
# ============================================================

def coordinator_qr_matches(
    payload,
    registration_qr_token,
    coordinator_identity,
):
    """
    Validate a coordinator-specific QR against:

    1. current Registration.qr_token
    2. coordinator role
    3. current coordinator identity/email

    This protects against both:

    A. Registration QR rotation/revocation

        approved -> master A
        reopened -> None
        reapproved -> master B

    and:

    B. Coordinator replacement

        Coach QR issued to:
            coach1@gmail.com

        Coach later changed to:
            coach2@gmail.com

        Result:
            old Coach QR fails because identity_version differs.
    """

    if not payload:
        return False


    if not registration_qr_token:
        return False


    # ========================================================
    # TOKEN TYPE
    # ========================================================

    if (
        payload.get(
            "type"
        )
        != "coordinator_qr"
    ):
        return False


    # ========================================================
    # ROLE
    # ========================================================

    supplied_role = (
        str(
            payload.get(
                "coordinator_role"
            )
            or ''
        )
        .strip()
        .upper()
    )


    if supplied_role not in COORDINATOR_QR_ROLES:
        return False


    # ========================================================
    # CURRENT COORDINATOR IDENTITY
    # ========================================================

    try:
        current_identity = (
            normalize_coordinator_identity(
                coordinator_identity
            )
        )

    except ValueError:
        return False


    # ========================================================
    # MASTER QR VERSION
    # ========================================================

    supplied_qr_version = (
        payload.get(
            "qr_version"
        )
    )


    if not supplied_qr_version:
        return False


    expected_qr_version = (
        registration_qr_fingerprint(
            registration_qr_token
        )
    )


    if not expected_qr_version:
        return False


    if not hmac.compare_digest(
        str(
            supplied_qr_version
        ),
        str(
            expected_qr_version
        ),
    ):
        return False


    # ========================================================
    # COORDINATOR IDENTITY VERSION
    # ========================================================

    supplied_identity_version = (
        payload.get(
            "identity_version"
        )
    )


    if not supplied_identity_version:
        return False


    try:
        expected_identity_version = (
            coordinator_identity_fingerprint(
                supplied_role,
                current_identity,
            )
        )

    except ValueError:
        return False


    if not hmac.compare_digest(
        str(
            supplied_identity_version
        ),
        str(
            expected_identity_version
        ),
    ):
        return False


    return True


# ============================================================
# FILE TOKEN
# ============================================================

def create_file_token(
    bucket,
    path,
    minutes=30,
):
    return create_token(
        path,

        "file",

        minutes,

        bucket=bucket,
    )


# ============================================================
# DECODE TOKEN
# ============================================================

def decode_token(
    token,
    expected_type=None,
):
    try:
        payload = jwt.decode(
            token,

            settings.SECRET_KEY,

            algorithms=[
                ALGORITHM
            ],
        )

    except jwt.ExpiredSignatureError as exc:

        raise HTTPException(
            status_code=
                status.HTTP_401_UNAUTHORIZED,

            detail=
                "Token expired",
        ) from exc


    except jwt.InvalidTokenError as exc:

        raise HTTPException(
            status_code=
                status.HTTP_401_UNAUTHORIZED,

            detail=
                "Invalid token",
        ) from exc


    if (
        expected_type
        and payload.get(
            "type"
        ) != expected_type
    ):
        raise HTTPException(
            status_code=
                status.HTTP_401_UNAUTHORIZED,

            detail=
                "Invalid token type",
        )


    return payload