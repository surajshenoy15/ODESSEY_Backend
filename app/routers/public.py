import hmac
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db

from app.core.security import (
    COORDINATOR_QR_ROLES,
    coordinator_qr_matches,
    decode_token,
)

from app.models.entities import (
    EventConfig,
    Fixture,
    LiveStream,
    Registration,
)

from app.services.helpers import qr_public_url
from app.services.storage import storage


router = APIRouter(
    prefix='/public',
    tags=['Public'],
)

logger = logging.getLogger(__name__)


# ============================================================
# SAFE STORAGE URL
# ============================================================

async def safe_signed_url(
    bucket: str,
    path: str | None,
):
    """
    Generate a signed storage URL safely.

    If:
    - the object is missing,
    - Supabase returns an error,
    - storage is temporarily unavailable,

    return None instead of crashing the whole API endpoint.
    """

    if not path:
        return None

    try:
        return await storage.signed_url(
            bucket,
            path,
        )

    except Exception as exc:
        logger.warning(
            "Unable to generate signed URL "
            "for bucket=%s path=%s error=%s",
            bucket,
            path,
            exc,
        )

        return None


# ============================================================
# EVENT PAYLOAD
# ============================================================

async def event_payload(
    event: EventConfig,
):
    poster_url = await safe_signed_url(
        settings.SUPABASE_BUCKET_EVENT_MEDIA,
        event.poster_path,
    )

    return {
        'id':
            event.id,

        'sport_name':
            event.sport_name,

        'event_name':
            event.sport_name,

        'event_type':
            event.event_type,

        'category':
            event.category,

        'description':
            event.description,

        'fee_paise':
            event.fee_paise,

        'team_min_size':
            event.team_min_size,

        'team_max_size':
            event.team_max_size,

        'max_substitutes':
            event.max_substitutes,

        'registration_opens_at':
            event.registration_opens_at,

        'registration_closes_at':
            event.registration_closes_at,

        'event_date':
            event.event_date,

        'venue':
            event.venue,

        'reporting_instructions':
            event.reporting_instructions,

        'is_registration_open':
            event.is_registration_open,

        'is_active':
            event.is_active,

        'poster_url':
            poster_url,
    }


# ============================================================
# PUBLIC EVENTS
# ============================================================

@router.get('/events')
async def events(
    db: AsyncSession = Depends(
        get_db
    ),
):
    rows = (
        await db.scalars(
            select(
                EventConfig
            )
            .where(
                EventConfig
                .is_active
                .is_(True)
            )
            .order_by(
                EventConfig.event_type,
                EventConfig.sport_name,
                EventConfig.category,
            )
        )
    ).all()

    result = []

    for event in rows:
        result.append(
            await event_payload(
                event
            )
        )

    return result


# ============================================================
# PUBLIC FIXTURES
# ============================================================

@router.get('/fixtures')
async def fixtures(
    db: AsyncSession = Depends(
        get_db
    ),
):
    rows = (
        await db.scalars(
            select(
                Fixture
            )
            .where(
                Fixture.status
                == 'PUBLISHED',

                Fixture.visibility
                == 'PUBLIC',
            )
            .order_by(
                Fixture
                .published_at
                .desc()
            )
        )
    ).all()

    result = []

    for item in rows:

        download_url = (
            await safe_signed_url(
                settings
                .SUPABASE_BUCKET_FIXTURES,

                item.file_path,
            )
        )

        result.append({
            'id':
                item.id,

            'event_config_id':
                item.event_config_id,

            'title':
                item.title,

            'note':
                item.note,

            'version':
                item.version,

            'published_at':
                item.published_at,

            'download_url':
                download_url,
        })

    return result


# ============================================================
# PUBLIC LIVE STREAMS
# ============================================================

@router.get('/live-streams')
async def streams(
    db: AsyncSession = Depends(
        get_db
    ),
):
    rows = (
        await db.scalars(
            select(
                LiveStream
            )
            .where(
                LiveStream.visibility
                == 'PUBLIC',

                LiveStream.status
                != 'OFFLINE',
            )
            .order_by(
                LiveStream
                .scheduled_at
                .desc()
            )
        )
    ).all()

    return [
        {
            'id':
                item.id,

            'event_config_id':
                item.event_config_id,

            'title':
                item.title,

            'youtube_url':
                item.youtube_url,

            'status':
                item.status,

            'offline_message':
                item.offline_message,

            'scheduled_at':
                item.scheduled_at,
        }

        for item in rows
    ]


# ============================================================
# COORDINATOR IDENTITY
# ============================================================

def _coordinator_identity(
    registration: Registration,
    coordinator_role: str,
):
    """
    Resolve ONLY the coordinator represented by the QR.

    Returns:

        (
            public coordinator data,
            coordinator login email
        )

    The email is used internally ONLY for validating the
    identity fingerprint stored inside the coordinator QR.

    It is NEVER returned from the public endpoint.
    """

    if not registration.ped:
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,

            detail=
                'Coordinator information is unavailable',
        )


    role = (
        str(
            coordinator_role
            or ''
        )
        .strip()
        .upper()
    )


    if role not in COORDINATOR_QR_ROLES:
        raise HTTPException(
            status_code=
                status.HTTP_400_BAD_REQUEST,

            detail=
                'Invalid coordinator QR role',
        )


    # ========================================================
    # PED
    # ========================================================

    if role == 'PED':

        name = (
            registration
            .ped
            .name
            or ''
        ).strip()

        email = (
            registration
            .ped
            .official_email
            or ''
        ).strip().lower()


        if not name or not email:
            raise HTTPException(
                status_code=
                    status.HTTP_409_CONFLICT,

                detail=
                    'PED is no longer registered',
            )


        return (
            {
                'role':
                    'PED',

                'label':
                    'PED',

                'name':
                    name,
            },

            email,
        )


    # ========================================================
    # COACH
    # ========================================================

    if role == 'COACH':

        name = (
            registration
            .ped
            .coach_name
            or ''
        ).strip()

        email = (
            registration
            .ped
            .coach_email
            or ''
        ).strip().lower()


        if not name or not email:
            raise HTTPException(
                status_code=
                    status.HTTP_409_CONFLICT,

                detail=
                    'Coach is no longer registered',
            )


        return (
            {
                'role':
                    'COACH',

                'label':
                    'Coach',

                'name':
                    name,
            },

            email,
        )


    # ========================================================
    # MANAGER
    # ========================================================

    if role == 'MANAGER':

        name = (
            registration
            .ped
            .manager_name
            or ''
        ).strip()

        email = (
            registration
            .ped
            .manager_email
            or ''
        ).strip().lower()


        if not name or not email:
            raise HTTPException(
                status_code=
                    status.HTTP_409_CONFLICT,

                detail=
                    'Manager is no longer registered',
            )


        return (
            {
                'role':
                    'MANAGER',

                'label':
                    'Manager',

                'name':
                    name,
            },

            email,
        )


    raise HTTPException(
        status_code=
            status.HTTP_400_BAD_REQUEST,

        detail=
            'Invalid coordinator QR role',
    )


# ============================================================
# PUBLIC QR VERIFICATION
# ============================================================

@router.get('/qr/{token}')
async def verify_qr(
    token: str,

    db: AsyncSession = Depends(
        get_db
    ),
):
    """
    Public, read-only QR verification.

    Supports:

    1. registration_qr

       Legacy/generic registration QR.

    2. coordinator_qr

       PED / Coach / Manager attendance QR.


    SECURITY:

    Public response NEVER includes:

    - student names
    - student photographs
    - student USNs
    - student email
    - coordinator email
    - coordinator phone
    - other coordinator names

    Coordinator QR validation checks:

    1. JWT signature
    2. token type
    3. approved registration
    4. current master registration QR
    5. coordinator role
    6. current coordinator identity/email

    Student roster/photo verification remains available only
    through the authenticated attendance admin endpoint.
    """


    # ========================================================
    # DECODE TOKEN
    # ========================================================

    try:
        payload = decode_token(
            token
        )

    except HTTPException as exc:
        raise HTTPException(
            status_code=
                status.HTTP_400_BAD_REQUEST,

            detail=
                'Invalid or expired QR code',
        ) from exc


    token_type = (
        payload.get(
            'type'
        )
    )


    registration_id = (
        payload.get(
            'sub'
        )
    )


    if not registration_id:
        raise HTTPException(
            status_code=
                status.HTTP_400_BAD_REQUEST,

            detail=
                'Invalid QR payload',
        )


    # ========================================================
    # ONLY ACCEPT QR TOKENS
    # ========================================================

    if token_type not in {
        'registration_qr',
        'coordinator_qr',
    }:
        raise HTTPException(
            status_code=
                status.HTTP_400_BAD_REQUEST,

            detail=
                'Invalid QR token type',
        )


    # ========================================================
    # LOAD REGISTRATION
    # ========================================================

    registration = await db.scalar(
        select(
            Registration
        )
        .where(
            Registration.id
            == registration_id
        )
        .options(
            selectinload(
                Registration.event_config
            ),

            selectinload(
                Registration.ped
            ),
        )
    )


    if not registration:
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,

            detail=
                'Registration not found',
        )


    # ========================================================
    # APPROVAL STATUS
    # ========================================================

    if (
        registration.status
        != 'APPROVED'
    ):
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,

            detail=(
                f'Registration status is '
                f'{registration.status}'
            ),
        )


    # ========================================================
    # MASTER QR MUST EXIST
    #
    # REJECT:
    # registration.qr_token = None
    #
    # REOPEN:
    # registration.qr_token = None
    #
    # Therefore every role-specific QR from that approval
    # generation becomes invalid.
    # ========================================================

    if not registration.qr_token:
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,

            detail=
                'QR not found or revoked',
        )


    coordinator = None


    # ========================================================
    # LEGACY / GENERIC REGISTRATION QR
    # ========================================================

    if token_type == 'registration_qr':

        if not hmac.compare_digest(
            str(
                registration.qr_token
            ),
            str(
                token
            ),
        ):
            raise HTTPException(
                status_code=
                    status.HTTP_404_NOT_FOUND,

                detail=
                    'QR not found or revoked',
            )


        # ----------------------------------------------------
        # Generic registration QR intentionally does NOT
        # identify the holder as PED / Coach / Manager.
        # ----------------------------------------------------

        coordinator = None


    # ========================================================
    # COORDINATOR-SPECIFIC QR
    # ========================================================

    elif token_type == 'coordinator_qr':

        coordinator_role = (
            str(
                payload.get(
                    'coordinator_role'
                )
                or ''
            )
            .strip()
            .upper()
        )


        if (
            coordinator_role
            not in COORDINATOR_QR_ROLES
        ):
            raise HTTPException(
                status_code=
                    status.HTTP_400_BAD_REQUEST,

                detail=
                    'Invalid coordinator QR role',
            )


        # ====================================================
        # RESOLVE CURRENT ROLE IDENTITY
        #
        # This gives us:
        #
        # public identity:
        #     role
        #     label
        #     name
        #
        # internal identity:
        #     registered coordinator email
        #
        # Email is NEVER exposed to the public response.
        # ====================================================

        (
            coordinator,
            coordinator_email,
        ) = _coordinator_identity(
            registration,
            coordinator_role,
        )


        # ====================================================
        # VERIFY:
        #
        # 1. CURRENT MASTER QR VERSION
        # 2. CURRENT COORDINATOR IDENTITY
        #
        # Example:
        #
        # QR issued:
        # coach1@gmail.com
        #
        # Coach later becomes:
        # coach2@gmail.com
        #
        # identity fingerprint mismatch
        # -> old Coach QR rejected.
        # ====================================================

        if not coordinator_qr_matches(
            payload,

            registration.qr_token,

            coordinator_email,
        ):
            raise HTTPException(
                status_code=
                    status.HTTP_404_NOT_FOUND,

                detail=
                    (
                        'Coordinator QR is expired, '
                        'revoked or no longer belongs '
                        'to the current coordinator'
                    ),
            )


    # ========================================================
    # EVENT
    # ========================================================

    event = (
        registration.event_config
    )


    # ========================================================
    # SAFE PUBLIC COORDINATOR RESPONSE
    # ========================================================

    coordinator_role = (
        coordinator.get(
            'role'
        )
        if coordinator
        else None
    )


    coordinator_label = (
        coordinator.get(
            'label'
        )
        if coordinator
        else None
    )


    coordinator_name = (
        coordinator.get(
            'name'
        )
        if coordinator
        else None
    )


    # ========================================================
    # RESPONSE
    # ========================================================

    return {
        'valid':
            True,

        'registration_id':
            registration.id,

        'registration_code':
            registration.registration_code,

        'college_name':
            registration.college_name,

        'event':
            (
                event.sport_name
                if event
                else None
            ),

        'event_type':
            (
                event.event_type
                if event
                else None
            ),

        'category':
            (
                event.category
                if event
                else None
            ),


        # ====================================================
        # ONLY THE ROLE REPRESENTED BY THIS QR
        # ====================================================

        'coordinator':
            coordinator,

        'coordinator_role':
            coordinator_role,

        'coordinator_label':
            coordinator_label,

        'coordinator_name':
            coordinator_name,


        # ====================================================
        # ATTENDANCE STATE
        # ====================================================

        'already_checked_in':
            (
                registration
                .attendance_confirmed_at
                is not None
            ),

        'checked_in_at':
            registration
            .attendance_confirmed_at,


        # ====================================================
        # ORIGINAL QR LANDING URL
        # ====================================================

        'qr_url':
            qr_public_url(
                token
            ),
    }