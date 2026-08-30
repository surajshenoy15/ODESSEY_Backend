import io
import mimetypes
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin, require_admin_roles
from app.core.security import create_qr_token, hash_password, utcnow
from app.models.entities import (
    Admin,
    AuditLog,
    Certificate,
    CertificateTemplate,
    EmailLog,
    EventConfig,
    Fixture,
    LiveStream,
    Ped,
    Registration,
    Student,
)
from app.schemas import (
    AdminCreate,
    AdminOut,
    AdminUpdate,
    DashboardStats,
    EventCreate,
    EventOut,
    EventUpdate,
    LiveStreamUpsert,
    MessageResponse,
    RegistrationOut,
    ReviewAction,
)
from app.services.email import approval_html, branded_email, email_service, fixture_html
from app.services.helpers import audit, qr_png_bytes
from app.services.storage import storage

router = APIRouter(prefix='/admin', tags=['Admin'])
IMG_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


@router.get('/dashboard', response_model=DashboardStats)
async def dashboard(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    async def count_reg(*conditions):
        return int(await db.scalar(select(func.count(Registration.id)).where(*conditions)) or 0)

    return DashboardStats(
        total_registrations=await count_reg(),
        paid_registrations=await count_reg(Registration.payment_status == 'PAID'),
        under_review=await count_reg(Registration.status == 'UNDER_REVIEW'),
        approved=await count_reg(Registration.status == 'APPROVED'),
        rejected=await count_reg(Registration.status == 'REJECTED'),
        attendance_verified=await count_reg(Registration.attendance_confirmed_at.is_not(None)),
        present_students=int(await db.scalar(select(func.count(Student.id)).where(Student.attendance_status == 'PRESENT')) or 0),
        certificates_published=int(await db.scalar(select(func.count(Certificate.id)).where(Certificate.status == 'PUBLISHED')) or 0),
    )


@router.post('/events', response_model=EventOut, status_code=201)
async def event_create(
    payload: EventCreate,
    admin: Admin = Depends(require_admin_roles('REGISTRATION_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    duplicate = await db.scalar(
        select(EventConfig).where(
            EventConfig.sport_name == payload.sport_name,
            EventConfig.category == payload.category,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail='Event/category already exists')
    event = EventConfig(**payload.model_dump())
    db.add(event)
    await db.flush()
    await audit(db, 'ADMIN', admin.id, 'CREATE_EVENT', 'EVENT_CONFIG', event.id)
    await db.commit()
    await db.refresh(event)
    return event


@router.get('/events', response_model=list[EventOut])
async def events(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return (
        await db.scalars(
            select(EventConfig).order_by(EventConfig.event_type, EventConfig.sport_name, EventConfig.category)
        )
    ).all()


@router.patch('/events/{eid}', response_model=EventOut)
async def event_update(
    eid: str,
    payload: EventUpdate,
    admin: Admin = Depends(require_admin_roles('REGISTRATION_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(EventConfig, eid)
    if not event:
        raise HTTPException(status_code=404, detail='Event not found')
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(event, key, value)
    await audit(db, 'ADMIN', admin.id, 'UPDATE_EVENT', 'EVENT_CONFIG', event.id, details={'fields': list(data)})
    await db.commit()
    await db.refresh(event)
    return event


@router.post('/events/{eid}/poster')
async def event_poster(
    eid: str,
    file: UploadFile = File(...),
    admin: Admin = Depends(require_admin_roles('REGISTRATION_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(EventConfig, eid)
    if not event:
        raise HTTPException(status_code=404, detail='Event not found')
    if file.content_type not in IMG_TYPES:
        raise HTTPException(status_code=415, detail='Poster must be JPG, PNG or WEBP')
    data = await file.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail='Poster exceeds 8 MB')
    ext = mimetypes.guess_extension(file.content_type) or '.jpg'
    path = f'{event.id}/poster{ext}'
    await storage.upload(settings.SUPABASE_BUCKET_EVENT_MEDIA, path, data, file.content_type)
    event.poster_path = path
    await audit(db, 'ADMIN', admin.id, 'UPLOAD_EVENT_POSTER', 'EVENT_CONFIG', event.id)
    await db.commit()
    return {'message': 'Event poster uploaded', 'poster_url': await storage.signed_url(settings.SUPABASE_BUCKET_EVENT_MEDIA, path)}


@router.delete('/events/{eid}', response_model=MessageResponse)
async def event_delete(
    eid: str,
    admin: Admin = Depends(require_admin_roles('REGISTRATION_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(EventConfig, eid)
    if not event:
        raise HTTPException(status_code=404, detail='Event not found')
    related = 0
    for model in (Registration, Fixture, LiveStream, CertificateTemplate):
        field = getattr(model, 'event_config_id')
        related += int(await db.scalar(select(func.count()).select_from(model).where(field == eid)) or 0)
    if related:
        raise HTTPException(status_code=409, detail='This event already has related records. Mark it inactive instead.')
    await audit(db, 'ADMIN', admin.id, 'DELETE_EVENT', 'EVENT_CONFIG', event.id)
    await db.delete(event)
    await db.commit()
    return MessageResponse(message='Event deleted successfully')


@router.get('/registrations', response_model=list[RegistrationOut])
async def registrations(
    status_filter: str | None = Query(None, alias='status'),
    payment_status: str | None = None,
    event_config_id: str | None = None,
    college: str | None = None,
    search: str | None = None,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(Registration).options(selectinload(Registration.students)).order_by(Registration.created_at.desc())
    if search:
        query = query.join(Ped, Registration.ped_id == Ped.id).where(
            or_(
                Registration.id == search,
                Registration.registration_code.ilike(f'%{search}%'),
                Registration.college_name.ilike(f'%{search}%'),
                Ped.official_email.ilike(f'%{search}%'),
            )
        )
    if status_filter:
        query = query.where(Registration.status == status_filter)
    if payment_status:
        query = query.where(Registration.payment_status == payment_status)
    if event_config_id:
        query = query.where(Registration.event_config_id == event_config_id)
    if college:
        query = query.where(Registration.college_name.ilike(f'%{college}%'))
    return (await db.scalars(query)).all()


@router.get('/registrations/{rid}')
async def registration_detail(
    rid: str,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == rid)
        .options(
            selectinload(Registration.students),
            selectinload(Registration.event_config),
            selectinload(Registration.ped),
            selectinload(Registration.payments),
        )
    )
    if not registration:
        raise HTTPException(status_code=404, detail='Registration not found')
    students = []
    for student in registration.students:
        students.append({
            'id': student.id,
            'full_name': student.full_name,
            'email': student.email,
            'usn': student.usn,
            'semester': student.current_semester,
            'contact_number': student.contact_number,
            'attendance_status': student.attendance_status,
            'certificate_override': student.certificate_override,
            'certificate_override_reason': student.certificate_override_reason,
            'photo_url': await storage.signed_url(settings.SUPABASE_BUCKET_STUDENT_PHOTOS, student.photo_path) if student.photo_path else None,
        })
    return {
        'id': registration.id,
        'registration_code': registration.registration_code,
        'college_name': registration.college_name,
        'college_location': registration.college_location,
        'student_coordinator_name': registration.student_coordinator_name,
        'student_coordinator_contact': registration.student_coordinator_contact,
        'ped': {
            'name': registration.ped.name,
            'email': registration.ped.official_email,
            'contact': registration.ped_contact,
        },
        'event': {
            'id': registration.event_config.id,
            'sport_name': registration.event_config.sport_name,
            'event_type': registration.event_config.event_type,
            'category': registration.event_config.category,
        },
        'status': registration.status,
        'payment_status': registration.payment_status,
        'fee_paise': registration.fee_paise,
        'admin_note': registration.admin_note,
        'correction_fields': registration.correction_fields,
        'bonafide_url': await storage.signed_url(settings.SUPABASE_BUCKET_BONAFIDES, registration.bonafide_path) if registration.bonafide_path else None,
        'students': students,
        'payments': [
            {
                'order_id': payment.order_id,
                'payment_id': payment.payment_id,
                'status': payment.status,
                'amount_paise': payment.amount_paise,
                'paid_at': payment.paid_at,
            }
            for payment in registration.payments
        ],
        'qr_token': registration.qr_token if registration.status == 'APPROVED' else None,
        'attendance_confirmed_at': registration.attendance_confirmed_at,
    }


async def _send_approval_emails(db: AsyncSession, registration: Registration):
    qr = qr_png_bytes(registration.qr_token)
    dashboard_url = f"{settings.PUBLIC_APP_URL.rstrip('/')}/#/ped"
    html = approval_html(registration, registration.event_config, dashboard_url)
    attachment = [(f'{registration.registration_code}-QR.png', qr)]
    recipients = {registration.ped.official_email}
    recipients.update(student.email for student in registration.students if student.email)
    for email in recipients:
        await email_service.send(
            db,
            email,
            f'BNMIT ODYSSEY registration approved — {registration.registration_code}',
            html,
            'REGISTRATION_APPROVED',
            registration.id,
            attachment,
        )
    return len(recipients)


@router.post('/registrations/{rid}/review', response_model=MessageResponse)
async def review(
    rid: str,
    payload: ReviewAction,
    admin: Admin = Depends(require_admin_roles('REGISTRATION_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    registration = await db.scalar(
        select(Registration)
        .where(Registration.id == rid)
        .options(
            selectinload(Registration.event_config),
            selectinload(Registration.ped),
            selectinload(Registration.students),
        )
    )
    if not registration:
        raise HTTPException(status_code=404, detail='Registration not found')

    if payload.action == 'APPROVE':
        if registration.status != 'UNDER_REVIEW':
            raise HTTPException(status_code=409, detail='Only registrations under review can be approved')
        if registration.payment_status != 'PAID':
            raise HTTPException(status_code=409, detail='Only paid registrations can be approved')
        if not registration.event_config.team_min_size <= len(registration.students) <= registration.event_config.team_max_size:
            raise HTTPException(status_code=422, detail='Team size is outside configured limits')
        if not registration.bonafide_path or any(not student.photo_path for student in registration.students):
            raise HTTPException(status_code=422, detail='Bonafide or student photographs are incomplete')
        if any(not student.email for student in registration.students):
            raise HTTPException(status_code=422, detail='Every student must have an email address so the QR can be delivered')
        registration.status = 'APPROVED'
        registration.admin_note = payload.reason
        registration.correction_fields = None
        registration.approved_by = admin.id
        registration.approved_at = utcnow()
        registration.qr_token = create_qr_token(registration.id)
        recipient_count = await _send_approval_emails(db, registration)
        response_message = f'Registration approved and QR emailed to {recipient_count} recipient(s)'
    elif payload.action == 'REQUEST_CORRECTION':
        if not payload.reason or not payload.correction_fields:
            raise HTTPException(status_code=422, detail='Reason and correction_fields are required')
        registration.status = 'CORRECTION_REQUIRED'
        registration.admin_note = payload.reason
        registration.correction_fields = payload.correction_fields
        html = branded_email(
            'Registration correction required',
            f'<p>{payload.reason}</p><p><strong>Fields to correct:</strong> {", ".join(payload.correction_fields)}</p>',
        )
        await email_service.send(db, registration.ped.official_email, f'Correction required — {registration.registration_code}', html, 'CORRECTION_REQUIRED', registration.id)
        response_message = 'Correction request sent to PED'
    elif payload.action == 'REJECT':
        if not payload.reason:
            raise HTTPException(status_code=422, detail='Reason required')
        registration.status = 'REJECTED'
        registration.admin_note = payload.reason
        registration.qr_token = None
        html = branded_email('Registration not approved', f'<p>{payload.reason}</p>')
        await email_service.send(db, registration.ped.official_email, f'Registration update — {registration.registration_code}', html, 'REGISTRATION_REJECTED', registration.id)
        response_message = 'Registration rejected and PED notified'
    elif payload.action == 'REOPEN':
        registration.status = 'CORRECTION_REQUIRED'
        registration.admin_note = payload.reason or 'Reopened by admin'
        registration.correction_fields = payload.correction_fields or [
            'college_name', 'team_name', 'coach_name', 'ped_contact',
            'student_coordinator_name', 'student_coordinator_contact', 'students',
            'student_photos', 'bonafide', 'declaration',
        ]
        registration.qr_token = None
        registration.approved_at = None
        registration.approved_by = None
        response_message = 'Registration reopened for correction'
    else:
        raise HTTPException(status_code=422, detail='Unsupported review action')

    await audit(
        db,
        'ADMIN',
        admin.id,
        f'REGISTRATION_{payload.action}',
        'REGISTRATION',
        registration.id,
        payload.reason,
        {'correction_fields': payload.correction_fields},
    )
    await db.commit()
    return MessageResponse(message=response_message)


@router.get('/registrations/{rid}/qr.png')
async def qr(
    rid: str,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    registration = await db.get(Registration, rid)
    if not registration or registration.status != 'APPROVED' or not registration.qr_token:
        raise HTTPException(status_code=404, detail='QR unavailable')
    return StreamingResponse(
        io.BytesIO(qr_png_bytes(registration.qr_token)),
        media_type='image/png',
        headers={'Content-Disposition': f'inline; filename={registration.registration_code}-QR.png'},
    )


@router.get('/live-streams')
async def live_streams(
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return (await db.scalars(select(LiveStream).order_by(LiveStream.updated_at.desc()))).all()


@router.post('/live-streams')
async def livestream(
    payload: LiveStreamUpsert,
    admin: Admin = Depends(require_admin_roles('FIXTURE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    stream = await db.scalar(select(LiveStream).where(LiveStream.event_config_id == payload.event_config_id)) if payload.event_config_id else None
    data = payload.model_dump()
    data['youtube_url'] = str(data['youtube_url'])
    if not stream:
        stream = LiveStream(updated_by=admin.id, **data)
        db.add(stream)
        await db.flush()
    else:
        for key, value in data.items():
            setattr(stream, key, value)
        stream.updated_by = admin.id
    await audit(db, 'ADMIN', admin.id, 'UPSERT_LIVE_STREAM', 'LIVE_STREAM', stream.id)
    await db.commit()
    await db.refresh(stream)
    return stream


@router.get('/fixtures')
async def fixtures(
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.scalars(
            select(Fixture).options(selectinload(Fixture.event_config)).order_by(Fixture.created_at.desc())
        )
    ).all()
    return [
        {
            'id': item.id,
            'event_config_id': item.event_config_id,
            'event_name': item.event_config.sport_name if item.event_config else 'General',
            'category': item.event_config.category if item.event_config else 'All',
            'title': item.title,
            'note': item.note,
            'version': item.version,
            'visibility': item.visibility,
            'status': item.status,
            'published_at': item.published_at,
            'supersedes_id': item.supersedes_id,
            'download_url': await storage.signed_url(settings.SUPABASE_BUCKET_FIXTURES, item.file_path),
        }
        for item in rows
    ]


@router.post('/fixtures', status_code=201)
async def fixture_upload(
    title: str = Form(...),
    event_config_id: str | None = Form(None),
    note: str | None = Form(None),
    visibility: str = Form('RELEVANT_PEDS'),
    supersedes_id: str | None = Form(None),
    file: UploadFile = File(...),
    admin: Admin = Depends(require_admin_roles('FIXTURE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    if visibility not in {'PUBLIC', 'RELEVANT_PEDS', 'ALL_PEDS'}:
        raise HTTPException(status_code=422, detail='Invalid fixture visibility')
    if file.content_type not in {'application/pdf', 'image/jpeg', 'image/png'}:
        raise HTTPException(status_code=415, detail='Fixture must be PDF, JPG or PNG')
    if event_config_id and not await db.get(EventConfig, event_config_id):
        raise HTTPException(status_code=404, detail='Event not found')
    data = await file.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail='Fixture exceeds 15 MB')
    previous = await db.get(Fixture, supersedes_id) if supersedes_id else None
    version = previous.version + 1 if previous else 1
    ext = mimetypes.guess_extension(file.content_type) or '.pdf'
    path = f'{event_config_id or "general"}/{uuid.uuid4().hex}{ext}'
    await storage.upload(settings.SUPABASE_BUCKET_FIXTURES, path, data, file.content_type)
    fixture = Fixture(
        event_config_id=event_config_id,
        title=title,
        note=note,
        version=version,
        file_path=path,
        visibility=visibility,
        uploaded_by=admin.id,
        supersedes_id=supersedes_id,
    )
    db.add(fixture)
    await db.flush()
    await audit(db, 'ADMIN', admin.id, 'UPLOAD_FIXTURE', 'FIXTURE', fixture.id)
    await db.commit()
    return {'id': fixture.id, 'version': fixture.version, 'status': fixture.status}


@router.post('/fixtures/{fid}/publish', response_model=MessageResponse)
async def fixture_publish(
    fid: str,
    admin: Admin = Depends(require_admin_roles('FIXTURE_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    fixture = await db.scalar(
        select(Fixture).where(Fixture.id == fid).options(selectinload(Fixture.event_config))
    )
    if not fixture:
        raise HTTPException(status_code=404, detail='Fixture not found')
    if fixture.supersedes_id:
        old = await db.get(Fixture, fixture.supersedes_id)
        if old:
            old.status = 'SUPERSEDED'
    fixture.status = 'PUBLISHED'
    fixture.published_at = utcnow()

    query = select(Ped.official_email).join(Registration, Registration.ped_id == Ped.id)
    if fixture.visibility == 'RELEVANT_PEDS' and fixture.event_config_id:
        query = query.where(Registration.event_config_id == fixture.event_config_id)
    recipients = set((await db.scalars(query)).all())
    event_name = fixture.event_config.sport_name if fixture.event_config else 'BNMIT ODYSSEY'
    category = fixture.event_config.category if fixture.event_config else 'All participants'
    for email in recipients:
        await email_service.send(
            db,
            email,
            f'Fixture published — {event_name} · {category}',
            fixture_html(
                event_name,
                category,
                fixture.title,
                fixture.version,
                f"{settings.PUBLIC_APP_URL.rstrip('/')}/#/ped",
                fixture.note,
            ),
            'FIXTURE_PUBLISHED',
        )
    await audit(db, 'ADMIN', admin.id, 'PUBLISH_FIXTURE', 'FIXTURE', fixture.id, details={'recipient_count': len(recipients)})
    await db.commit()
    return MessageResponse(message=f'Fixture published and {len(recipients)} PED(s) notified')


@router.get('/users', response_model=list[AdminOut])
async def list_admin_users(
    admin: Admin = Depends(require_admin_roles('SUPER_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    return (await db.scalars(select(Admin).order_by(Admin.created_at))).all()


@router.post('/users', response_model=AdminOut, status_code=201)
async def create_admin_user(
    payload: AdminCreate,
    admin: Admin = Depends(require_admin_roles('SUPER_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    email = payload.email.lower()
    if await db.scalar(select(Admin).where(Admin.email == email)):
        raise HTTPException(status_code=409, detail='Admin email already exists')
    user = Admin(name=payload.name, email=email, password_hash=hash_password(payload.password), role=payload.role)
    db.add(user)
    await db.flush()
    await audit(db, 'ADMIN', admin.id, 'CREATE_ADMIN', 'ADMIN', user.id)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch('/users/{admin_id}', response_model=AdminOut)
async def update_admin_user(
    admin_id: str,
    payload: AdminUpdate,
    admin: Admin = Depends(require_admin_roles('SUPER_ADMIN')),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(Admin, admin_id)
    if not user:
        raise HTTPException(status_code=404, detail='Admin not found')
    data = payload.model_dump(exclude_unset=True)
    password = data.pop('password', None)
    new_email = data.pop('email', None)
    if new_email is not None:
        normalized = str(new_email).strip().lower()
        existing = await db.scalar(select(Admin).where(Admin.email == normalized, Admin.id != admin_id))
        if existing:
            raise HTTPException(status_code=409, detail='Another admin already uses this email address')
        user.email = normalized
    for key, value in data.items():
        setattr(user, key, value)
    if password:
        user.password_hash = hash_password(password)
    await audit(db, 'ADMIN', admin.id, 'UPDATE_ADMIN', 'ADMIN', user.id, details={'fields': list(payload.model_dump(exclude_unset=True))})
    await db.commit()
    await db.refresh(user)
    return user


@router.get('/audit-logs')
async def audit_logs(
    action: str | None = None,
    entity_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        query = query.where(AuditLog.action == action)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    return (await db.scalars(query)).all()


@router.get('/email-logs')
async def email_logs(
    message_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(EmailLog).order_by(EmailLog.created_at.desc()).limit(limit)
    if message_type:
        query = query.where(EmailLog.message_type == message_type)
    return (await db.scalars(query)).all()
