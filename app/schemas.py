from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    message: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    actor_type: str
    role: str


class PedOtpRequest(BaseModel):
    email: EmailStr


class PedOtpResponse(BaseModel):
    message: str
    expires_in_seconds: int
    debug_otp: str | None = None


class PedOtpVerify(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6)
    name: str | None = Field(None, max_length=150)
    college_name: str | None = Field(None, max_length=255)
    college_location: str | None = Field(None, max_length=255)
    contact_number: str | None = Field(None, min_length=8, max_length=20)
    declaration_accepted: bool = False


class AdminLogin(BaseModel):
    email: EmailStr
    password: str


class PedProfileUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    college_name: str = Field(min_length=2, max_length=255)
    college_location: str | None = None
    contact_number: str = Field(min_length=8, max_length=20)
    declaration_accepted: bool = True


class PedOut(ORM):
    id: str
    official_email: EmailStr
    name: str | None
    college_name: str | None
    college_location: str | None
    contact_number: str | None
    is_email_verified: bool
    created_at: datetime


Category = Literal['PU Boys', 'PU Girls', 'Engineering Boys', 'Engineering Girls']
EventType = Literal['SPORTS', 'CULTURAL']


class EventCreate(BaseModel):
    sport_name: str = Field(min_length=2, max_length=100)
    event_type: EventType = 'SPORTS'
    category: Category
    description: str | None = Field(None, max_length=5000)
    fee_paise: int = Field(ge=0)
    team_min_size: int = Field(ge=1, le=100)
    team_max_size: int = Field(ge=1, le=100)
    max_substitutes: int = Field(0, ge=0, le=50)
    registration_opens_at: datetime | None = None
    registration_closes_at: datetime | None = None
    event_date: datetime | None = None
    venue: str | None = None
    reporting_instructions: str | None = None
    is_registration_open: bool = True
    is_active: bool = True

    @field_validator('team_max_size')
    @classmethod
    def check_size(cls, value, info):
        if info.data.get('team_min_size') and value < info.data['team_min_size']:
            raise ValueError('team_max_size must be >= team_min_size')
        return value


class EventUpdate(BaseModel):
    sport_name: str | None = Field(None, min_length=2, max_length=100)
    event_type: EventType | None = None
    category: Category | None = None
    description: str | None = Field(None, max_length=5000)
    fee_paise: int | None = Field(None, ge=0)
    team_min_size: int | None = Field(None, ge=1, le=100)
    team_max_size: int | None = Field(None, ge=1, le=100)
    max_substitutes: int | None = Field(None, ge=0, le=50)
    registration_opens_at: datetime | None = None
    registration_closes_at: datetime | None = None
    event_date: datetime | None = None
    venue: str | None = None
    reporting_instructions: str | None = None
    is_registration_open: bool | None = None
    is_active: bool | None = None


class EventOut(ORM):
    id: str
    sport_name: str
    event_type: str
    category: str
    description: str | None
    poster_path: str | None
    fee_paise: int
    team_min_size: int
    team_max_size: int
    max_substitutes: int
    registration_opens_at: datetime | None
    registration_closes_at: datetime | None
    event_date: datetime | None
    venue: str | None
    reporting_instructions: str | None
    is_registration_open: bool
    is_active: bool


class RegistrationCreate(BaseModel):
    event_config_id: str
    college_name: str = Field(min_length=2, max_length=255)
    college_location: str | None = None
    team_name: str | None = Field(None, max_length=150)
    coach_name: str | None = Field(None, max_length=150)
    ped_contact: str = Field(min_length=8, max_length=20)
    student_coordinator_name: str | None = Field(None, max_length=150)
    student_coordinator_contact: str = Field(min_length=8, max_length=20)
    declaration_accepted: bool = False
    consent_accepted: bool = False


class RegistrationUpdate(BaseModel):
    college_name: str | None = Field(None, min_length=2, max_length=255)
    college_location: str | None = None
    team_name: str | None = Field(None, max_length=150)
    coach_name: str | None = Field(None, max_length=150)
    ped_contact: str | None = Field(None, min_length=8, max_length=20)
    student_coordinator_name: str | None = Field(None, max_length=150)
    student_coordinator_contact: str | None = Field(None, min_length=8, max_length=20)
    declaration_accepted: bool | None = None
    consent_accepted: bool | None = None


class StudentCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=180)
    email: EmailStr
    usn: str = Field(min_length=2, max_length=80)
    current_semester: int = Field(ge=1, le=12)
    contact_number: str = Field(min_length=8, max_length=20)

    @field_validator('usn')
    @classmethod
    def usn_upper(cls, value):
        return value.strip().upper()


class StudentUpdate(BaseModel):
    full_name: str | None = Field(None, min_length=2, max_length=180)
    email: EmailStr | None = None
    usn: str | None = Field(None, min_length=2, max_length=80)
    current_semester: int | None = Field(None, ge=1, le=12)
    contact_number: str | None = Field(None, min_length=8, max_length=20)

    @field_validator('usn')
    @classmethod
    def usn_upper(cls, value):
        return value.strip().upper() if value else value


class StudentOut(ORM):
    id: str
    full_name: str
    email: EmailStr | None
    usn: str
    current_semester: int
    contact_number: str
    photo_path: str | None
    attendance_status: str
    attendance_note: str | None
    certificate_override: bool = False
    certificate_override_reason: str | None = None


class RegistrationOut(ORM):
    id: str
    registration_code: str
    event_config_id: str
    college_name: str
    college_location: str | None
    team_name: str | None
    coach_name: str | None
    ped_contact: str
    student_coordinator_name: str | None
    student_coordinator_contact: str
    bonafide_path: str | None
    declaration_accepted: bool
    consent_accepted: bool
    status: str
    payment_status: str
    fee_paise: int
    admin_note: str | None
    correction_fields: list[str] | None
    qr_token: str | None
    created_at: datetime
    approved_at: datetime | None
    attendance_confirmed_at: datetime | None
    students: list[StudentOut] = Field(default_factory=list)


class PaymentOrderOut(BaseModel):
    key_id: str
    order_id: str
    amount: int
    currency: str
    registration_id: str
    registration_code: str
    test_mode: bool = False


class PaymentVerifyIn(BaseModel):
    registration_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class ReviewAction(BaseModel):
    action: Literal['APPROVE', 'REQUEST_CORRECTION', 'REJECT', 'REOPEN']
    reason: str | None = Field(None, max_length=2000)
    correction_fields: list[str] | None = None


class AttendanceItem(BaseModel):
    student_id: str
    is_present: bool
    note: str | None = Field(None, max_length=1000)


class AttendanceConfirm(BaseModel):
    students: list[AttendanceItem] = Field(min_length=1)
    gate: str | None = Field(None, max_length=100)
    confirmation_note: str | None = Field(None, max_length=1000)


class CertificateEligibilityOverride(BaseModel):
    eligible: bool = True
    reason: str = Field(min_length=3, max_length=1000)


class LiveStreamUpsert(BaseModel):
    event_config_id: str | None = None
    title: str = Field(min_length=2, max_length=200)
    youtube_url: HttpUrl
    visibility: Literal['PUBLIC', 'PED_ONLY', 'HIDDEN'] = 'PUBLIC'
    status: Literal['SCHEDULED', 'LIVE', 'ENDED', 'OFFLINE'] = 'SCHEDULED'
    offline_message: str | None = None
    scheduled_at: datetime | None = None


class CertificateMapping(BaseModel):
    page_width: float = 842
    page_height: float = 595
    name_x: float = 421
    name_y: float = 310
    name_font_size: float = 26
    details_x: float = 421
    details_y: float = 265
    details_font_size: float = 13
    number_x: float = 70
    number_y: float = 40
    number_font_size: float = 9
    text_color: str = '#000000'


class CertificateGenerateIn(BaseModel):
    template_id: str
    event_config_id: str
    publish_immediately: bool = False


class DashboardStats(BaseModel):
    total_registrations: int
    paid_registrations: int
    under_review: int
    approved: int
    rejected: int
    attendance_verified: int
    present_students: int
    certificates_published: int


class AdminCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    role: Literal['SUPER_ADMIN', 'REGISTRATION_ADMIN', 'ATTENDANCE_ADMIN', 'FIXTURE_ADMIN', 'CERTIFICATE_ADMIN']


class AdminUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=150)
    email: EmailStr | None = None
    role: Literal['SUPER_ADMIN', 'REGISTRATION_ADMIN', 'ATTENDANCE_ADMIN', 'FIXTURE_ADMIN', 'CERTIFICATE_ADMIN'] | None = None
    is_active: bool | None = None
    password: str | None = Field(None, min_length=1, max_length=128)


class AdminOut(ORM):
    id: str
    name: str
    email: EmailStr
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class CertificateCorrectionCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class CertificateCorrectionResolve(BaseModel):
    action: Literal['APPROVE_REISSUE', 'REJECT']
    admin_note: str = Field(min_length=2, max_length=2000)
