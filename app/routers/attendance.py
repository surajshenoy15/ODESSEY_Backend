from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_admin_roles
from app.core.security import decode_token, utcnow
from app.models.entities import Admin, AttendanceRecord, Registration
from app.schemas import AttendanceConfirm, MessageResponse
from app.services.helpers import audit
from app.services.storage import storage

router = APIRouter(prefix='/admin/attendance', tags=['Attendance'])


async def _load_registration(db: AsyncSession, rid: str):
    return await db.scalar(
        select(Registration)
        .where(Registration.id == rid)
        .options(
            selectinload(Registration.students),
            selectinload(Registration.event_config),
            selectinload(Registration.ped),
        )
    )


async def _scan_payload(registration: Registration):
    students = []
    for student in registration.students:
        students.append({
            'id': student.id,
            'full_name': student.full_name,
            'email': student.email,
            'usn': student.usn,
            'semester': student.current_semester,
            'attendance_status': student.attendance_status,
            'photo_url': await storage.signed_url(settings.SUPABASE_BUCKET_STUDENT_PHOTOS, student.photo_path) if student.photo_path else None,
        })
    return {
        'registration_id': registration.id,
        'registration_code': registration.registration_code,
        'college_name': registration.college_name,
        'ped_name': registration.ped.name,
        'event': registration.event_config.sport_name,
        'event_type': registration.event_config.event_type,
        'category': registration.event_config.category,
        'payment_status': registration.payment_status,
        'approval_status': registration.status,
        'previous_check_in': registration.attendance_confirmed_at,
        'student_count': len(students),
        'students': students,
    }


@router.get('/scan')
async def scan(
    token: str,
    admin: Admin = Depends(require_admin_roles('ATTENDANCE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    payload = decode_token(token, 'registration_qr')
    registration = await _load_registration(db, payload.get('sub'))
    if not registration or registration.qr_token != token:
        raise HTTPException(status_code=404, detail='QR not found or revoked')
    if registration.status != 'APPROVED':
        raise HTTPException(status_code=409, detail=f'Registration status is {registration.status}')
    return await _scan_payload(registration)


@router.get('/registrations/{rid}')
async def lookup_registration(
    rid: str,
    admin: Admin = Depends(require_admin_roles('ATTENDANCE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    registration = await _load_registration(db, rid)
    if not registration:
        registration = await db.scalar(
            select(Registration)
            .where(Registration.registration_code == rid)
            .options(
                selectinload(Registration.students),
                selectinload(Registration.event_config),
                selectinload(Registration.ped),
            )
        )
    if not registration:
        raise HTTPException(status_code=404, detail='Registration not found')
    return await _scan_payload(registration)


@router.post('/registrations/{rid}/confirm', response_model=MessageResponse)
async def confirm(
    rid: str,
    payload: AttendanceConfirm,
    admin: Admin = Depends(require_admin_roles('ATTENDANCE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    registration = await _load_registration(db, rid)
    if not registration:
        raise HTTPException(status_code=404, detail='Registration not found')
    if registration.status != 'APPROVED':
        raise HTTPException(status_code=409, detail='Only approved registrations can check in')
    if registration.attendance_confirmed_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                'message': 'Attendance already confirmed. Duplicate scanning is blocked.',
                'checked_in_at': registration.attendance_confirmed_at.isoformat(),
            },
        )

    student_map = {student.id: student for student in registration.students}
    given = {item.student_id for item in payload.students}
    if given != set(student_map):
        raise HTTPException(
            status_code=422,
            detail={
                'message': 'Every roster student must be marked',
                'missing': list(set(student_map) - given),
                'invalid': list(given - set(student_map)),
            },
        )

    version = int(
        await db.scalar(
            select(func.max(AttendanceRecord.version)).where(AttendanceRecord.registration_id == registration.id)
        )
        or 0
    ) + 1
    now = utcnow()
    present = 0
    for item in payload.students:
        student = student_map[item.student_id]
        student.attendance_status = 'PRESENT' if item.is_present else 'ABSENT'
        student.attendance_note = item.note
        student.attendance_checked_at = now
        student.attendance_checked_by = admin.id
        present += int(item.is_present)
        db.add(
            AttendanceRecord(
                registration_id=registration.id,
                student_id=student.id,
                is_present=item.is_present,
                note=item.note,
                gate=payload.gate,
                admin_id=admin.id,
                version=version,
            )
        )
    registration.attendance_confirmed_at = now
    registration.attendance_confirmed_by = admin.id
    await audit(
        db,
        'ADMIN',
        admin.id,
        'CONFIRM_ATTENDANCE',
        'REGISTRATION',
        registration.id,
        payload.confirmation_note,
        {'version': version, 'present': present, 'absent': len(registration.students) - present, 'gate': payload.gate},
    )
    await db.commit()
    return MessageResponse(message=f'Attendance saved: {present} present, {len(registration.students) - present} absent')


@router.get('/registrations/{rid}/history')
async def history(
    rid: str,
    admin: Admin = Depends(require_admin_roles('ATTENDANCE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    return (
        await db.scalars(
            select(AttendanceRecord)
            .where(AttendanceRecord.registration_id == rid)
            .order_by(AttendanceRecord.version.desc(), AttendanceRecord.created_at.desc())
        )
    ).all()
