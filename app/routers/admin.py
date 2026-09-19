import logging
import io
import mimetypes
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)

from fastapi.responses import StreamingResponse

from sqlalchemy import (
    func,
    or_,
    select,
)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import (
    get_current_admin,
    require_admin_roles,
)
from app.core.security import (
    create_coordinator_qr_token,
    create_qr_token,
    hash_password,
    utcnow,
)

from app.models.entities import (
    Admin,
    AttendanceRecord,
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

from app.services.email import (
    approval_html,
    branded_email,
    email_service,
    fixture_html,
)

from app.services.helpers import (
    audit,
    qr_png_bytes,
)

from app.services.storage import storage


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix='/admin',
    tags=['Admin'],
)


IMG_TYPES = {
    'image/jpeg',
    'image/png',
    'image/webp',
}


# ============================================================
# HELPERS
# ============================================================

async def safe_signed_url(
    bucket: str,
    path: str | None,
):
    """
    Generate a signed storage URL without allowing one missing
    or invalid object to break the entire admin detail response.

    Upload/download operations remain strict. This helper is only
    used when a URL is optional presentation data.
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


def coordinator_emails(
    ped: Ped | None,
) -> set[str]:
    """
    Return all authorised coordinator emails for one
    college/PED account.

    Includes:
    - PED official email
    - Coach email
    - Manager email
    """

    if not ped:
        return set()

    emails = {
        email.strip().lower()
        for email in (
            ped.official_email,
            ped.coach_email,
            ped.manager_email,
        )
        if email
    }

    return emails


async def send_to_coordinators(
    db: AsyncSession,
    registration: Registration,
    subject: str,
    html: str,
    message_type: str,
):
    """
    Send one email to all authorised coordinators attached
    to the registration's common PED account.
    """

    recipients = coordinator_emails(
        registration.ped
    )

    for email in recipients:
        await email_service.send(
            db,
            email,
            subject,
            html,
            message_type,
            registration.id,
        )

    return len(recipients)


# ============================================================
# DASHBOARD
# ============================================================

@router.get(
    '/dashboard',
    response_model=DashboardStats,
)
async def dashboard(
    admin: Admin = Depends(
        get_current_admin
    ),
    db: AsyncSession = Depends(
        get_db
    ),
):
    async def count_reg(
        *conditions
    ):
        return int(
            await db.scalar(
                select(
                    func.count(
                        Registration.id
                    )
                ).where(
                    Registration.status != 'CANCELLED',
                    *conditions
                )
            )
            or 0
        )

    return DashboardStats(
        total_registrations=
            await count_reg(),

        paid_registrations=
            await count_reg(
                Registration.payment_status
                == 'PAID'
            ),

        under_review=
            await count_reg(
                Registration.status
                == 'UNDER_REVIEW'
            ),

        approved=
            await count_reg(
                Registration.status
                == 'APPROVED'
            ),

        rejected=
            await count_reg(
                Registration.status
                == 'REJECTED'
            ),

        attendance_verified=
            await count_reg(
                Registration
                .attendance_confirmed_at
                .is_not(None)
            ),

        present_students=int(
            await db.scalar(
                select(
                    func.count(
                        Student.id
                    )
                ).where(
                    Student.attendance_status
                    == 'PRESENT'
                )
            )
            or 0
        ),

        certificates_published=int(
            await db.scalar(
                select(
                    func.count(
                        Certificate.id
                    )
                ).where(
                    Certificate.status
                    == 'PUBLISHED'
                )
            )
            or 0
        ),
    )


# ============================================================
# EVENTS
# ============================================================

@router.post(
    '/events',
    response_model=EventOut,
    status_code=201,
)
async def event_create(
    payload: EventCreate,

    admin: Admin = Depends(
        require_admin_roles(
            'REGISTRATION_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    duplicate = await db.scalar(
        select(
            EventConfig
        ).where(
            EventConfig.sport_name
            == payload.sport_name,

            EventConfig.category
            == payload.category,
        )
    )

    if duplicate:
        raise HTTPException(
            status_code=409,
            detail=(
                'Event/category already exists'
            ),
        )

    event = EventConfig(
        **payload.model_dump()
    )

    db.add(
        event
    )

    await db.flush()

    await audit(
        db,
        'ADMIN',
        admin.id,
        'CREATE_EVENT',
        'EVENT_CONFIG',
        event.id,
    )

    await db.commit()

    await db.refresh(
        event
    )

    return event


@router.get(
    '/events',
    response_model=list[EventOut],
)
async def events(
    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    return (
        await db.scalars(
            select(
                EventConfig
            ).order_by(
                EventConfig.event_type,
                EventConfig.sport_name,
                EventConfig.category,
            )
        )
    ).all()


@router.patch(
    '/events/{eid}',
    response_model=EventOut,
)
async def event_update(
    eid: str,
    payload: EventUpdate,

    admin: Admin = Depends(
        require_admin_roles(
            'REGISTRATION_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    event = await db.get(
        EventConfig,
        eid,
    )

    if not event:
        raise HTTPException(
            status_code=404,
            detail='Event not found',
        )

    data = payload.model_dump(
        exclude_unset=True
    )

    for key, value in data.items():
        setattr(
            event,
            key,
            value,
        )

    await audit(
        db,
        'ADMIN',
        admin.id,
        'UPDATE_EVENT',
        'EVENT_CONFIG',
        event.id,
        details={
            'fields': list(data)
        },
    )

    await db.commit()

    await db.refresh(
        event
    )

    return event


@router.post(
    '/events/{eid}/poster'
)
async def event_poster(
    eid: str,

    file: UploadFile = File(
        ...
    ),

    admin: Admin = Depends(
        require_admin_roles(
            'REGISTRATION_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    event = await db.get(
        EventConfig,
        eid,
    )

    if not event:
        raise HTTPException(
            status_code=404,
            detail='Event not found',
        )

    if (
        file.content_type
        not in IMG_TYPES
    ):
        raise HTTPException(
            status_code=415,
            detail=(
                'Poster must be JPG, '
                'PNG or WEBP'
            ),
        )

    data = await file.read(
        8 * 1024 * 1024 + 1
    )

    if (
        len(data)
        > 8 * 1024 * 1024
    ):
        raise HTTPException(
            status_code=413,
            detail='Poster exceeds 8 MB',
        )

    ext = (
        mimetypes.guess_extension(
            file.content_type
        )
        or '.jpg'
    )

    path = (
        f'{event.id}/'
        f'poster{ext}'
    )

    await storage.upload(
        settings
        .SUPABASE_BUCKET_EVENT_MEDIA,

        path,

        data,

        file.content_type,
    )

    event.poster_path = path

    await audit(
        db,
        'ADMIN',
        admin.id,
        'UPLOAD_EVENT_POSTER',
        'EVENT_CONFIG',
        event.id,
    )

    await db.commit()

    return {
        'message':
            'Event poster uploaded',

        'poster_url':
            await storage.signed_url(
                settings
                .SUPABASE_BUCKET_EVENT_MEDIA,

                path,
            ),
    }


@router.delete(
    '/events/{eid}',
    response_model=MessageResponse,
)
async def event_delete(
    eid: str,

    admin: Admin = Depends(
        require_admin_roles(
            'REGISTRATION_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    event = await db.get(
        EventConfig,
        eid,
    )

    if not event:
        raise HTTPException(
            status_code=404,
            detail='Event not found',
        )

    related = 0

    for model in (
        Registration,
        Fixture,
        LiveStream,
        CertificateTemplate,
    ):
        field = getattr(
            model,
            'event_config_id',
        )

        related += int(
            await db.scalar(
                select(
                    func.count()
                )
                .select_from(
                    model
                )
                .where(
                    field == eid
                )
            )
            or 0
        )

    if related:
        raise HTTPException(
            status_code=409,
            detail=(
                'This event already has related records. '
                'Mark it inactive instead.'
            ),
        )

    await audit(
        db,
        'ADMIN',
        admin.id,
        'DELETE_EVENT',
        'EVENT_CONFIG',
        event.id,
    )

    await db.delete(
        event
    )

    await db.commit()

    return MessageResponse(
        message=(
            'Event deleted successfully'
        )
    )


# ============================================================
# REGISTRATIONS LIST
# ============================================================

@router.get(
    '/registrations',
    response_model=list[
        RegistrationOut
    ],
)
async def registrations(
    status_filter:
        str | None = Query(
            None,
            alias='status',
        ),

    payment_status:
        str | None = None,

    event_config_id:
        str | None = None,

    college:
        str | None = None,

    search:
        str | None = None,

    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    query = (
        select(
            Registration
        )
        .options(
            selectinload(
                Registration.students
            )
        )
        .order_by(
            Registration.created_at.desc()
        )
    )

    # ========================================================
    # SEARCH
    #
    # Search now supports:
    # - registration id
    # - registration code
    # - college
    # - PED email/name
    # - Coach email/name
    # - Manager email/name
    # ========================================================

    if search:
        search_value = (
            search.strip()
        )

        query = (
            query
            .join(
                Ped,
                Registration.ped_id
                == Ped.id,
            )
            .where(
                or_(
                    Registration.id
                    == search_value,

                    Registration
                    .registration_code
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Registration
                    .college_name
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Ped.official_email
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Ped.name
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Ped.coach_email
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Ped.coach_name
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Ped.manager_email
                    .ilike(
                        f'%{search_value}%'
                    ),

                    Ped.manager_name
                    .ilike(
                        f'%{search_value}%'
                    ),
                )
            )
        )

    if status_filter:
        query = query.where(
            Registration.status
            == status_filter
        )
    else:
        # Soft-deleted registrations stay in the database for
        # audit/payment history, but are hidden from the normal
        # operational list. Admins can explicitly filter by
        # CANCELLED when they need to inspect removed records.
        query = query.where(
            Registration.status
            != 'CANCELLED'
        )

    if payment_status:
        query = query.where(
            Registration.payment_status
            == payment_status
        )

    if event_config_id:
        query = query.where(
            Registration.event_config_id
            == event_config_id
        )

    if college:
        query = query.where(
            Registration.college_name
            .ilike(
                f'%{college}%'
            )
        )

    return (
        await db.scalars(
            query
        )
    ).all()


# ============================================================
# REGISTRATION DETAIL
# ============================================================

@router.get(
    '/registrations/{rid}'
)
async def registration_detail(
    rid: str,

    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await db.scalar(
        select(
            Registration
        )
        .where(
            Registration.id == rid
        )
        .options(
            selectinload(
                Registration.students
            ),

            selectinload(
                Registration.event_config
            ),

            selectinload(
                Registration.ped
            ),

            selectinload(
                Registration.payments
            ),
        )
    )

    if not registration:
        raise HTTPException(
            status_code=404,
            detail=(
                'Registration not found'
            ),
        )


    # ========================================================
    # STUDENTS
    # ========================================================

    students = []

    for student in (
        registration.students
    ):
        students.append({
            'id':
                student.id,

            'full_name':
                student.full_name,

            'email':
                student.email,

            'usn':
                student.usn,

            'semester':
                student.current_semester,

            'contact_number':
                student.contact_number,

            'attendance_status':
                student.attendance_status,

            'certificate_override':
                student.certificate_override,

            'certificate_override_reason':
                student.certificate_override_reason,

            'photo_url':
                await safe_signed_url(
                    settings
                    .SUPABASE_BUCKET_STUDENT_PHOTOS,

                    student.photo_path,
                ),
        })


    # ========================================================
    # RESPONSE
    # ========================================================

    return {
        'id':
            registration.id,

        'registration_code':
            registration.registration_code,

        'college_name':
            registration.college_name,

        'college_location':
            registration.college_location,

        'team_name':
            registration.team_name,

        'coach_name':
            registration.coach_name,

        'student_coordinator_name':
            registration
            .student_coordinator_name,

        'student_coordinator_contact':
            registration
            .student_coordinator_contact,


        # ====================================================
        # PED / COACH / MANAGER
        # ====================================================

        'ped': {

            # Primary PED
            'name':
                registration.ped.name,

            'email':
                registration
                .ped
                .official_email,

            'contact':
                registration
                .ped_contact,


            # Coach
            'coach_name':
                registration
                .ped
                .coach_name,

            'coach_email':
                registration
                .ped
                .coach_email,

            'coach_contact':
                registration
                .ped
                .coach_contact_number,


            # Manager
            'manager_name':
                registration
                .ped
                .manager_name,

            'manager_email':
                registration
                .ped
                .manager_email,

            'manager_contact':
                registration
                .ped
                .manager_contact_number,
        },


        'event': {
            'id':
                registration
                .event_config
                .id,

            'sport_name':
                registration
                .event_config
                .sport_name,

            'event_type':
                registration
                .event_config
                .event_type,

            'category':
                registration
                .event_config
                .category,
        },


        'status':
            registration.status,

        'payment_status':
            registration.payment_status,

        'fee_paise':
            registration.fee_paise,

        'admin_note':
            registration.admin_note,

        'correction_fields':
            registration.correction_fields,


        'bonafide_url':
            await safe_signed_url(
                settings
                .SUPABASE_BUCKET_BONAFIDES,

                registration
                .bonafide_path,
            ),


        'students':
            students,


        'payments': [
            {
                'order_id':
                    payment.order_id,

                'payment_id':
                    payment.payment_id,

                'status':
                    payment.status,

                'amount_paise':
                    payment.amount_paise,

                'paid_at':
                    payment.paid_at,
            }

            for payment
            in registration.payments
        ],


        'qr_token':
            (
                registration.qr_token

                if registration.status
                == 'APPROVED'

                else None
            ),


        'attendance_confirmed_at':
            registration
            .attendance_confirmed_at,
    }


# ============================================================
# REMOVE REGISTRATION (SAFE SOFT DELETE)
# ============================================================

@router.delete(
    '/registrations/{rid}',
    response_model=MessageResponse,
)
async def delete_registration(
    rid: str,

    admin: Admin = Depends(
        require_admin_roles(
            'REGISTRATION_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    """
    Remove a registration from active operations without
    physically deleting payment/audit history.

    The registration is moved to CANCELLED and every current QR
    is revoked by clearing registration.qr_token.

    Registrations that already have attendance or certificates
    are protected from deletion because those records are part
    of event-day / certification history.
    """

    registration = await db.scalar(
        select(
            Registration
        )
        .where(
            Registration.id == rid
        )
    )

    if not registration:
        raise HTTPException(
            status_code=404,
            detail='Registration not found',
        )

    if registration.status == 'CANCELLED':
        raise HTTPException(
            status_code=409,
            detail='Registration is already deleted',
        )

    attendance_count = int(
        await db.scalar(
            select(
                func.count()
            )
            .select_from(
                AttendanceRecord
            )
            .where(
                AttendanceRecord.registration_id
                == registration.id
            )
        )
        or 0
    )

    certificate_count = int(
        await db.scalar(
            select(
                func.count()
            )
            .select_from(
                Certificate
            )
            .where(
                Certificate.registration_id
                == registration.id
            )
        )
        or 0
    )

    if attendance_count or certificate_count:
        raise HTTPException(
            status_code=409,
            detail={
                'message': (
                    'This registration cannot be deleted because '
                    'attendance or certificate history already exists.'
                ),
                'attendance_records': attendance_count,
                'certificates': certificate_count,
            },
        )

    previous_status = registration.status
    previous_qr_active = bool(
        registration.qr_token
    )

    registration.status = 'CANCELLED'

    # Revokes the generic registration QR and every role-specific
    # PED / Coach / Manager QR derived from the current master.
    registration.qr_token = None

    removal_note = (
        'Registration removed from active records by admin.'
    )

    if registration.admin_note:
        registration.admin_note = (
            f'{registration.admin_note}\n\n{removal_note}'
        )
    else:
        registration.admin_note = removal_note

    await audit(
        db,
        'ADMIN',
        admin.id,
        'DELETE_REGISTRATION',
        'REGISTRATION',
        registration.id,
        details={
            'soft_delete': True,
            'previous_status': previous_status,
            'payment_status': registration.payment_status,
            'qr_revoked': previous_qr_active,
        },
    )

    await db.commit()

    return MessageResponse(
        message=(
            'Registration deleted from active records successfully'
        )
    )


# ============================================================
# APPROVAL EMAILS
# ============================================================

async def _send_approval_emails(
    db: AsyncSession,
    registration: Registration,
):
    """
    Send different attendance QR codes to each coordinator.

    PED
        -> PED-specific QR

    Coach
        -> Coach-specific QR

    Manager
        -> Manager-specific QR

    Students
        -> existing generic registration QR

    IMPORTANT:
    Coordinator QR tokens are linked to registration.qr_token
    through the QR fingerprint/version.

    Therefore:
    - reject/reopen -> registration.qr_token = None
      -> all coordinator QRs become invalid

    - reapprove -> new registration.qr_token
      -> new coordinator QRs are required
    """

    if not registration.qr_token:
        raise HTTPException(
            status_code=409,
            detail='Registration QR token has not been generated',
        )

    if not registration.ped:
        raise HTTPException(
            status_code=409,
            detail='PED/coordinator account is missing',
        )

    dashboard_url = (
        f"{settings.PUBLIC_APP_URL.rstrip('/')}"
        f"/#/ped"
    )

    html = approval_html(
        registration,
        registration.event_config,
        dashboard_url,
    )

    sent_count = 0


    # ========================================================
    # PED QR
    # ========================================================

    ped_email = (
        registration.ped.official_email
        .strip()
        .lower()
        if registration.ped.official_email
        else None
    )

    if ped_email:

        ped_token = create_coordinator_qr_token(
            registration.id,
            'PED',
            registration.qr_token,
            ped_email,
        )

        ped_qr = qr_png_bytes(
            ped_token
        )

        ped_attachment = [
            (
                (
                    f'{registration.registration_code}'
                    f'-PED-QR.png'
                ),
                ped_qr,
            )
        ]

        await email_service.send(
            db,
            ped_email,

            (
                'BNMIT ODYSSEY registration approved '
                '— PED attendance QR — '
                f'{registration.registration_code}'
            ),

            html,

            'REGISTRATION_APPROVED',

            registration.id,

            ped_attachment,
        )

        sent_count += 1


    # ========================================================
    # COACH QR
    # ========================================================

    coach_email = (
        registration.ped.coach_email
        .strip()
        .lower()
        if registration.ped.coach_email
        else None
    )

    if coach_email:

        coach_token = create_coordinator_qr_token(
            registration.id,
            'COACH',
            registration.qr_token,
            coach_email,
        )

        coach_qr = qr_png_bytes(
            coach_token
        )

        coach_attachment = [
            (
                (
                    f'{registration.registration_code}'
                    f'-COACH-QR.png'
                ),
                coach_qr,
            )
        ]

        await email_service.send(
            db,
            coach_email,

            (
                'BNMIT ODYSSEY registration approved '
                '— Coach attendance QR — '
                f'{registration.registration_code}'
            ),

            html,

            'REGISTRATION_APPROVED',

            registration.id,

            coach_attachment,
        )

        sent_count += 1


    # ========================================================
    # MANAGER QR
    # ========================================================

    manager_email = (
        registration.ped.manager_email
        .strip()
        .lower()
        if registration.ped.manager_email
        else None
    )

    if manager_email:

        manager_token = create_coordinator_qr_token(
            registration.id,
            'MANAGER',
            registration.qr_token,
            manager_email,
        )

        manager_qr = qr_png_bytes(
            manager_token
        )

        manager_attachment = [
            (
                (
                    f'{registration.registration_code}'
                    f'-MANAGER-QR.png'
                ),
                manager_qr,
            )
        ]

        await email_service.send(
            db,
            manager_email,

            (
                'BNMIT ODYSSEY registration approved '
                '— Manager attendance QR — '
                f'{registration.registration_code}'
            ),

            html,

            'REGISTRATION_APPROVED',

            registration.id,

            manager_attachment,
        )

        sent_count += 1


    # ========================================================
    # STUDENT GENERIC REGISTRATION QR
    # ========================================================

    generic_qr = qr_png_bytes(
        registration.qr_token
    )

    student_attachment = [
        (
            (
                f'{registration.registration_code}'
                f'-Registration-QR.png'
            ),
            generic_qr,
        )
    ]


    # ========================================================
    # STUDENT EMAILS
    #
    # Students retain the legacy/generic registration QR.
    #
    # This QR validates the registration but DOES NOT claim
    # that the holder is the PED, Coach or Manager.
    # ========================================================

    student_emails = {
        student.email
        .strip()
        .lower()

        for student
        in registration.students

        if student.email
    }


    for email in student_emails:

        await email_service.send(
            db,
            email,

            (
                'BNMIT ODYSSEY registration approved — '
                f'{registration.registration_code}'
            ),

            html,

            'REGISTRATION_APPROVED',

            registration.id,

            student_attachment,
        )

        sent_count += 1


    return sent_count


# ============================================================
# REVIEW REGISTRATION
# ============================================================

@router.post(
    '/registrations/{rid}/review',
    response_model=MessageResponse,
)
async def review(
    rid: str,
    payload: ReviewAction,

    admin: Admin = Depends(
        require_admin_roles(
            'REGISTRATION_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await db.scalar(
        select(
            Registration
        )
        .where(
            Registration.id == rid
        )
        .options(
            selectinload(
                Registration.event_config
            ),

            selectinload(
                Registration.ped
            ),

            selectinload(
                Registration.students
            ),
        )
    )


    if not registration:
        raise HTTPException(
            status_code=404,
            detail=(
                'Registration not found'
            ),
        )


    # ========================================================
    # APPROVE
    # ========================================================

    if (
        payload.action ==
        'APPROVE'
    ):

        if (
            registration.status
            != 'UNDER_REVIEW'
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    'Only registrations under review '
                    'can be approved'
                ),
            )


        if (
            registration.payment_status
            != 'PAID'
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    'Only paid registrations '
                    'can be approved'
                ),
            )


        if not (
            registration
            .event_config
            .team_min_size

            <=
            len(
                registration.students
            )

            <=

            registration
            .event_config
            .team_max_size
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    'Team size is outside '
                    'configured limits'
                ),
            )


        if (
            not registration
            .bonafide_path

            or any(
                not student.photo_path

                for student
                in registration.students
            )
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    'Bonafide or student '
                    'photographs are incomplete'
                ),
            )


        if any(
            not student.email

            for student
            in registration.students
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    'Every student must have an '
                    'email address so the QR '
                    'can be delivered'
                ),
            )


        registration.status = (
            'APPROVED'
        )

        registration.admin_note = (
            payload.reason
        )

        registration.correction_fields = (
            None
        )

        registration.approved_by = (
            admin.id
        )

        registration.approved_at = (
            utcnow()
        )

        registration.qr_token = (
            create_qr_token(
                registration.id
            )
        )


        recipient_count = (
            await _send_approval_emails(
                db,
                registration,
            )
        )


        response_message = (
            'Registration approved and QR '
            f'emailed to {recipient_count} '
            'recipient(s)'
        )


    # ========================================================
    # REQUEST CORRECTION
    # ========================================================

    elif (
        payload.action ==
        'REQUEST_CORRECTION'
    ):

        if (
            not payload.reason
            or not payload.correction_fields
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    'Reason and correction_fields '
                    'are required'
                ),
            )


        registration.status = (
            'CORRECTION_REQUIRED'
        )

        registration.admin_note = (
            payload.reason
        )

        registration.correction_fields = (
            payload.correction_fields
        )


        html = branded_email(
            'Registration correction required',

            (
                f'<p>{payload.reason}</p>'
                f'<p><strong>Fields to correct:</strong> '
                f'{", ".join(payload.correction_fields)}</p>'
            ),
        )


        recipient_count = (
            await send_to_coordinators(
                db,
                registration,

                (
                    'Correction required — '
                    f'{registration.registration_code}'
                ),

                html,

                'CORRECTION_REQUIRED',
            )
        )


        response_message = (
            'Correction request sent to '
            f'{recipient_count} coordinator(s)'
        )


    # ========================================================
    # REJECT
    # ========================================================

    elif (
        payload.action ==
        'REJECT'
    ):

        if not payload.reason:
            raise HTTPException(
                status_code=422,
                detail='Reason required',
            )


        registration.status = (
            'REJECTED'
        )

        registration.admin_note = (
            payload.reason
        )

        registration.qr_token = (
            None
        )


        html = branded_email(
            'Registration not approved',

            (
                f'<p>{payload.reason}</p>'
            ),
        )


        recipient_count = (
            await send_to_coordinators(
                db,
                registration,

                (
                    'Registration update — '
                    f'{registration.registration_code}'
                ),

                html,

                'REGISTRATION_REJECTED',
            )
        )


        response_message = (
            'Registration rejected and '
            f'{recipient_count} coordinator(s) notified'
        )


    # ========================================================
    # REOPEN
    # ========================================================

    elif (
        payload.action ==
        'REOPEN'
    ):

        registration.status = (
            'CORRECTION_REQUIRED'
        )

        registration.admin_note = (
            payload.reason
            or
            'Reopened by admin'
        )

        registration.correction_fields = (
            payload.correction_fields
            or [
                'college_name',
                'team_name',
                'coach_name',
                'ped_contact',
                'student_coordinator_name',
                'student_coordinator_contact',
                'students',
                'student_photos',
                'bonafide',
                'declaration',
            ]
        )

        registration.qr_token = (
            None
        )

        registration.approved_at = (
            None
        )

        registration.approved_by = (
            None
        )


        html = branded_email(
            'Registration reopened',

            (
                f'<p>'
                f'{registration.admin_note}'
                f'</p>'
                f'<p>Please log in to the Coordinator '
                f'Portal and update the registration.</p>'
            ),
        )


        recipient_count = (
            await send_to_coordinators(
                db,
                registration,

                (
                    'Registration reopened — '
                    f'{registration.registration_code}'
                ),

                html,

                'REGISTRATION_REOPENED',
            )
        )


        response_message = (
            'Registration reopened and '
            f'{recipient_count} coordinator(s) notified'
        )


    else:
        raise HTTPException(
            status_code=422,
            detail=(
                'Unsupported review action'
            ),
        )


    await audit(
        db,
        'ADMIN',
        admin.id,

        (
            f'REGISTRATION_'
            f'{payload.action}'
        ),

        'REGISTRATION',

        registration.id,

        payload.reason,

        {
            'correction_fields':
                payload.correction_fields
        },
    )


    await db.commit()


    return MessageResponse(
        message=response_message
    )


# ============================================================
# QR
# ============================================================

@router.get(
    '/registrations/{rid}/qr.png'
)
async def qr(
    rid: str,

    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await db.get(
        Registration,
        rid,
    )

    if (
        not registration
        or registration.status
        != 'APPROVED'
        or not registration.qr_token
    ):
        raise HTTPException(
            status_code=404,
            detail='QR unavailable',
        )

    return StreamingResponse(
        io.BytesIO(
            qr_png_bytes(
                registration.qr_token
            )
        ),

        media_type='image/png',

        headers={
            'Content-Disposition':
                (
                    f'inline; filename='
                    f'{registration.registration_code}'
                    f'-QR.png'
                )
        },
    )


# ============================================================
# LIVE STREAMS
# ============================================================

@router.get(
    '/live-streams'
)
async def live_streams(
    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    return (
        await db.scalars(
            select(
                LiveStream
            ).order_by(
                LiveStream.updated_at.desc()
            )
        )
    ).all()


@router.post(
    '/live-streams'
)
async def livestream(
    payload: LiveStreamUpsert,

    admin: Admin = Depends(
        require_admin_roles(
            'FIXTURE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    stream = (
        await db.scalar(
            select(
                LiveStream
            ).where(
                LiveStream.event_config_id
                == payload.event_config_id
            )
        )

        if payload.event_config_id

        else None
    )


    data = payload.model_dump()

    data[
        'youtube_url'
    ] = str(
        data[
            'youtube_url'
        ]
    )


    if not stream:

        stream = LiveStream(
            updated_by=
                admin.id,

            **data,
        )

        db.add(
            stream
        )

        await db.flush()

    else:

        for (
            key,
            value
        ) in data.items():

            setattr(
                stream,
                key,
                value,
            )

        stream.updated_by = (
            admin.id
        )


    await audit(
        db,
        'ADMIN',
        admin.id,
        'UPSERT_LIVE_STREAM',
        'LIVE_STREAM',
        stream.id,
    )


    await db.commit()

    await db.refresh(
        stream
    )

    return stream


# ============================================================
# FIXTURES
# ============================================================

@router.get(
    '/fixtures'
)
async def fixtures(
    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    rows = (
        await db.scalars(
            select(
                Fixture
            )
            .options(
                selectinload(
                    Fixture.event_config
                )
            )
            .order_by(
                Fixture.created_at.desc()
            )
        )
    ).all()


    return [
        {
            'id':
                item.id,

            'event_config_id':
                item.event_config_id,

            'event_name':
                (
                    item.event_config.sport_name
                    if item.event_config
                    else 'General'
                ),

            'category':
                (
                    item.event_config.category
                    if item.event_config
                    else 'All'
                ),

            'title':
                item.title,

            'note':
                item.note,

            'version':
                item.version,

            'visibility':
                item.visibility,

            'status':
                item.status,

            'published_at':
                item.published_at,

            'supersedes_id':
                item.supersedes_id,

            'download_url':
                await storage.signed_url(
                    settings
                    .SUPABASE_BUCKET_FIXTURES,

                    item.file_path,
                ),
        }

        for item
        in rows
    ]


@router.post(
    '/fixtures',
    status_code=201,
)
async def fixture_upload(
    title: str = Form(...),

    event_config_id:
        str | None = Form(None),

    note:
        str | None = Form(None),

    visibility:
        str = Form(
            'RELEVANT_PEDS'
        ),

    supersedes_id:
        str | None = Form(None),

    file: UploadFile = File(
        ...
    ),

    admin: Admin = Depends(
        require_admin_roles(
            'FIXTURE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    if visibility not in {
        'PUBLIC',
        'RELEVANT_PEDS',
        'ALL_PEDS',
    }:
        raise HTTPException(
            status_code=422,
            detail=(
                'Invalid fixture visibility'
            ),
        )


    if file.content_type not in {
        'application/pdf',
        'image/jpeg',
        'image/png',
    }:
        raise HTTPException(
            status_code=415,
            detail=(
                'Fixture must be PDF, JPG or PNG'
            ),
        )


    if (
        event_config_id
        and not await db.get(
            EventConfig,
            event_config_id,
        )
    ):
        raise HTTPException(
            status_code=404,
            detail='Event not found',
        )


    data = await file.read(
        15 * 1024 * 1024 + 1
    )


    if (
        len(data)
        > 15 * 1024 * 1024
    ):
        raise HTTPException(
            status_code=413,
            detail=(
                'Fixture exceeds 15 MB'
            ),
        )


    previous = (
        await db.get(
            Fixture,
            supersedes_id,
        )

        if supersedes_id

        else None
    )


    version = (
        previous.version + 1

        if previous

        else 1
    )


    ext = (
        mimetypes.guess_extension(
            file.content_type
        )
        or '.pdf'
    )


    path = (
        f'{event_config_id or "general"}/'
        f'{uuid.uuid4().hex}{ext}'
    )


    await storage.upload(
        settings
        .SUPABASE_BUCKET_FIXTURES,

        path,

        data,

        file.content_type,
    )


    fixture = Fixture(
        event_config_id=
            event_config_id,

        title=
            title,

        note=
            note,

        version=
            version,

        file_path=
            path,

        visibility=
            visibility,

        uploaded_by=
            admin.id,

        supersedes_id=
            supersedes_id,
    )


    db.add(
        fixture
    )


    await db.flush()


    await audit(
        db,
        'ADMIN',
        admin.id,
        'UPLOAD_FIXTURE',
        'FIXTURE',
        fixture.id,
    )


    await db.commit()


    return {
        'id':
            fixture.id,

        'version':
            fixture.version,

        'status':
            fixture.status,
    }


# ============================================================
# PUBLISH FIXTURE
# ============================================================

@router.post(
    '/fixtures/{fid}/publish',
    response_model=MessageResponse,
)
async def fixture_publish(
    fid: str,

    admin: Admin = Depends(
        require_admin_roles(
            'FIXTURE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    fixture = await db.scalar(
        select(
            Fixture
        )
        .where(
            Fixture.id == fid
        )
        .options(
            selectinload(
                Fixture.event_config
            )
        )
    )


    if not fixture:
        raise HTTPException(
            status_code=404,
            detail='Fixture not found',
        )


    if fixture.supersedes_id:

        old = await db.get(
            Fixture,
            fixture.supersedes_id,
        )

        if old:
            old.status = (
                'SUPERSEDED'
            )


    fixture.status = (
        'PUBLISHED'
    )

    fixture.published_at = (
        utcnow()
    )


    # ========================================================
    # FIND RELEVANT COLLEGE ACCOUNTS
    # ========================================================

    query = (
        select(
            Ped
        )
        .join(
            Registration,
            Registration.ped_id
            == Ped.id,
        )
    )


    if (
        fixture.visibility
        == 'RELEVANT_PEDS'
        and fixture.event_config_id
    ):
        query = query.where(
            Registration.event_config_id
            == fixture.event_config_id
        )


    ped_accounts = (
        await db.scalars(
            query
        )
    ).all()


    # ========================================================
    # PED + COACH + MANAGER
    # ========================================================

    recipients = set()


    for ped in ped_accounts:
        recipients.update(
            coordinator_emails(
                ped
            )
        )


    event_name = (
        fixture.event_config.sport_name

        if fixture.event_config

        else 'BNMIT ODYSSEY'
    )


    category = (
        fixture.event_config.category

        if fixture.event_config

        else 'All participants'
    )


    for email in recipients:

        await email_service.send(
            db,
            email,

            (
                'Fixture published — '
                f'{event_name} · '
                f'{category}'
            ),

            fixture_html(
                event_name,

                category,

                fixture.title,

                fixture.version,

                (
                    f"{settings.PUBLIC_APP_URL.rstrip('/')}"
                    f"/#/ped"
                ),

                fixture.note,
            ),

            'FIXTURE_PUBLISHED',
        )


    await audit(
        db,
        'ADMIN',
        admin.id,
        'PUBLISH_FIXTURE',
        'FIXTURE',
        fixture.id,
        details={
            'recipient_count':
                len(recipients)
        },
    )


    await db.commit()


    return MessageResponse(
        message=(
            'Fixture published and '
            f'{len(recipients)} '
            'coordinator(s) notified'
        )
    )


# ============================================================
# ADMIN USERS
# ============================================================

@router.get(
    '/users',
    response_model=list[AdminOut],
)
async def list_admin_users(
    admin: Admin = Depends(
        require_admin_roles(
            'SUPER_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    return (
        await db.scalars(
            select(
                Admin
            ).order_by(
                Admin.created_at
            )
        )
    ).all()


@router.post(
    '/users',
    response_model=AdminOut,
    status_code=201,
)
async def create_admin_user(
    payload: AdminCreate,

    admin: Admin = Depends(
        require_admin_roles(
            'SUPER_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    email = (
        str(
            payload.email
        )
        .strip()
        .lower()
    )


    if await db.scalar(
        select(
            Admin
        ).where(
            Admin.email == email
        )
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                'Admin email already exists'
            ),
        )


    user = Admin(
        name=
            payload.name,

        email=
            email,

        password_hash=
            hash_password(
                payload.password
            ),

        role=
            payload.role,
    )


    db.add(
        user
    )

    await db.flush()


    await audit(
        db,
        'ADMIN',
        admin.id,
        'CREATE_ADMIN',
        'ADMIN',
        user.id,
    )


    await db.commit()

    await db.refresh(
        user
    )


    return user


@router.patch(
    '/users/{admin_id}',
    response_model=AdminOut,
)
async def update_admin_user(
    admin_id: str,
    payload: AdminUpdate,

    admin: Admin = Depends(
        require_admin_roles(
            'SUPER_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    user = await db.get(
        Admin,
        admin_id,
    )


    if not user:
        raise HTTPException(
            status_code=404,
            detail='Admin not found',
        )


    data = payload.model_dump(
        exclude_unset=True
    )


    password = data.pop(
        'password',
        None,
    )


    new_email = data.pop(
        'email',
        None,
    )


    if (
        new_email is not None
    ):
        normalized = (
            str(
                new_email
            )
            .strip()
            .lower()
        )


        existing = await db.scalar(
            select(
                Admin
            ).where(
                Admin.email
                == normalized,

                Admin.id
                != admin_id,
            )
        )


        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    'Another admin already uses '
                    'this email address'
                ),
            )


        user.email = (
            normalized
        )


    for (
        key,
        value
    ) in data.items():

        setattr(
            user,
            key,
            value,
        )


    if password:
        user.password_hash = (
            hash_password(
                password
            )
        )


    await audit(
        db,
        'ADMIN',
        admin.id,
        'UPDATE_ADMIN',
        'ADMIN',
        user.id,
        details={
            'fields':
                list(
                    payload
                    .model_dump(
                        exclude_unset=True
                    )
                )
        },
    )


    await db.commit()

    await db.refresh(
        user
    )


    return user


# ============================================================
# AUDIT LOGS
# ============================================================

@router.get(
    '/audit-logs'
)
async def audit_logs(
    action:
        str | None = None,

    entity_type:
        str | None = None,

    limit: int = Query(
        100,
        ge=1,
        le=500,
    ),

    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    query = (
        select(
            AuditLog
        )
        .order_by(
            AuditLog.created_at.desc()
        )
        .limit(
            limit
        )
    )


    if action:
        query = query.where(
            AuditLog.action
            == action
        )


    if entity_type:
        query = query.where(
            AuditLog.entity_type
            == entity_type
        )


    return (
        await db.scalars(
            query
        )
    ).all()


# ============================================================
# EMAIL LOGS
# ============================================================

@router.get(
    '/email-logs'
)
async def email_logs(
    message_type:
        str | None = None,

    limit: int = Query(
        100,
        ge=1,
        le=500,
    ),

    admin: Admin = Depends(
        get_current_admin
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    query = (
        select(
            EmailLog
        )
        .order_by(
            EmailLog.created_at.desc()
        )
        .limit(
            limit
        )
    )


    if message_type:
        query = query.where(
            EmailLog.message_type
            == message_type
        )


    return (
        await db.scalars(
            query
        )
    ).all()