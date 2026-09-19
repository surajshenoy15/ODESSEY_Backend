from datetime import timedelta

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from sqlalchemy import (
    desc,
    func,
    or_,
    select,
)

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

from app.core.security import (
    create_access_token,
    generate_otp,
    hash_otp,
    utcnow,
    verify_otp_hash,
    verify_password,
)

from app.models.entities import (
    Admin,
    OtpCode,
    Ped,
)

from app.schemas import (
    AdminLogin,
    PedOtpRequest,
    PedOtpResponse,
    PedOtpVerify,
    TokenResponse,
)

from app.services.email import (
    email_service,
    otp_html,
)

from app.services.helpers import audit


router = APIRouter(
    prefix='/auth',
    tags=['Authentication'],
)


# ============================================================
# EMAIL NORMALISATION
# ============================================================

def normalize_email(
    value,
):
    if value is None:
        return None

    normalized = (
        str(value)
        .strip()
        .lower()
    )

    return (
        normalized
        if normalized
        else None
    )


# ============================================================
# ALLOWED PED DOMAINS
# ============================================================

def allowed_ped_domains():
    return {
        str(item)
        .strip()
        .lower()
        .lstrip('@')

        for item
        in (
            settings
            .ALLOWED_PED_EMAIL_DOMAINS
            or []
        )

        if str(item).strip()
    }


def validate_new_ped_domain(
    email: str,
):
    """
    New primary PED accounts must use an approved official
    college domain.

    Existing Coach / Manager login emails are allowed to use
    Gmail or other external providers.
    """

    domains = (
        allowed_ped_domains()
    )

    if not domains:
        return

    if '@' not in email:
        raise HTTPException(
            status_code=403,
            detail=(
                'Use a valid official college email address'
            ),
        )

    domain = (
        email
        .rsplit(
            '@',
            1,
        )[1]
        .strip()
        .lower()
    )

    if domain not in domains:
        raise HTTPException(
            status_code=403,
            detail=(
                'Use an approved official college email '
                'or a registered Coach / Manager email'
            ),
        )


# ============================================================
# PED ACCESS EMAIL CONDITIONS
# ============================================================

def ped_access_email_condition(
    email: str,
):
    """
    Case-insensitive lookup across all coordinator identities.
    """

    email = normalize_email(
        email
    )

    return or_(
        func.lower(
            Ped.official_email
        )
        == email,

        func.lower(
            Ped.coach_email
        )
        == email,

        func.lower(
            Ped.manager_email
        )
        == email,
    )


# ============================================================
# FIND PED ACCOUNT BY ANY AUTHORISED LOGIN EMAIL
# ============================================================

async def find_ped_by_access_email(
    db: AsyncSession,
    email: str,
) -> Ped | None:
    """
    Find the common Ped account using:

    - PED official email
    - Coach email
    - Manager email

    All identities belonging to one college resolve to the
    SAME Ped.id.

    If corrupted/legacy data causes the same email to point
    to multiple Ped accounts, login is rejected rather than
    selecting an arbitrary account.
    """

    email = normalize_email(
        email
    )

    if not email:
        return None

    rows = (
        await db.scalars(
            select(
                Ped
            )
            .where(
                ped_access_email_condition(
                    email
                )
            )
        )
    ).all()


    # --------------------------------------------------------
    # DEDUPLICATE BY PED ID
    #
    # One Ped row might theoretically match more than one
    # coordinator column. SQL itself still normally returns
    # the row once, but this makes the safety rule explicit.
    # --------------------------------------------------------

    unique = {
        str(row.id):
            row

        for row in rows
    }


    if len(unique) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                'This email is associated with multiple '
                'college coordinator accounts. '
                'Please contact the BNMIT ODYSSEY administrator.'
            ),
        )


    if not unique:
        return None


    return next(
        iter(
            unique.values()
        )
    )


# ============================================================
# EMAIL AVAILABILITY
# ============================================================

async def ensure_access_email_available(
    db: AsyncSession,
    email: str | None,
    exclude_ped_id=None,
):
    """
    An email may belong to only ONE Ped account regardless of
    whether it appears as:

    - official_email
    - coach_email
    - manager_email

    This provides application-level protection against
    cross-column conflicts that independent database UNIQUE
    indexes cannot detect.
    """

    email = normalize_email(
        email
    )

    if not email:
        return


    query = (
        select(
            Ped
        )
        .where(
            ped_access_email_condition(
                email
            )
        )
    )


    if exclude_ped_id is not None:
        query = query.where(
            Ped.id
            != exclude_ped_id
        )


    existing = await db.scalar(
        query
    )


    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                f'{email} is already associated with '
                'another PED / Coach / Manager account'
            ),
        )


# ============================================================
# VALIDATE COORDINATOR EMAIL SET
# ============================================================

def validate_coordinator_email_set(
    primary_email: str,
    coach_email: str | None,
    manager_email: str | None,
):
    """
    Within one Ped account:

    PED, Coach and Manager must all have different emails.
    """

    primary_email = normalize_email(
        primary_email
    )

    coach_email = normalize_email(
        coach_email
    )

    manager_email = normalize_email(
        manager_email
    )


    if (
        coach_email
        and coach_email
        == primary_email
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                'Coach email cannot be '
                'the same as PED email'
            ),
        )


    if (
        manager_email
        and manager_email
        == primary_email
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                'Manager email cannot be '
                'the same as PED email'
            ),
        )


    if (
        coach_email
        and manager_email
        and coach_email
        == manager_email
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                'Coach and Manager must use '
                'different email addresses'
            ),
        )


# ============================================================
# PED / COACH / MANAGER OTP REQUEST
# ============================================================

@router.post(
    '/ped/request-otp',
    response_model=PedOtpResponse,
)
async def request_otp(
    p: PedOtpRequest,

    db: AsyncSession = Depends(
        get_db
    ),
):
    email = normalize_email(
        p.email
    )


    if not email:
        raise HTTPException(
            status_code=422,
            detail='Email is required',
        )


    now = utcnow()


    # ========================================================
    # EXISTING PED / COACH / MANAGER
    # ========================================================

    existing_ped = (
        await find_ped_by_access_email(
            db,
            email,
        )
    )


    if (
        existing_ped
        and not existing_ped.is_active
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                'This college account is inactive'
            ),
        )


    # ========================================================
    # NEW PRIMARY PED DOMAIN
    # ========================================================

    if not existing_ped:
        validate_new_ped_domain(
            email
        )


    # ========================================================
    # OTP RESEND COOLDOWN
    # ========================================================

    latest = await db.scalar(
        select(
            OtpCode
        )
        .where(
            OtpCode.email
            == email,

            OtpCode.purpose
            == 'PED_LOGIN',
        )
        .order_by(
            desc(
                OtpCode.created_at
            )
        )
        .limit(1)
    )


    if (
        latest
        and latest.created_at
    ):

        created = (
            latest.created_at.replace(
                tzinfo=now.tzinfo
            )

            if (
                latest.created_at.tzinfo
                is None
            )

            else latest.created_at
        )


        elapsed = (
            now - created
        ).total_seconds()


        if (
            elapsed
            < settings
            .OTP_RESEND_COOLDOWN_SECONDS
        ):

            remaining = max(
                1,

                int(
                    settings
                    .OTP_RESEND_COOLDOWN_SECONDS
                    - elapsed
                ),
            )


            raise HTTPException(
                status_code=429,

                detail=(
                    f'Please wait '
                    f'{remaining} seconds'
                ),
            )


    # ========================================================
    # GENERATE OTP
    # ========================================================

    otp = generate_otp()


    db.add(
        OtpCode(
            email=
                email,

            purpose=
                'PED_LOGIN',

            otp_hash=
                hash_otp(
                    otp
                ),

            expires_at=
                (
                    now
                    + timedelta(
                        minutes=
                            settings
                            .OTP_EXPIRE_MINUTES
                    )
                ),
        )
    )


    # ========================================================
    # SEND OTP
    # ========================================================

    await email_service.send(
        db,

        email,

        'Your BNMIT ODYSSEY login OTP',

        otp_html(
            otp,
            settings.OTP_EXPIRE_MINUTES,
        ),

        'PED_OTP',
    )


    await db.commit()


    return PedOtpResponse(
        message=
            'OTP sent',

        expires_in_seconds=
            (
                settings
                .OTP_EXPIRE_MINUTES
                * 60
            ),

        debug_otp=
            (
                otp

                if (
                    settings.TEST_MODE
                    and
                    settings
                    .RETURN_OTP_IN_RESPONSE
                )

                else None
            ),
    )


# ============================================================
# PED / COACH / MANAGER OTP VERIFY
# ============================================================

@router.post(
    '/ped/verify-otp',
    response_model=TokenResponse,
)
async def verify_otp(
    p: PedOtpVerify,

    db: AsyncSession = Depends(
        get_db
    ),
):
    email = normalize_email(
        p.email
    )


    if not email:
        raise HTTPException(
            status_code=422,
            detail='Email is required',
        )


    now = utcnow()


    # ========================================================
    # FIND ACTIVE OTP
    # ========================================================

    otp_record = await db.scalar(
        select(
            OtpCode
        )
        .where(
            OtpCode.email
            == email,

            OtpCode.purpose
            == 'PED_LOGIN',

            OtpCode.used
            .is_(False),
        )
        .order_by(
            desc(
                OtpCode.created_at
            )
        )
        .limit(1)
    )


    if not otp_record:
        raise HTTPException(
            status_code=400,
            detail=
                'No active OTP',
        )


    # ========================================================
    # OTP EXPIRY
    # ========================================================

    expires_at = (
        otp_record.expires_at.replace(
            tzinfo=now.tzinfo
        )

        if (
            otp_record.expires_at.tzinfo
            is None
        )

        else otp_record.expires_at
    )


    if expires_at < now:
        raise HTTPException(
            status_code=400,
            detail=
                'OTP expired',
        )


    # ========================================================
    # OTP ATTEMPTS
    # ========================================================

    if (
        otp_record.attempts
        >= settings.OTP_MAX_ATTEMPTS
    ):
        raise HTTPException(
            status_code=429,
            detail=
                'Too many attempts',
        )


    # ========================================================
    # VERIFY OTP
    # ========================================================

    if not verify_otp_hash(
        p.otp,
        otp_record.otp_hash,
    ):

        otp_record.attempts += 1

        await db.commit()


        raise HTTPException(
            status_code=400,
            detail=
                'Invalid OTP',
        )


    # ========================================================
    # FIND EXISTING ACCOUNT
    # ========================================================

    ped = (
        await find_ped_by_access_email(
            db,
            email,
        )
    )


    created_new_ped = False


    # ========================================================
    # NEW PED ACCOUNT
    # ========================================================

    if not ped:

        """
        Only a completely new PRIMARY PED may create a college
        account.

        Coach / Manager cannot independently create an account.
        """

        if not (
            p.name
            and p.college_name
            and p.contact_number
            and p.declaration_accepted
        ):
            raise HTTPException(
                status_code=403,

                detail=(
                    'This email is not registered as '
                    'PED, Coach or Manager'
                ),
            )


        # ----------------------------------------------------
        # Domain defence-in-depth
        # ----------------------------------------------------

        validate_new_ped_domain(
            email
        )


        # ====================================================
        # REQUESTED COORDINATOR IDENTITIES
        # ====================================================

        requested_coach_email = (
            normalize_email(
                p.coach_email
            )
        )


        requested_manager_email = (
            normalize_email(
                p.manager_email
            )
        )


        validate_coordinator_email_set(
            email,
            requested_coach_email,
            requested_manager_email,
        )


        # ----------------------------------------------------
        # Cross-account checks BEFORE inserting the new Ped.
        # ----------------------------------------------------

        await ensure_access_email_available(
            db,
            email,
        )


        await ensure_access_email_available(
            db,
            requested_coach_email,
        )


        await ensure_access_email_available(
            db,
            requested_manager_email,
        )


        # ====================================================
        # CREATE FULL PED ROW
        #
        # Do not flush an incomplete row first.
        # ====================================================

        ped = Ped(
            official_email=
                email,

            name=
                p.name.strip(),

            college_name=
                p.college_name.strip(),

            college_location=
                (
                    p.college_location.strip()

                    if p.college_location

                    else None
                ),

            contact_number=
                p.contact_number.strip(),

            is_email_verified=
                True,

            is_active=
                True,

            declaration_accepted_at=
                now,


            # ------------------------------------------------
            # Optional Coach
            # ------------------------------------------------

            coach_name=
                (
                    p.coach_name.strip()

                    if (
                        p.coach_name
                        and p.coach_email
                        and p.coach_contact_number
                    )

                    else None
                ),

            coach_email=
                (
                    requested_coach_email

                    if (
                        p.coach_name
                        and p.coach_email
                        and p.coach_contact_number
                    )

                    else None
                ),

            coach_contact_number=
                (
                    p.coach_contact_number.strip()

                    if (
                        p.coach_name
                        and p.coach_email
                        and p.coach_contact_number
                    )

                    else None
                ),


            # ------------------------------------------------
            # Optional Manager
            # ------------------------------------------------

            manager_name=
                (
                    p.manager_name.strip()

                    if (
                        p.manager_name
                        and p.manager_email
                        and p.manager_contact_number
                    )

                    else None
                ),

            manager_email=
                (
                    requested_manager_email

                    if (
                        p.manager_name
                        and p.manager_email
                        and p.manager_contact_number
                    )

                    else None
                ),

            manager_contact_number=
                (
                    p.manager_contact_number.strip()

                    if (
                        p.manager_name
                        and p.manager_email
                        and p.manager_contact_number
                    )

                    else None
                ),
        )


        db.add(
            ped
        )


        # Ped.id is needed by audit and token generation.

        await db.flush()


        created_new_ped = True


    # ========================================================
    # ACCOUNT STATUS
    # ========================================================

    if not ped.is_active:
        raise HTTPException(
            status_code=403,
            detail=
                'This college account is inactive',
        )


    # ========================================================
    # CURRENT LOGIN IDENTITIES
    # ========================================================

    primary_email = (
        normalize_email(
            ped.official_email
        )
    )


    coach_login_email = (
        normalize_email(
            ped.coach_email
        )
    )


    manager_login_email = (
        normalize_email(
            ped.manager_email
        )
    )


    # ========================================================
    # SAFETY: STORED IDENTITIES MUST BE DISTINCT
    # ========================================================

    validate_coordinator_email_set(
        primary_email,
        coach_login_email,
        manager_login_email,
    )


    is_primary_ped = (
        primary_email
        == email
    )


    is_coach = (
        bool(
            coach_login_email
        )
        and
        coach_login_email
        == email
    )


    is_manager = (
        bool(
            manager_login_email
        )
        and
        manager_login_email
        == email
    )


    matched_roles = sum([
        int(
            is_primary_ped
        ),
        int(
            is_coach
        ),
        int(
            is_manager
        ),
    ])


    if matched_roles == 0:
        raise HTTPException(
            status_code=403,

            detail=(
                'Email is not authorised '
                'for this coordinator account'
            ),
        )


    if matched_roles > 1:
        raise HTTPException(
            status_code=409,

            detail=(
                'This email is assigned to multiple '
                'coordinator roles in the same college account. '
                'Please contact the BNMIT ODYSSEY administrator.'
            ),
        )


    # ========================================================
    # LOGIN INFO
    # ========================================================

    ped.last_login_at = (
        now
    )


    ped.is_email_verified = (
        True
    )


    # ========================================================
    # PRIMARY PED ACCOUNT UPDATES
    #
    # Coach and Manager login cannot change these fields.
    # ========================================================

    if (
        is_primary_ped
        and not created_new_ped
    ):

        # ====================================================
        # COMMON PED PROFILE
        # ====================================================

        if p.name:
            ped.name = (
                p.name.strip()
            )


        if p.college_name:
            ped.college_name = (
                p.college_name.strip()
            )


        if p.college_location:
            ped.college_location = (
                p.college_location.strip()
            )


        if p.contact_number:
            ped.contact_number = (
                p.contact_number.strip()
            )


        # ====================================================
        # REQUESTED COORDINATOR EMAILS
        # ====================================================

        requested_coach_email = (
            normalize_email(
                p.coach_email
            )
        )


        requested_manager_email = (
            normalize_email(
                p.manager_email
            )
        )


        existing_coach_email = (
            normalize_email(
                ped.coach_email
            )
        )


        existing_manager_email = (
            normalize_email(
                ped.manager_email
            )
        )


        # ====================================================
        # FINAL EFFECTIVE EMAILS
        #
        # Missing request fields preserve existing values.
        # ====================================================

        final_coach_email = (
            requested_coach_email

            if (
                requested_coach_email
                is not None
            )

            else existing_coach_email
        )


        final_manager_email = (
            requested_manager_email

            if (
                requested_manager_email
                is not None
            )

            else existing_manager_email
        )


        # ====================================================
        # SAME-ACCOUNT COLLISION CHECK
        # ====================================================

        validate_coordinator_email_set(
            primary_email,
            final_coach_email,
            final_manager_email,
        )


        # ====================================================
        # CROSS-ACCOUNT COLLISION CHECK
        # ====================================================

        if requested_coach_email:

            await ensure_access_email_available(
                db,
                requested_coach_email,
                exclude_ped_id=
                    ped.id,
            )


        if requested_manager_email:

            await ensure_access_email_available(
                db,
                requested_manager_email,
                exclude_ped_id=
                    ped.id,
            )


        # ====================================================
        # SAVE COACH
        # ====================================================

        if (
            p.coach_name
            and p.coach_email
            and p.coach_contact_number
        ):

            ped.coach_name = (
                p.coach_name.strip()
            )

            ped.coach_email = (
                requested_coach_email
            )

            ped.coach_contact_number = (
                p.coach_contact_number
                .strip()
            )


        # ====================================================
        # SAVE MANAGER
        # ====================================================

        if (
            p.manager_name
            and p.manager_email
            and p.manager_contact_number
        ):

            ped.manager_name = (
                p.manager_name.strip()
            )

            ped.manager_email = (
                requested_manager_email
            )

            ped.manager_contact_number = (
                p.manager_contact_number
                .strip()
            )


        # ====================================================
        # DECLARATION
        # ====================================================

        if p.declaration_accepted:
            ped.declaration_accepted_at = (
                now
            )


    # ========================================================
    # MARK OTP USED
    #
    # OTP remains unused when validation fails, allowing the
    # user to correct registration data and retry while the OTP
    # is still valid.
    # ========================================================

    otp_record.used = (
        True
    )


    # ========================================================
    # LOGIN IDENTITY FOR AUDIT ONLY
    # ========================================================

    login_identity = (
        'PED'

        if is_primary_ped

        else (
            'COACH'

            if is_coach

            else 'MANAGER'
        )
    )


    # ========================================================
    # AUDIT
    # ========================================================

    await audit(
        db,

        'PED',

        ped.id,

        'PED_LOGIN',

        'PED',

        ped.id,

        details={
            'login_identity':
                login_identity,
        },
    )


    await db.commit()


    # ========================================================
    # COMMON COORDINATOR PORTAL TOKEN
    #
    # PED / Coach / Manager all resolve to the SAME Ped.id.
    #
    # The access token intentionally remains:
    #
    # actor_type = PED
    # role       = PED
    # sub        = Ped.id
    #
    # Therefore all three use the same portal.
    # ========================================================

    return TokenResponse(
        access_token=
            create_access_token(
                ped.id,
                'PED',
                'PED',
            ),

        actor_type=
            'PED',

        role=
            'PED',
    )


# ============================================================
# ADMIN LOGIN
# ============================================================

@router.post(
    '/admin/login',
    response_model=TokenResponse,
)
async def admin_login(
    p: AdminLogin,

    db: AsyncSession = Depends(
        get_db
    ),
):
    email = normalize_email(
        p.email
    )


    if not email:
        raise HTTPException(
            status_code=401,
            detail=
                'Invalid credentials',
        )


    admin = await db.scalar(
        select(
            Admin
        )
        .where(
            func.lower(
                Admin.email
            )
            == email
        )
    )


    if (
        not admin
        or not admin.is_active
        or not verify_password(
            p.password,
            admin.password_hash,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail=
                'Invalid credentials',
        )


    admin.last_login_at = (
        utcnow()
    )


    await audit(
        db,

        'ADMIN',

        admin.id,

        'ADMIN_LOGIN',

        'ADMIN',

        admin.id,
    )


    await db.commit()


    return TokenResponse(
        access_token=
            create_access_token(
                admin.id,
                admin.role,
                'ADMIN',
            ),

        actor_type=
            'ADMIN',

        role=
            admin.role,
    )