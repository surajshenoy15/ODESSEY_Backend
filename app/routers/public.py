from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_token
from app.models.entities import EventConfig, Fixture, LiveStream, Registration
from app.services.helpers import qr_public_url
from app.services.storage import storage

router = APIRouter(prefix='/public', tags=['Public'])


async def event_payload(event: EventConfig):
    return {
        'id': event.id,
        'sport_name': event.sport_name,
        'event_name': event.sport_name,
        'event_type': event.event_type,
        'category': event.category,
        'description': event.description,
        'fee_paise': event.fee_paise,
        'team_min_size': event.team_min_size,
        'team_max_size': event.team_max_size,
        'max_substitutes': event.max_substitutes,
        'registration_opens_at': event.registration_opens_at,
        'registration_closes_at': event.registration_closes_at,
        'event_date': event.event_date,
        'venue': event.venue,
        'reporting_instructions': event.reporting_instructions,
        'is_registration_open': event.is_registration_open,
        'is_active': event.is_active,
        'poster_url': await storage.signed_url(settings.SUPABASE_BUCKET_EVENT_MEDIA, event.poster_path) if event.poster_path else None,
    }


@router.get('/events')
async def events(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.scalars(
            select(EventConfig)
            .where(EventConfig.is_active.is_(True))
            .order_by(EventConfig.event_type, EventConfig.sport_name, EventConfig.category)
        )
    ).all()
    return [await event_payload(event) for event in rows]


@router.get('/fixtures')
async def fixtures(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.scalars(
            select(Fixture)
            .where(Fixture.status == 'PUBLISHED', Fixture.visibility == 'PUBLIC')
            .order_by(Fixture.published_at.desc())
        )
    ).all()
    return [
        {
            'id': item.id,
            'event_config_id': item.event_config_id,
            'title': item.title,
            'note': item.note,
            'version': item.version,
            'published_at': item.published_at,
            'download_url': await storage.signed_url(settings.SUPABASE_BUCKET_FIXTURES, item.file_path),
        }
        for item in rows
    ]


@router.get('/live-streams')
async def streams(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.scalars(
            select(LiveStream)
            .where(LiveStream.visibility == 'PUBLIC', LiveStream.status != 'OFFLINE')
            .order_by(LiveStream.scheduled_at.desc())
        )
    ).all()
    return [
        {
            'id': item.id,
            'event_config_id': item.event_config_id,
            'title': item.title,
            'youtube_url': item.youtube_url,
            'status': item.status,
            'offline_message': item.offline_message,
            'scheduled_at': item.scheduled_at,
        }
        for item in rows
    ]


@router.get('/qr/{token}')
async def verify_qr(token: str, db: AsyncSession = Depends(get_db)):
    """Read-only QR landing data.

    The QR is a long-lived signed token. The landing page intentionally returns only the
    minimum event-day identity data requested by the organiser: college, event, category,
    student name and student photo. Contact numbers, email and USN are not exposed here.
    Attendance mutation remains admin-authenticated.
    """
    payload = decode_token(token, 'registration_qr')
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == payload.get('sub'))
        .options(
            selectinload(Registration.students),
            selectinload(Registration.event_config),
        )
    )
    if not registration or registration.qr_token != token:
        raise HTTPException(status_code=404, detail='QR not found or revoked')
    if registration.status != 'APPROVED':
        raise HTTPException(status_code=409, detail=f'Registration status is {registration.status}')

    students = []
    for student in registration.students:
        students.append({
            'id': student.id,
            'full_name': student.full_name,
            'photo_url': await storage.signed_url(settings.SUPABASE_BUCKET_STUDENT_PHOTOS, student.photo_path) if student.photo_path else None,
            'attendance_status': student.attendance_status,
        })
    return {
        'registration_id': registration.id,
        'registration_code': registration.registration_code,
        'college_name': registration.college_name,
        'event': registration.event_config.sport_name,
        'event_type': registration.event_config.event_type,
        'category': registration.event_config.category,
        'student_coordinator_name': registration.student_coordinator_name,
        'student_count': len(students),
        'already_checked_in': registration.attendance_confirmed_at is not None,
        'checked_in_at': registration.attendance_confirmed_at,
        'students': students,
        'qr_url': qr_public_url(token),
    }
