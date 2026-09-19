import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_token
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
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.scalars(
            select(EventConfig)
            .where(
                EventConfig.is_active.is_(True)
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
            await event_payload(event)
        )

    return result


# ============================================================
# PUBLIC FIXTURES
# ============================================================

@router.get('/fixtures')
async def fixtures(
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.scalars(
            select(Fixture)
            .where(
                Fixture.status == 'PUBLISHED',
                Fixture.visibility == 'PUBLIC',
            )
            .order_by(
                Fixture.published_at.desc()
            )
        )
    ).all()

    result = []

    for item in rows:

        download_url = await safe_signed_url(
            settings.SUPABASE_BUCKET_FIXTURES,
            item.file_path,
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
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.scalars(
            select(LiveStream)
            .where(
                LiveStream.visibility == 'PUBLIC',
                LiveStream.status != 'OFFLINE',
            )
            .order_by(
                LiveStream.scheduled_at.desc()
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
# PUBLIC QR VERIFICATION
# ============================================================

@router.get('/qr/{token}')
async def verify_qr(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Read-only QR landing data.

    Only minimum event-day identity information
    is returned publicly.

    Contact number, email and USN are intentionally
    not exposed here.

    Attendance mutation remains admin-authenticated.
    """

    # --------------------------------------------------------
    # Decode QR
    # --------------------------------------------------------

    try:
        payload = decode_token(
            token,
            'registration_qr',
        )

    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Invalid or expired QR code',
        )


    registration_id = payload.get('sub')

    if not registration_id:
        raise HTTPException(
            status_code=400,
            detail='Invalid QR payload',
        )


    # --------------------------------------------------------
    # Load registration
    # --------------------------------------------------------

    registration = await db.scalar(
        select(Registration)
        .where(
            Registration.id ==
            registration_id
        )
        .options(
            selectinload(
                Registration.students
            ),
            selectinload(
                Registration.event_config
            ),
        )
    )


    # --------------------------------------------------------
    # QR validation
    # --------------------------------------------------------

    if (
        not registration
        or registration.qr_token != token
    ):
        raise HTTPException(
            status_code=404,
            detail='QR not found or revoked',
        )


    if registration.status != 'APPROVED':
        raise HTTPException(
            status_code=409,
            detail=(
                f'Registration status is '
                f'{registration.status}'
            ),
        )


    # --------------------------------------------------------
    # Student payload
    # --------------------------------------------------------

    students = []

    for student in registration.students:

        photo_url = await safe_signed_url(
            settings.SUPABASE_BUCKET_STUDENT_PHOTOS,
            student.photo_path,
        )

        students.append({
            'id':
                student.id,

            'full_name':
                student.full_name,

            'photo_url':
                photo_url,

            'attendance_status':
                student.attendance_status,
        })


    # --------------------------------------------------------
    # Event information
    # --------------------------------------------------------

    event = registration.event_config


    return {
        'registration_id':
            registration.id,

        'registration_code':
            registration.registration_code,

        'college_name':
            registration.college_name,

        'event':
            event.sport_name
            if event
            else None,

        'event_type':
            event.event_type
            if event
            else None,

        'category':
            event.category
            if event
            else None,

        'student_coordinator_name':
            registration.student_coordinator_name,

        'student_count':
            len(students),

        'already_checked_in':
            registration.attendance_confirmed_at
            is not None,

        'checked_in_at':
            registration.attendance_confirmed_at,

        'students':
            students,

        'qr_url':
            qr_public_url(token),
    }