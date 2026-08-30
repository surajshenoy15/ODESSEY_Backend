from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


def uid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(50), default="SUPER_ADMIN", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ped(Base, TimestampMixin):
    __tablename__ = "peds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    official_email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(150))
    college_name: Mapped[str | None] = mapped_column(String(255), index=True)
    college_location: Mapped[str | None] = mapped_column(String(255))
    contact_number: Mapped[str | None] = mapped_column(String(20))
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    declaration_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registrations: Mapped[list["Registration"]] = relationship(back_populates="ped")


class OtpCode(Base):
    __tablename__ = "otp_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255), index=True)
    purpose: Mapped[str] = mapped_column(String(50), default="PED_LOGIN")
    otp_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_otp_email_purpose_created", "email", "purpose", "created_at"),)


class EventConfig(Base, TimestampMixin):
    __tablename__ = "event_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # Kept as sport_name for backward compatibility. UI presents this as Event name.
    sport_name: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(30), default="SPORTS", index=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    poster_path: Mapped[str | None] = mapped_column(String(500))
    fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    team_min_size: Mapped[int] = mapped_column(Integer, default=1)
    team_max_size: Mapped[int] = mapped_column(Integer, default=20)
    max_substitutes: Mapped[int] = mapped_column(Integer, default=0)
    registration_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    venue: Mapped[str | None] = mapped_column(String(255))
    reporting_instructions: Mapped[str | None] = mapped_column(Text)
    is_registration_open: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("sport_name", "category", name="uq_event_sport_category"),)


class Registration(Base, TimestampMixin):
    __tablename__ = "registrations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    registration_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    ped_id: Mapped[str] = mapped_column(ForeignKey("peds.id", ondelete="CASCADE"), index=True)
    event_config_id: Mapped[str] = mapped_column(ForeignKey("event_configs.id"), index=True)
    college_name: Mapped[str] = mapped_column(String(255), index=True)
    college_location: Mapped[str | None] = mapped_column(String(255))
    team_name: Mapped[str | None] = mapped_column(String(150))
    coach_name: Mapped[str | None] = mapped_column(String(150))
    ped_contact: Mapped[str] = mapped_column(String(20))
    student_coordinator_name: Mapped[str | None] = mapped_column(String(150))
    student_coordinator_contact: Mapped[str] = mapped_column(String(20))
    bonafide_path: Mapped[str | None] = mapped_column(String(500))
    declaration_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", index=True)
    payment_status: Mapped[str] = mapped_column(String(40), default="UNPAID", index=True)
    fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    admin_note: Mapped[str | None] = mapped_column(Text)
    correction_fields: Mapped[list[str] | None] = mapped_column(JSON)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))
    qr_token: Mapped[str | None] = mapped_column(Text)
    attendance_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attendance_confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))
    ped: Mapped[Ped] = relationship(back_populates="registrations")
    event_config: Mapped[EventConfig] = relationship()
    students: Mapped[list["Student"]] = relationship(back_populates="registration", cascade="all, delete-orphan", order_by="Student.full_name")
    payments: Mapped[list["Payment"]] = relationship(back_populates="registration")
    __table_args__ = (
        Index("ix_reg_event_status", "event_config_id", "status"),
        Index("ix_reg_college_status", "college_name", "status"),
    )


class Student(Base, TimestampMixin):
    __tablename__ = "students"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    registration_id: Mapped[str] = mapped_column(ForeignKey("registrations.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(180))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    usn: Mapped[str] = mapped_column(String(80), index=True)
    current_semester: Mapped[int] = mapped_column(Integer)
    contact_number: Mapped[str] = mapped_column(String(20))
    photo_path: Mapped[str | None] = mapped_column(String(500))
    attendance_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN", index=True)
    attendance_note: Mapped[str | None] = mapped_column(Text)
    attendance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attendance_checked_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))
    certificate_override: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    certificate_override_reason: Mapped[str | None] = mapped_column(Text)
    certificate_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    certificate_override_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))
    registration: Mapped[Registration] = relationship(back_populates="students")
    __table_args__ = (UniqueConstraint("registration_id", "usn", name="uq_student_registration_usn"),)


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    registration_id: Mapped[str] = mapped_column(ForeignKey("registrations.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30), default="RAZORPAY")
    order_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    payment_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    signature: Mapped[str | None] = mapped_column(String(255))
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    status: Mapped[str] = mapped_column(String(30), default="CREATED", index=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration: Mapped[Registration] = relationship(back_populates="payments")


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    registration_id: Mapped[str] = mapped_column(ForeignKey("registrations.id"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    is_present: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    gate: Mapped[str | None] = mapped_column(String(100))
    admin_id: Mapped[str] = mapped_column(ForeignKey("admins.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Fixture(Base, TimestampMixin):
    __tablename__ = "fixtures"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_config_id: Mapped[str | None] = mapped_column(ForeignKey("event_configs.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    file_path: Mapped[str] = mapped_column(String(500))
    visibility: Mapped[str] = mapped_column(String(30), default="RELEVANT_PEDS")
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("admins.id"))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("fixtures.id"))
    event_config: Mapped[EventConfig | None] = relationship()


class LiveStream(Base, TimestampMixin):
    __tablename__ = "live_streams"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_config_id: Mapped[str | None] = mapped_column(ForeignKey("event_configs.id"), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    youtube_url: Mapped[str] = mapped_column(String(500))
    visibility: Mapped[str] = mapped_column(String(30), default="PUBLIC")
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED")
    offline_message: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(ForeignKey("admins.id"))


class CertificateTemplate(Base, TimestampMixin):
    __tablename__ = "certificate_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_config_id: Mapped[str | None] = mapped_column(ForeignKey("event_configs.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    file_path: Mapped[str] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(20))
    field_mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("admins.id"))


class Certificate(Base, TimestampMixin):
    __tablename__ = "certificates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    registration_id: Mapped[str] = mapped_column(ForeignKey("registrations.id"), index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("certificate_templates.id"))
    certificate_number: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    file_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    generated_by: Mapped[str] = mapped_column(ForeignKey("admins.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_reason: Mapped[str | None] = mapped_column(Text)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    student: Mapped[Student] = relationship()
    registration: Mapped[Registration] = relationship()
    template: Mapped[CertificateTemplate] = relationship()
    __table_args__ = (UniqueConstraint("student_id", "version", name="uq_cert_student_version"),)


class CertificateCorrectionRequest(Base, TimestampMixin):
    __tablename__ = "certificate_correction_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    certificate_id: Mapped[str] = mapped_column(ForeignKey("certificates.id"), index=True)
    ped_id: Mapped[str] = mapped_column(ForeignKey("peds.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    admin_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailLog(Base):
    __tablename__ = "email_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    recipient: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    message_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    related_registration_id: Mapped[str | None] = mapped_column(ForeignKey("registrations.id"))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_type: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(30), default="RAZORPAY")
    external_event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
