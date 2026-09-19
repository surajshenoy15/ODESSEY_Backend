import io
import mimetypes
import zipfile

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
)

from fastapi.responses import StreamingResponse

from sqlalchemy import (
    and_,
    or_,
    select,
)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_ped
from app.core.security import utcnow

from app.models.entities import (
    Certificate,
    CertificateCorrectionRequest,
    EventConfig,
    Fixture,
    Ped,
    Registration,
    Student,
)

from app.schemas import (
    CertificateCorrectionCreate,
    MessageResponse,
    PedOut,
    PedProfileUpdate,
    RegistrationCreate,
    RegistrationOut,
    RegistrationUpdate,
    StudentCreate,
    StudentOut,
    StudentUpdate,
)

from app.services.helpers import (
    audit,
    ensure_editable,
    qr_png_bytes,
    registration_code,
    safe_filename,
)

from app.services.storage import storage


router = APIRouter(
    prefix='/ped',
    tags=['PED Portal'],
)


IMG = {
    'image/jpeg',
    'image/png',
    'image/webp',
}


DOC = {
    'application/pdf',
    'image/jpeg',
    'image/png',
}


# ============================================================
# HELPERS
# ============================================================

def aware(value):
    if not value:
        return None

    return (
        value.replace(
            tzinfo=utcnow().tzinfo
        )
        if value.tzinfo is None
        else value
    )


async def owned(
    db: AsyncSession,
    ped: Ped,
    rid: str,
    students: bool = True,
):
    query = (
        select(
            Registration
        )
        .where(
            Registration.id == rid,
            Registration.ped_id == ped.id,
        )
    )

    if students:
        query = query.options(
            selectinload(
                Registration.students
            )
        )

    registration = await db.scalar(
        query
    )

    if not registration:
        raise HTTPException(
            status_code=404,
            detail='Registration not found',
        )

    return registration


# ============================================================
# PROFILE
# ============================================================

@router.get(
    '/me',
    response_model=PedOut,
)
async def me(
    ped: Ped = Depends(
        get_current_ped
    ),
):
    """
    Return the common PED account.

    PED, Coach and Manager login to the same Ped.id.

    Coach and Manager information can be READ here through
    PedOut, but their identity/login fields cannot be modified
    through the generic /ped/me update endpoint.
    """

    return ped


@router.put(
    '/me',
    response_model=PedOut,
)
async def profile(
    p: PedProfileUpdate,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    """
    Update only safe common PED profile fields.

    IMPORTANT SECURITY RULE:

    PED, Coach and Manager currently receive the same JWT:

        sub = Ped.id
        role = PED
        actor_type = PED

    Therefore this generic endpoint must NOT modify:

    - coach_name
    - coach_email
    - coach_contact_number
    - manager_name
    - manager_email
    - manager_contact_number

    PedProfileUpdate also uses extra='forbid', providing an
    additional validation layer.
    """

    data = p.model_dump(
        exclude_unset=True
    )


    # ========================================================
    # DEFENCE-IN-DEPTH ALLOWLIST
    # ========================================================

    allowed_fields = {
        'name',
        'college_name',
        'college_location',
        'contact_number',
        'declaration_accepted',
    }


    invalid_fields = (
        set(data.keys())
        - allowed_fields
    )


    if invalid_fields:
        raise HTTPException(
            status_code=422,
            detail={
                'message':
                    'These fields cannot be updated through '
                    'the common PED profile endpoint',

                'invalid_fields':
                    sorted(
                        invalid_fields
                    ),
            },
        )


    # ========================================================
    # UPDATE PROFILE
    # ========================================================

    changed_fields = []


    for key, value in data.items():

        if key == 'declaration_accepted':

            if value:
                ped.declaration_accepted_at = (
                    utcnow()
                )

                changed_fields.append(
                    'declaration_accepted'
                )

            continue


        setattr(
            ped,
            key,
            value,
        )

        changed_fields.append(
            key
        )


    # ========================================================
    # AUDIT
    # ========================================================

    await audit(
        db,
        'PED',
        ped.id,
        'UPDATE_PROFILE',
        'PED',
        ped.id,
        details={
            'fields':
                changed_fields
        },
    )


    await db.commit()

    await db.refresh(
        ped
    )


    return ped


# ============================================================
# DASHBOARD
# ============================================================

@router.get('/dashboard')
async def dashboard(
    ped: Ped = Depends(
        get_current_ped
    ),
    db: AsyncSession = Depends(
        get_db
    ),
):
    rows = (
        await db.scalars(
            select(
                Registration
            )
            .where(
                Registration.ped_id
                == ped.id
            )
            .options(
                selectinload(
                    Registration.students
                ),
                selectinload(
                    Registration.event_config
                ),
            )
            .order_by(
                Registration.created_at.desc()
            )
        )
    ).all()


    counts = {}

    items = []


    next_actions = {
        'DRAFT':
            'Complete roster, uploads and payment',

        'PAYMENT_PENDING':
            'Complete payment',

        'UNDER_REVIEW':
            'Wait for admin review',

        'CORRECTION_REQUIRED':
            'Correct requested fields and resubmit',

        'APPROVED':
            'Present QR and view fixtures',

        'REJECTED':
            'Contact support',
    }


    for registration in rows:

        counts[
            registration.status
        ] = (
            counts.get(
                registration.status,
                0,
            )
            + 1
        )


        items.append({
            'id':
                registration.id,

            'registration_code':
                registration.registration_code,

            'event':
                registration
                .event_config
                .sport_name,

            'category':
                registration
                .event_config
                .category,

            'student_count':
                len(
                    registration.students
                ),

            'status':
                registration.status,

            'payment_status':
                registration.payment_status,

            'next_action':
                next_actions.get(
                    registration.status,
                    'View details',
                ),
        })


    return {
        'profile_complete':
            bool(
                ped.name
                and ped.college_name
                and ped.contact_number
            ),

        'counts':
            counts,

        'registrations':
            items,
    }


# ============================================================
# CREATE REGISTRATION
# ============================================================

@router.post(
    '/registrations',
    response_model=RegistrationOut,
    status_code=201,
)
async def create_registration(
    p: RegistrationCreate,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    event = await db.get(
        EventConfig,
        p.event_config_id,
    )

    now = utcnow()


    if (
        not event
        or not event.is_active
    ):
        raise HTTPException(
            status_code=404,
            detail='Event/category not found',
        )


    if (
        not event.is_registration_open

        or (
            event.registration_opens_at
            and aware(
                event.registration_opens_at
            ) > now
        )

        or (
            event.registration_closes_at
            and aware(
                event.registration_closes_at
            ) < now
        )
    ):
        raise HTTPException(
            status_code=409,
            detail='Registration is closed',
        )


    duplicate = await db.scalar(
        select(
            Registration
        )
        .where(
            Registration.ped_id
            == ped.id,

            Registration.event_config_id
            == event.id,

            Registration.status.notin_(
                [
                    'REJECTED',
                    'CANCELLED',
                ]
            ),
        )
    )


    if duplicate:
        raise HTTPException(
            status_code=409,
            detail=(
                'Active registration already '
                'exists for this event/category'
            ),
        )


    payload = p.model_dump()


    # ========================================================
    # LEGACY REGISTRATION COACH SNAPSHOT
    #
    # Authoritative Coach identity lives on Ped:
    #
    # ped.coach_name
    # ped.coach_email
    # ped.coach_contact_number
    #
    # Registration.coach_name is retained only for backwards
    # compatibility with existing registrations/UI.
    # ========================================================

    if (
        not payload.get(
            'coach_name'
        )
        and ped.coach_name
    ):
        payload[
            'coach_name'
        ] = ped.coach_name


    registration = Registration(
        registration_code=
            registration_code(),

        ped_id=
            ped.id,

        fee_paise=
            event.fee_paise,

        **payload,
    )


    db.add(
        registration
    )

    await db.flush()


    await audit(
        db,
        'PED',
        ped.id,
        'CREATE_REGISTRATION',
        'REGISTRATION',
        registration.id,
    )


    await db.commit()


    return await owned(
        db,
        ped,
        registration.id,
    )


# ============================================================
# LIST REGISTRATIONS
# ============================================================

@router.get(
    '/registrations',
    response_model=list[
        RegistrationOut
    ],
)
async def registrations(
    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    return (
        await db.scalars(
            select(
                Registration
            )
            .where(
                Registration.ped_id
                == ped.id
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
    ).all()


# ============================================================
# SINGLE REGISTRATION
# ============================================================

@router.get(
    '/registrations/{rid}',
    response_model=RegistrationOut,
)
async def registration(
    rid: str,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    return await owned(
        db,
        ped,
        rid,
    )


# ============================================================
# UPDATE REGISTRATION
# ============================================================

@router.patch(
    '/registrations/{rid}',
    response_model=RegistrationOut,
)
async def update_registration(
    rid: str,

    p: RegistrationUpdate,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )

    ensure_editable(
        registration
    )


    data = p.model_dump(
        exclude_unset=True
    )


    if (
        registration.status
        == 'CORRECTION_REQUIRED'
        and registration.correction_fields
    ):

        invalid = [
            key
            for key in data

            if key
            not in registration.correction_fields
        ]

        if invalid:
            raise HTTPException(
                status_code=409,
                detail={
                    'allowed_fields':
                        registration
                        .correction_fields,

                    'invalid_fields':
                        invalid,
                },
            )


    for key, value in data.items():
        setattr(
            registration,
            key,
            value,
        )


    await audit(
        db,
        'PED',
        ped.id,
        'UPDATE_REGISTRATION',
        'REGISTRATION',
        registration.id,
        details={
            'fields':
                list(
                    data
                )
        },
    )


    await db.commit()


    return await owned(
        db,
        ped,
        rid,
    )


# ============================================================
# ADD STUDENT
# ============================================================

@router.post(
    '/registrations/{rid}/students',
    response_model=StudentOut,
    status_code=201,
)
async def add_student(
    rid: str,

    p: StudentCreate,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )

    ensure_editable(
        registration
    )


    event = await db.get(
        EventConfig,
        registration.event_config_id,
    )


    if (
        len(
            registration.students
        )
        >= event.team_max_size
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f'Maximum team size is '
                f'{event.team_max_size}'
            ),
        )


    existing_usn = await db.scalar(
        select(
            Student
        )
        .where(
            Student.registration_id
            == registration.id,

            Student.usn
            == p.usn,
        )
    )


    if existing_usn:
        raise HTTPException(
            status_code=409,
            detail=(
                'USN already exists '
                'in this registration'
            ),
        )


    duplicate = await db.scalar(
        select(
            Student
        )
        .join(
            Registration
        )
        .where(
            Student.usn
            == p.usn,

            Registration.event_config_id
            == registration.event_config_id,

            Registration.id
            != registration.id,

            Registration.status.notin_(
                [
                    'REJECTED',
                    'CANCELLED',
                ]
            ),
        )
    )


    if duplicate:
        raise HTTPException(
            status_code=409,
            detail=(
                'USN already registered '
                'in this event/category'
            ),
        )


    student = Student(
        registration_id=
            registration.id,

        **p.model_dump(),
    )


    db.add(
        student
    )

    await db.flush()


    await audit(
        db,
        'PED',
        ped.id,
        'ADD_STUDENT',
        'STUDENT',
        student.id,
        details={
            'registration_id':
                registration.id
        },
    )


    await db.commit()

    await db.refresh(
        student
    )


    return student


# ============================================================
# UPDATE STUDENT
# ============================================================

@router.patch(
    '/registrations/{rid}/students/{sid}',
    response_model=StudentOut,
)
async def update_student(
    rid: str,
    sid: str,

    p: StudentUpdate,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )

    ensure_editable(
        registration
    )


    student = await db.scalar(
        select(
            Student
        )
        .where(
            Student.id
            == sid,

            Student.registration_id
            == registration.id,
        )
    )


    if not student:
        raise HTTPException(
            status_code=404,
            detail='Student not found',
        )


    data = p.model_dump(
        exclude_unset=True
    )


    for key, value in data.items():
        setattr(
            student,
            key,
            value,
        )


    await audit(
        db,
        'PED',
        ped.id,
        'UPDATE_STUDENT',
        'STUDENT',
        student.id,
        details={
            'fields':
                list(
                    data
                )
        },
    )


    await db.commit()

    await db.refresh(
        student
    )


    return student


# ============================================================
# DELETE STUDENT
# ============================================================

@router.delete(
    '/registrations/{rid}/students/{sid}',
    response_model=MessageResponse,
)
async def delete_student(
    rid: str,
    sid: str,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )

    ensure_editable(
        registration
    )


    student = await db.scalar(
        select(
            Student
        )
        .where(
            Student.id
            == sid,

            Student.registration_id
            == registration.id,
        )
    )


    if not student:
        raise HTTPException(
            status_code=404,
            detail='Student not found',
        )


    await audit(
        db,
        'PED',
        ped.id,
        'DELETE_STUDENT',
        'STUDENT',
        student.id,
    )


    await db.delete(
        student
    )

    await db.commit()


    return MessageResponse(
        message='Student removed'
    )


# ============================================================
# STUDENT PHOTO
# ============================================================

@router.post(
    '/registrations/{rid}/students/{sid}/photo',
    response_model=StudentOut,
)
async def photo(
    rid: str,
    sid: str,

    file: UploadFile = File(
        ...
    ),

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )

    ensure_editable(
        registration
    )


    student = await db.scalar(
        select(
            Student
        )
        .where(
            Student.id
            == sid,

            Student.registration_id
            == registration.id,
        )
    )


    if not student:
        raise HTTPException(
            status_code=404,
            detail='Student not found',
        )


    if file.content_type not in IMG:
        raise HTTPException(
            status_code=415,
            detail=(
                'Photo must be JPG, '
                'PNG or WEBP'
            ),
        )


    data = await file.read(
        5 * 1024 * 1024 + 1
    )


    if (
        len(data)
        > 5 * 1024 * 1024
    ):
        raise HTTPException(
            status_code=413,
            detail='Photo exceeds 5 MB',
        )


    extension = (
        mimetypes.guess_extension(
            file.content_type
        )
        or '.jpg'
    )


    path = (
        f'{registration.id}/'
        f'{student.id}'
        f'{extension}'
    )


    await storage.upload(
        settings
        .SUPABASE_BUCKET_STUDENT_PHOTOS,

        path,

        data,

        file.content_type,
    )


    student.photo_path = (
        path
    )


    await audit(
        db,
        'PED',
        ped.id,
        'UPLOAD_STUDENT_PHOTO',
        'STUDENT',
        student.id,
    )


    await db.commit()

    await db.refresh(
        student
    )


    return student


# ============================================================
# BONAFIDE
# ============================================================

@router.post(
    '/registrations/{rid}/bonafide',
    response_model=RegistrationOut,
)
async def bonafide(
    rid: str,

    file: UploadFile = File(
        ...
    ),

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )

    ensure_editable(
        registration
    )


    if file.content_type not in DOC:
        raise HTTPException(
            status_code=415,
            detail=(
                'Bonafide must be PDF, '
                'JPG or PNG'
            ),
        )


    data = await file.read(
        10 * 1024 * 1024 + 1
    )


    if (
        len(data)
        > 10 * 1024 * 1024
    ):
        raise HTTPException(
            status_code=413,
            detail='Bonafide exceeds 10 MB',
        )


    extension = (
        mimetypes.guess_extension(
            file.content_type
        )
        or '.pdf'
    )


    path = (
        f'{registration.id}/'
        f'bonafide{extension}'
    )


    await storage.upload(
        settings
        .SUPABASE_BUCKET_BONAFIDES,

        path,

        data,

        file.content_type,
    )


    registration.bonafide_path = (
        path
    )


    await audit(
        db,
        'PED',
        ped.id,
        'UPLOAD_BONAFIDE',
        'REGISTRATION',
        registration.id,
    )


    await db.commit()


    return await owned(
        db,
        ped,
        rid,
    )


# ============================================================
# VALIDATE REGISTRATION
# ============================================================

async def validate_ready(
    db: AsyncSession,
    registration: Registration,
):
    event = await db.get(
        EventConfig,
        registration.event_config_id,
    )


    students = (
        await db.scalars(
            select(
                Student
            )
            .where(
                Student.registration_id
                == registration.id
            )
        )
    ).all()


    if not (
        event.team_min_size
        <= len(
            students
        )
        <= event.team_max_size
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f'Team size must be '
                f'{event.team_min_size}-'
                f'{event.team_max_size}'
            ),
        )


    missing = [
        student.full_name

        for student
        in students

        if not student.photo_path
    ]


    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                'missing_student_photos':
                    missing
            },
        )


    if not registration.bonafide_path:
        raise HTTPException(
            status_code=422,
            detail='Bonafide is required',
        )


    if (
        not registration.declaration_accepted
        or not registration.consent_accepted
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                'Declaration and consent '
                'are required'
            ),
        )


# ============================================================
# RESUBMIT
# ============================================================

@router.post(
    '/registrations/{rid}/resubmit',
    response_model=RegistrationOut,
)
async def resubmit(
    rid: str,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
    )


    if (
        registration.status
        != 'CORRECTION_REQUIRED'
    ):
        raise HTTPException(
            status_code=409,
            detail='Not awaiting correction',
        )


    await validate_ready(
        db,
        registration,
    )


    registration.status = (
        'UNDER_REVIEW'
    )

    registration.admin_note = (
        None
    )

    registration.correction_fields = (
        None
    )

    registration.submitted_at = (
        utcnow()
    )


    await audit(
        db,
        'PED',
        ped.id,
        'RESUBMIT_REGISTRATION',
        'REGISTRATION',
        registration.id,
    )


    await db.commit()


    return await owned(
        db,
        ped,
        rid,
    )


# ============================================================
# FIXTURES
# ============================================================

@router.get('/fixtures')
async def fixtures(
    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    ids = (
        await db.scalars(
            select(
                Registration.event_config_id
            )
            .where(
                Registration.ped_id
                == ped.id
            )
        )
    ).all()


    query = (
        select(
            Fixture
        )
        .where(
            Fixture.status
            == 'PUBLISHED',

            or_(
                Fixture.visibility.in_(
                    [
                        'PUBLIC',
                        'ALL_PEDS',
                    ]
                ),

                and_(
                    Fixture.visibility
                    == 'RELEVANT_PEDS',

                    Fixture.event_config_id.in_(
                        ids
                        or [
                            'none'
                        ]
                    ),
                ),
            ),
        )
        .order_by(
            Fixture.published_at.desc()
        )
    )


    rows = (
        await db.scalars(
            query
        )
    ).all()


    output = []


    for fixture in rows:

        output.append({
            'id':
                fixture.id,

            'event_config_id':
                fixture.event_config_id,

            'title':
                fixture.title,

            'note':
                fixture.note,

            'version':
                fixture.version,

            'published_at':
                fixture.published_at,

            'download_url':
                await storage.signed_url(
                    settings
                    .SUPABASE_BUCKET_FIXTURES,

                    fixture.file_path,
                ),
        })


    return output


# ============================================================
# CERTIFICATES
# ============================================================

@router.get('/certificates')
async def certificates(
    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    rows = (
        await db.execute(
            select(
                Certificate,
                Student,
                Registration,
                EventConfig,
            )
            .join(
                Student,
                Certificate.student_id
                == Student.id,
            )
            .join(
                Registration,
                Certificate.registration_id
                == Registration.id,
            )
            .join(
                EventConfig,
                Registration.event_config_id
                == EventConfig.id,
            )
            .where(
                Registration.ped_id
                == ped.id,

                Certificate.status
                == 'PUBLISHED',
            )
            .order_by(
                EventConfig.sport_name,
                Student.full_name,
            )
        )
    ).all()


    output = []


    for (
        certificate,
        student,
        registration,
        event,
    ) in rows:

        output.append({
            'id':
                certificate.id,

            'student_name':
                student.full_name,

            'usn':
                student.usn,

            'registration_id':
                registration.id,

            'registration_code':
                registration.registration_code,

            'event':
                event.sport_name,

            'category':
                event.category,

            'certificate_number':
                certificate
                .certificate_number,

            'version':
                certificate.version,

            'download_url':
                (
                    f"{settings.API_PUBLIC_URL.rstrip('/')}"
                    f"{settings.API_V1_PREFIX}"
                    f"/ped/certificates/"
                    f"{certificate.id}/download"
                ),

            'download_count':
                certificate.download_count,
        })


    return output


# ============================================================
# TEAM CERTIFICATE ZIP
# ============================================================

@router.get(
    '/registrations/{rid}/certificates.zip'
)
async def team_certificates(
    rid: str,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
        False,
    )


    rows = (
        await db.execute(
            select(
                Certificate,
                Student,
            )
            .join(
                Student,
                Certificate.student_id
                == Student.id,
            )
            .where(
                Certificate.registration_id
                == registration.id,

                Certificate.status
                == 'PUBLISHED',
            )
        )
    ).all()


    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                'No published certificates'
            ),
        )


    buffer = (
        io.BytesIO()
    )


    with zipfile.ZipFile(
        buffer,
        'w',
        zipfile.ZIP_DEFLATED,
    ) as archive:

        for (
            certificate,
            student,
        ) in rows:

            data = await storage.download(
                settings
                .SUPABASE_BUCKET_CERTIFICATES,

                certificate.file_path,
            )


            archive.writestr(
                (
                    f'{safe_filename(student.full_name)}-'
                    f'{safe_filename(student.usn)}'
                    f'.pdf'
                ),

                data,
            )


    buffer.seek(
        0
    )


    return StreamingResponse(
        buffer,

        media_type=
            'application/zip',

        headers={
            'Content-Disposition':
                (
                    f'attachment; '
                    f'filename='
                    f'{registration.registration_code}'
                    f'-certificates.zip'
                )
        },
    )


# ============================================================
# REGISTRATION QR
#
# This endpoint intentionally remains the legacy/generic
# registration QR.
#
# It does NOT identify the holder as PED / Coach / Manager.
#
# Role-specific coordinator QRs are generated separately during
# registration approval and emailed to the corresponding person.
# ============================================================

@router.get(
    '/registrations/{rid}/qr.png'
)
async def ped_registration_qr(
    rid: str,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = await owned(
        db,
        ped,
        rid,
        False,
    )


    if (
        registration.status
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
                    f'inline; '
                    f'filename='
                    f'{registration.registration_code}'
                    f'-QR.png'
                )
        },
    )


# ============================================================
# CERTIFICATE DOWNLOAD
# ============================================================

@router.get(
    '/certificates/{certificate_id}/download'
)
async def download_certificate(
    certificate_id: str,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    row = await db.execute(
        select(
            Certificate,
            Student,
            Registration,
        )
        .join(
            Student,
            Certificate.student_id
            == Student.id,
        )
        .join(
            Registration,
            Certificate.registration_id
            == Registration.id,
        )
        .where(
            Certificate.id
            == certificate_id,

            Certificate.status
            == 'PUBLISHED',

            Registration.ped_id
            == ped.id,
        )
    )


    result = row.first()


    if not result:
        raise HTTPException(
            status_code=404,
            detail='Certificate not found',
        )


    (
        certificate,
        student,
        registration,
    ) = result


    data = await storage.download(
        settings
        .SUPABASE_BUCKET_CERTIFICATES,

        certificate.file_path,
    )


    certificate.download_count += (
        1
    )

    certificate.last_downloaded_at = (
        utcnow()
    )


    await audit(
        db,
        'PED',
        ped.id,
        'DOWNLOAD_CERTIFICATE',
        'CERTIFICATE',
        certificate.id,
    )


    await db.commit()


    filename = (
        f'{safe_filename(student.full_name)}-'
        f'{safe_filename(student.usn)}'
        f'.pdf'
    )


    return StreamingResponse(
        io.BytesIO(
            data
        ),

        media_type=
            'application/pdf',

        headers={
            'Content-Disposition':
                (
                    f'attachment; '
                    f'filename={filename}'
                )
        },
    )


# ============================================================
# CERTIFICATE CORRECTION REQUEST
# ============================================================

@router.post(
    '/certificates/{certificate_id}/correction-request',
    response_model=MessageResponse,
)
async def request_certificate_correction(
    certificate_id: str,

    payload: CertificateCorrectionCreate,

    ped: Ped = Depends(
        get_current_ped
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    certificate = await db.scalar(
        select(
            Certificate
        )
        .join(
            Registration,
            Certificate.registration_id
            == Registration.id,
        )
        .where(
            Certificate.id
            == certificate_id,

            Certificate.status
            == 'PUBLISHED',

            Registration.ped_id
            == ped.id,
        )
    )


    if not certificate:
        raise HTTPException(
            status_code=404,
            detail='Certificate not found',
        )


    existing = await db.scalar(
        select(
            CertificateCorrectionRequest
        )
        .where(
            CertificateCorrectionRequest.certificate_id
            == certificate.id,

            CertificateCorrectionRequest.status
            == 'OPEN',
        )
    )


    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                'Correction request '
                'already open'
            ),
        )


    correction_request = (
        CertificateCorrectionRequest(
            certificate_id=
                certificate.id,

            ped_id=
                ped.id,

            reason=
                payload.reason,
        )
    )


    db.add(
        correction_request
    )

    await db.flush()


    await audit(
        db,
        'PED',
        ped.id,
        'REQUEST_CERTIFICATE_CORRECTION',
        'CERTIFICATE_CORRECTION_REQUEST',
        correction_request.id,
        payload.reason,
    )


    await db.commit()


    return MessageResponse(
        message=(
            'Certificate correction '
            'request submitted'
        )
    )