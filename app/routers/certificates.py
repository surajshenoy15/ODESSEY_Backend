import json
import mimetypes
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_admin_roles
from app.core.security import utcnow
from app.models.entities import (
    Admin,
    Certificate,
    CertificateCorrectionRequest,
    CertificateTemplate,
    EventConfig,
    Registration,
    Student,
)
from app.schemas import (
    CertificateCorrectionResolve,
    CertificateGenerateIn,
    CertificateMapping,
    CertificateEligibilityOverride,
    MessageResponse,
)
from app.services.certificates import generate_certificate_pdf
from app.services.email import certificate_html, email_service
from app.services.helpers import audit, safe_filename
from app.services.storage import storage

router = APIRouter(prefix="/admin/certificates", tags=["Certificates"])


@router.post("/templates", status_code=201)
async def upload_template(
    name: str = Form(...),
    event_config_id: str | None = Form(None),
    field_mapping_json: str = Form("{}"),
    file: UploadFile = File(...),
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    if file.content_type not in {"application/pdf", "image/jpeg", "image/png"}:
        raise HTTPException(status_code=415, detail="Template must be PDF/JPG/PNG")
    data = await file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Template exceeds 20 MB")
    if event_config_id and not await db.get(EventConfig, event_config_id):
        raise HTTPException(status_code=404, detail="Event not found")
    try:
        mapping = CertificateMapping(**json.loads(field_mapping_json)).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid mapping JSON: {exc}") from exc

    extension = mimetypes.guess_extension(file.content_type) or ".pdf"
    path = f"{event_config_id or 'general'}/{uuid.uuid4().hex}{extension}"
    await storage.upload(
        settings.SUPABASE_BUCKET_CERTIFICATE_TEMPLATES,
        path,
        data,
        file.content_type,
    )
    template = CertificateTemplate(
        event_config_id=event_config_id,
        name=name,
        file_path=path,
        file_type="pdf" if file.content_type == "application/pdf" else "image",
        field_mapping=mapping,
        uploaded_by=admin.id,
    )
    db.add(template)
    await db.flush()
    await audit(
        db,
        "ADMIN",
        admin.id,
        "UPLOAD_CERTIFICATE_TEMPLATE",
        "CERTIFICATE_TEMPLATE",
        template.id,
    )
    await db.commit()
    return {"id": template.id, "name": template.name, "mapping": template.field_mapping}


@router.get("/templates")
async def list_templates(
    event_config_id: str | None = None,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    query = select(CertificateTemplate).order_by(CertificateTemplate.created_at.desc())
    if event_config_id:
        query = query.where(
            CertificateTemplate.event_config_id.in_([event_config_id, None])
        )
    return (await db.scalars(query)).all()


@router.get("/eligible/{event_config_id}")
async def eligible_students(
    event_config_id: str,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Student, Registration)
            .join(Registration, Student.registration_id == Registration.id)
            .where(
                Registration.event_config_id == event_config_id,
                Registration.status == "APPROVED",
                or_(Student.attendance_status == "PRESENT", Student.certificate_override.is_(True)),
            )
            .order_by(Registration.college_name, Student.full_name)
        )
    ).all()
    return [
        {
            "student_id": student.id,
            "name": student.full_name,
            "usn": student.usn,
            "college": registration.college_name,
            "registration_id": registration.id,
            "registration_code": registration.registration_code,
        }
        for student, registration in rows
    ]


@router.get("/students/{event_config_id}")
async def event_students_for_certificate_review(
    event_config_id: str,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Student, Registration)
            .join(Registration, Student.registration_id == Registration.id)
            .where(
                Registration.event_config_id == event_config_id,
                Registration.status == "APPROVED",
            )
            .order_by(Registration.college_name, Student.full_name)
        )
    ).all()
    return [
        {
            "student_id": student.id,
            "name": student.full_name,
            "email": student.email,
            "usn": student.usn,
            "college": registration.college_name,
            "registration_id": registration.id,
            "registration_code": registration.registration_code,
            "attendance_status": student.attendance_status,
            "certificate_override": student.certificate_override,
            "certificate_override_reason": student.certificate_override_reason,
            "eligible": student.attendance_status == "PRESENT" or bool(student.certificate_override),
        }
        for student, registration in rows
    ]


@router.post("/eligibility/{student_id}/override", response_model=MessageResponse)
async def override_certificate_eligibility(
    student_id: str,
    payload: CertificateEligibilityOverride,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    student = await db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    student.certificate_override = payload.eligible
    student.certificate_override_reason = payload.reason
    student.certificate_override_at = utcnow()
    student.certificate_override_by = admin.id
    await audit(
        db,
        "ADMIN",
        admin.id,
        "CERTIFICATE_ELIGIBILITY_OVERRIDE",
        "STUDENT",
        student.id,
        payload.reason,
        {"eligible": payload.eligible},
    )
    await db.commit()
    return MessageResponse(message=("Student added to certificate eligibility" if payload.eligible else "Student removed from manual certificate eligibility"))


async def _render_certificate(
    template: CertificateTemplate,
    event: EventConfig,
    student: Student,
    registration: Registration,
    number: str,
    version: int,
) -> tuple[bytes, str]:
    template_bytes = await storage.download(
        settings.SUPABASE_BUCKET_CERTIFICATE_TEMPLATES, template.file_path
    )
    pdf = generate_certificate_pdf(
        template_bytes,
        template.file_type,
        template.field_mapping,
        student.full_name,
        student.usn,
        registration.college_name,
        event.sport_name,
        event.category,
        number,
        event.event_date,
    )
    path = (
        f"{event.id}/{registration.id}/{safe_filename(student.usn)}-v{version}.pdf"
    )
    await storage.upload(
        settings.SUPABASE_BUCKET_CERTIFICATES,
        path,
        pdf,
        "application/pdf",
    )
    return pdf, path


@router.post("/generate")
async def generate_certificates(
    payload: CertificateGenerateIn,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(CertificateTemplate, payload.template_id)
    event = await db.get(EventConfig, payload.event_config_id)
    if not template or not template.is_active:
        raise HTTPException(status_code=404, detail="Active template not found")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if template.event_config_id and template.event_config_id != event.id:
        raise HTTPException(status_code=409, detail="Template assigned to another event")

    rows = (
        await db.execute(
            select(Student, Registration)
            .join(Registration, Student.registration_id == Registration.id)
            .where(
                Registration.event_config_id == event.id,
                Registration.status == "APPROVED",
                or_(Student.attendance_status == "PRESENT", Student.certificate_override.is_(True)),
            )
            .order_by(Registration.college_name, Student.full_name)
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=409, detail="No attendance-verified students")

    sequence = int(await db.scalar(select(func.count(Certificate.id))) or 0)
    generated = 0
    skipped = 0
    for student, registration in rows:
        existing = await db.scalar(
            select(Certificate)
            .where(Certificate.student_id == student.id)
            .order_by(Certificate.version.desc())
            .limit(1)
        )
        if existing:
            skipped += 1
            continue
        sequence += 1
        number = (
            f"ODY/{event.sport_name[:3].upper()}/{utcnow():%Y}/{sequence:06d}"
        )
        _, path = await _render_certificate(
            template, event, student, registration, number, 1
        )
        db.add(
            Certificate(
                student_id=student.id,
                registration_id=registration.id,
                template_id=template.id,
                certificate_number=number,
                version=1,
                file_path=path,
                status="PUBLISHED" if payload.publish_immediately else "DRAFT",
                generated_by=admin.id,
                published_at=utcnow() if payload.publish_immediately else None,
            )
        )
        generated += 1

    await audit(
        db,
        "ADMIN",
        admin.id,
        "GENERATE_CERTIFICATES",
        "EVENT_CONFIG",
        event.id,
        details={
            "generated": generated,
            "skipped": skipped,
            "published": payload.publish_immediately,
        },
    )
    await db.commit()
    return {
        "generated": generated,
        "skipped_existing": skipped,
        "status": "PUBLISHED" if payload.publish_immediately else "DRAFT",
    }


@router.post("/publish/{event_config_id}", response_model=MessageResponse)
async def publish_certificates(
    event_config_id: str,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(EventConfig, event_config_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    rows = (
        await db.execute(
            select(Certificate, Registration, Student)
            .join(Registration, Certificate.registration_id == Registration.id)
            .join(Student, Certificate.student_id == Student.id)
            .where(
                Registration.event_config_id == event_config_id,
                Certificate.status == "DRAFT",
            )
            .options(selectinload(Registration.ped))
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="No draft certificates")

    ped_counts: dict[str, int] = defaultdict(int)
    student_emails = 0
    for certificate, registration, student in rows:
        certificate.status = "PUBLISHED"
        certificate.published_at = utcnow()
        ped_counts[registration.ped.official_email] += 1
        pdf = await storage.download(settings.SUPABASE_BUCKET_CERTIFICATES, certificate.file_path)
        html = certificate_html(
            student.full_name,
            event.sport_name,
            event.category,
            f"{settings.PUBLIC_APP_URL.rstrip('/')}/#/ped",
        )
        attachment = [(f"{safe_filename(student.full_name)}-{safe_filename(student.usn)}.pdf", pdf)]
        if student.email:
            await email_service.send(
                db,
                student.email,
                f"BNMIT ODYSSEY certificate — {event.sport_name}",
                html,
                "CERTIFICATE_STUDENT",
                registration.id,
                attachment,
            )
            student_emails += 1
        await email_service.send(
            db,
            registration.ped.official_email,
            f"BNMIT ODYSSEY certificate — {student.full_name}",
            html,
            "CERTIFICATE_PED",
            registration.id,
            attachment,
        )

    await audit(
        db,
        "ADMIN",
        admin.id,
        "PUBLISH_CERTIFICATES",
        "EVENT_CONFIG",
        event_config_id,
        details={"certificates": len(rows), "ped_recipients": len(ped_counts), "student_emails": student_emails},
    )
    await db.commit()
    return MessageResponse(
        message=f"Published {len(rows)} certificates; emailed {student_emails} student(s) and {len(ped_counts)} PED account(s)"
    )


@router.get("/correction-requests")
async def correction_requests(
    status: str | None = "OPEN",
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    query = select(CertificateCorrectionRequest).order_by(
        CertificateCorrectionRequest.created_at.desc()
    )
    if status:
        query = query.where(CertificateCorrectionRequest.status == status)
    return (await db.scalars(query)).all()


@router.post("/correction-requests/{request_id}/resolve", response_model=MessageResponse)
async def resolve_correction_request(
    request_id: str,
    payload: CertificateCorrectionResolve,
    admin: Admin = Depends(require_admin_roles("CERTIFICATE_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    request = await db.get(CertificateCorrectionRequest, request_id)
    if not request or request.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open correction request not found")
    certificate = await db.scalar(
        select(Certificate)
        .where(Certificate.id == request.certificate_id)
        .options(
            selectinload(Certificate.student),
            selectinload(Certificate.registration).selectinload(
                Registration.event_config
            ),
            selectinload(Certificate.registration).selectinload(Registration.ped),
            selectinload(Certificate.template),
        )
    )
    if not certificate:
        raise HTTPException(status_code=404, detail="Certificate not found")

    request.admin_note = payload.admin_note
    request.resolved_by = admin.id
    request.resolved_at = utcnow()
    if payload.action == "REJECT":
        request.status = "REJECTED"
    else:
        latest_version = int(
            await db.scalar(
                select(func.max(Certificate.version)).where(
                    Certificate.student_id == certificate.student_id
                )
            )
            or certificate.version
        )
        next_version = latest_version + 1
        number = f"{certificate.certificate_number}-R{next_version - 1}"
        event = certificate.registration.event_config
        _, path = await _render_certificate(
            certificate.template,
            event,
            certificate.student,
            certificate.registration,
            number,
            next_version,
        )
        db.add(
            Certificate(
                student_id=certificate.student_id,
                registration_id=certificate.registration_id,
                template_id=certificate.template_id,
                certificate_number=number,
                version=next_version,
                file_path=path,
                status="PUBLISHED",
                generated_by=admin.id,
                published_at=utcnow(),
                correction_reason=request.reason,
            )
        )
        request.status = "RESOLVED"
        await email_service.send(
            db,
            certificate.registration.ped.official_email,
            "Corrected BNMIT ODYSSEY certificate is ready",
            "<h2>Certificate corrected</h2><p>The revised certificate is available in your PED dashboard.</p>",
            "CERTIFICATE_CORRECTED",
            certificate.registration_id,
        )

    await audit(
        db,
        "ADMIN",
        admin.id,
        "RESOLVE_CERTIFICATE_CORRECTION",
        "CERTIFICATE_CORRECTION_REQUEST",
        request.id,
        payload.admin_note,
        {"action": payload.action},
    )
    await db.commit()
    return MessageResponse(message=f"Correction request {request.status.lower()}")
