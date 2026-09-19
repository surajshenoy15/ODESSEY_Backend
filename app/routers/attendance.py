from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from sqlalchemy import (
    func,
    select,
)

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import selectinload


from app.core.config import settings

from app.core.database import get_db

from app.core.dependencies import require_admin_roles

from app.core.security import (
    COORDINATOR_QR_ROLES,
    coordinator_qr_matches,
    decode_token,
    utcnow,
)

from app.models.entities import (
    Admin,
    AttendanceRecord,
    Registration,
)

from app.schemas import (
    AttendanceConfirm,
    MessageResponse,
)

from app.services.helpers import audit

from app.services.storage import storage


router = APIRouter(
    prefix='/admin/attendance',
    tags=['Attendance'],
)


# ============================================================
# LOAD REGISTRATION
# ============================================================

async def _load_registration(
    db: AsyncSession,
    rid: str,
):
    return await db.scalar(
        select(Registration)
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
        )
    )


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
        (public_coordinator_data, current_registered_email)

    The email is used internally only to validate the
    coordinator identity fingerprint. It is never returned
    by the attendance API response.
    """

    if not registration.ped:
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,
            detail=
                'Coordinator information is missing for this registration',
        )

    role = (
        str(coordinator_role)
        .strip()
        .upper()
    )

    if role not in COORDINATOR_QR_ROLES:
        raise HTTPException(
            status_code=
                status.HTTP_401_UNAUTHORIZED,
            detail=
                'Invalid coordinator QR role',
        )

    # --------------------------------------------------------
    # PED
    # --------------------------------------------------------

    if role == 'PED':

        name = (
            registration.ped.name
            or ''
        ).strip()

        email = (
            registration.ped.official_email
            or ''
        ).strip().lower()

        if not name or not email:
            raise HTTPException(
                status_code=
                    status.HTTP_409_CONFLICT,
                detail=
                    'PED is no longer registered for this account',
            )

        return (
            {
                'role': 'PED',
                'label': 'PED',
                'name': name,
            },
            email,
        )

    # --------------------------------------------------------
    # COACH
    # --------------------------------------------------------

    if role == 'COACH':

        name = (
            registration.ped.coach_name
            or ''
        ).strip()

        email = (
            registration.ped.coach_email
            or ''
        ).strip().lower()

        if not name or not email:
            raise HTTPException(
                status_code=
                    status.HTTP_409_CONFLICT,
                detail=
                    'Coach is no longer registered for this account',
            )

        return (
            {
                'role': 'COACH',
                'label': 'Coach',
                'name': name,
            },
            email,
        )

    # --------------------------------------------------------
    # MANAGER
    # --------------------------------------------------------

    if role == 'MANAGER':

        name = (
            registration.ped.manager_name
            or ''
        ).strip()

        email = (
            registration.ped.manager_email
            or ''
        ).strip().lower()

        if not name or not email:
            raise HTTPException(
                status_code=
                    status.HTTP_409_CONFLICT,
                detail=
                    'Manager is no longer registered for this account',
            )

        return (
            {
                'role': 'MANAGER',
                'label': 'Manager',
                'name': name,
            },
            email,
        )

    raise HTTPException(
        status_code=
            status.HTTP_401_UNAUTHORIZED,
        detail=
            'Invalid coordinator QR role',
    )


# ============================================================
# CREATE SCAN RESPONSE
# ============================================================

async def _scan_payload(
    registration: Registration,
    coordinator=None,
):
    """
    Build the response used by the attendance frontend.

    coordinator:
        None
            -> legacy/manual registration lookup

        {
            role,
            label,
            name
        }
            -> role-specific coordinator QR
    """

    students = []

    for student in registration.students:

        photo_url = None

        if student.photo_path:
            photo_url = await storage.signed_url(
                settings
                .SUPABASE_BUCKET_STUDENT_PHOTOS,

                student.photo_path,
            )

        students.append(
            {
                'id':
                    student.id,

                'full_name':
                    student.full_name,

                'usn':
                    student.usn,

                'semester':
                    student.current_semester,

                'attendance_status':
                    student.attendance_status,

                'attendance_note':
                    student.attendance_note,

                'photo_url':
                    photo_url,
            }
        )

    coordinator_role = None
    coordinator_label = None
    coordinator_name = None

    if coordinator:
        coordinator_role = (
            coordinator.get(
                'role'
            )
        )

        coordinator_label = (
            coordinator.get(
                'label'
            )
        )

        coordinator_name = (
            coordinator.get(
                'name'
            )
        )

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
                registration
                .event_config
                .sport_name
                if registration.event_config
                else None
            ),

        'event_type':
            (
                registration
                .event_config
                .event_type
                if registration.event_config
                else None
            ),

        'category':
            (
                registration
                .event_config
                .category
                if registration.event_config
                else None
            ),

        # ----------------------------------------------------
        # Only the coordinator represented by this QR.
        #
        # For legacy/manual lookup these values are None.
        # ----------------------------------------------------

        'coordinator':
            coordinator,

        'coordinator_role':
            coordinator_role,

        'coordinator_label':
            coordinator_label,

        'coordinator_name':
            coordinator_name,

        'payment_status':
            registration.payment_status,

        'approval_status':
            registration.status,

        'previous_check_in':
            registration.attendance_confirmed_at,

        'student_count':
            len(
                students
            ),

        'students':
            students,
    }


# ============================================================
# SCAN QR
# ============================================================

@router.get('/scan')
async def scan(
    token: str,

    admin: Admin = Depends(
        require_admin_roles(
            'ATTENDANCE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    """
    Supports:

    1. Legacy registration QR
       type = registration_qr

    2. Role-specific coordinator QR
       type = coordinator_qr

       coordinator_role =
           PED
           COACH
           MANAGER
    """

    # Decode without forcing one type because we support
    # both registration_qr and coordinator_qr.

    payload = decode_token(
        token
    )

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
                status.HTTP_401_UNAUTHORIZED,
            detail=
                'Invalid QR token',
        )

    # --------------------------------------------------------
    # Only accepted attendance QR token types
    # --------------------------------------------------------

    if token_type not in {
        'registration_qr',
        'coordinator_qr',
    }:
        raise HTTPException(
            status_code=
                status.HTTP_401_UNAUTHORIZED,
            detail=
                'Invalid QR token type',
        )

    registration = await _load_registration(
        db,
        registration_id,
    )

    if not registration:
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,
            detail=
                'Registration not found',
        )

    # --------------------------------------------------------
    # Registration MUST still be approved
    # --------------------------------------------------------

    if registration.status != 'APPROVED':
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,
            detail=
                f'Registration status is {registration.status}',
        )

    # ========================================================
    # LEGACY REGISTRATION QR
    # ========================================================

    if token_type == 'registration_qr':

        """
        Legacy/master QR remains supported temporarily.

        It proves the registration is valid but does NOT
        represent PED/Coach/Manager.
        """

        if (
            not registration.qr_token
            or not hmac_compare_tokens(
                registration.qr_token,
                token,
            )
        ):
            raise HTTPException(
                status_code=
                    status.HTTP_404_NOT_FOUND,
                detail=
                    'QR not found or revoked',
            )

        return await _scan_payload(
            registration,
            coordinator=None,
        )

    # ========================================================
    # COORDINATOR QR
    # ========================================================

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
                status.HTTP_401_UNAUTHORIZED,
            detail=
                'Invalid coordinator QR role',
        )

    # --------------------------------------------------------
    # Master QR must still exist
    #
    # If reopened/rejected:
    #
    # registration.qr_token = None
    #
    # Therefore every coordinator QR becomes invalid.
    # --------------------------------------------------------

    if not registration.qr_token:
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,
            detail=
                'Coordinator QR has been revoked',
        )

    # --------------------------------------------------------
    # Resolve ONLY this specific coordinator and obtain the
    # current registered email for identity fingerprint check.
    # --------------------------------------------------------

    (
        coordinator,
        coordinator_email,
    ) = _coordinator_identity(
        registration,
        coordinator_role,
    )

    # --------------------------------------------------------
    # Validate BOTH:
    #
    # 1. current master registration QR generation
    # 2. current coordinator identity/email
    #
    # If a Coach/Manager/PED identity changes, an older QR
    # issued to the previous identity becomes invalid.
    # --------------------------------------------------------

    if not coordinator_qr_matches(
        payload,
        registration.qr_token,
        coordinator_email,
    ):
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,
            detail=(
                'Coordinator QR is expired, revoked or no longer '
                'belongs to the current coordinator'
            ),
        )

    return await _scan_payload(
        registration,
        coordinator=coordinator,
    )


# ============================================================
# CONSTANT-TIME LEGACY QR COMPARISON
# ============================================================

def hmac_compare_tokens(
    stored_token: str,
    supplied_token: str,
):
    """
    Constant-time comparison for the legacy master QR.
    """

    if (
        not stored_token
        or not supplied_token
    ):
        return False

    import hmac

    return hmac.compare_digest(
        str(stored_token),
        str(supplied_token),
    )


# ============================================================
# MANUAL REGISTRATION LOOKUP
# ============================================================

@router.get(
    '/registrations/{rid}'
)
async def lookup_registration(
    rid: str,

    admin: Admin = Depends(
        require_admin_roles(
            'ATTENDANCE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = (
        await _load_registration(
            db,
            rid,
        )
    )

    # If ID was not supplied, try registration code.

    if not registration:

        registration = await db.scalar(
            select(
                Registration
            )
            .where(
                Registration
                .registration_code
                == rid
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
            )
        )

    if not registration:
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,
            detail=
                'Registration not found',
        )

    if registration.status != 'APPROVED':
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,
            detail=(
                f'Registration status is '
                f'{registration.status}'
            ),
        )

    # Manual lookup is registration-level.
    # It must NOT pretend to be PED/Coach/Manager.

    return await _scan_payload(
        registration,
        coordinator=None,
    )


# ============================================================
# CONFIRM ATTENDANCE
# ============================================================

@router.post(
    '/registrations/{rid}/confirm',
    response_model=MessageResponse,
)
async def confirm(
    rid: str,

    payload: AttendanceConfirm,

    admin: Admin = Depends(
        require_admin_roles(
            'ATTENDANCE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    registration = (
        await _load_registration(
            db,
            rid,
        )
    )

    if not registration:
        raise HTTPException(
            status_code=
                status.HTTP_404_NOT_FOUND,
            detail=
                'Registration not found',
        )

    if (
        registration.status
        != 'APPROVED'
    ):
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,
            detail=
                'Only approved registrations can check in',
        )

    if (
        registration
        .attendance_confirmed_at
        is not None
    ):
        raise HTTPException(
            status_code=
                status.HTTP_409_CONFLICT,

            detail={
                'message':
                    'Attendance already confirmed. Duplicate scanning is blocked.',

                'checked_in_at':
                    registration
                    .attendance_confirmed_at
                    .isoformat(),
            },
        )

    # ========================================================
    # VALIDATE COMPLETE ROSTER
    # ========================================================

    student_map = {
        student.id:
            student

        for student
        in registration.students
    }

    given = {
        item.student_id

        for item
        in payload.students
    }

    if (
        given
        != set(
            student_map
        )
    ):
        raise HTTPException(
            status_code=
                status.HTTP_422_UNPROCESSABLE_ENTITY,

            detail={
                'message':
                    'Every roster student must be marked',

                'missing':
                    list(
                        set(student_map)
                        - given
                    ),

                'invalid':
                    list(
                        given
                        - set(student_map)
                    ),
            },
        )

    # ========================================================
    # ATTENDANCE VERSION
    # ========================================================

    version = (
        int(
            await db.scalar(
                select(
                    func.max(
                        AttendanceRecord.version
                    )
                )
                .where(
                    AttendanceRecord
                    .registration_id
                    == registration.id
                )
            )
            or 0
        )
        + 1
    )

    now = utcnow()

    present = 0

    # ========================================================
    # MARK EACH STUDENT
    # ========================================================

    for item in payload.students:

        student = (
            student_map[
                item.student_id
            ]
        )

        student.attendance_status = (
            'PRESENT'
            if item.is_present
            else 'ABSENT'
        )

        student.attendance_note = (
            item.note
        )

        student.attendance_checked_at = (
            now
        )

        student.attendance_checked_by = (
            admin.id
        )

        present += int(
            item.is_present
        )

        db.add(
            AttendanceRecord(
                registration_id=
                    registration.id,

                student_id=
                    student.id,

                is_present=
                    item.is_present,

                note=
                    item.note,

                gate=
                    payload.gate,

                admin_id=
                    admin.id,

                version=
                    version,
            )
        )

    # ========================================================
    # FINALIZE REGISTRATION ATTENDANCE
    # ========================================================

    registration.attendance_confirmed_at = (
        now
    )

    registration.attendance_confirmed_by = (
        admin.id
    )

    absent = (
        len(
            registration.students
        )
        - present
    )

    # ========================================================
    # AUDIT
    # ========================================================

    await audit(
        db,

        'ADMIN',

        admin.id,

        'CONFIRM_ATTENDANCE',

        'REGISTRATION',

        registration.id,

        payload.confirmation_note,

        {
            'version':
                version,

            'present':
                present,

            'absent':
                absent,

            'gate':
                payload.gate,
        },
    )

    await db.commit()

    return MessageResponse(
        message=(
            f'Attendance saved: '
            f'{present} present, '
            f'{absent} absent'
        )
    )


# ============================================================
# ATTENDANCE HISTORY
# ============================================================

@router.get(
    '/registrations/{rid}/history'
)
async def history(
    rid: str,

    admin: Admin = Depends(
        require_admin_roles(
            'ATTENDANCE_ADMIN'
        )
    ),

    db: AsyncSession = Depends(
        get_db
    ),
):
    return (
        await db.scalars(
            select(
                AttendanceRecord
            )
            .where(
                AttendanceRecord
                .registration_id
                == rid
            )
            .order_by(
                AttendanceRecord
                .version
                .desc(),

                AttendanceRecord
                .created_at
                .desc(),
            )
        )
    ).all()