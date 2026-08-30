from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import EmailStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_NAME: str = "BNMIT ODYSSEY API"
    ENVIRONMENT: Literal["development", "production"] = "development"
    API_V1_PREFIX: str = "/api/v1"

    SECRET_KEY: str = "development-only-change-me-use-at-least-32-bytes"

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    OTP_EXPIRE_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    OTP_RESEND_COOLDOWN_SECONDS: int = 60

    DATABASE_URL: str = "sqlite+aiosqlite:///./odyssey.db"
    DB_REQUIRE_SSL: bool = False
    AUTO_CREATE_TABLES: bool = True

    # -----------------------------------------------------
    # CORS
    # -----------------------------------------------------

    CORS_ORIGINS: str = (
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:5174,"
        "http://127.0.0.1:5174"
        "https://odessey-frontend.vercel.app"
        "https://odessey-admin-frontend.vercel.app"
    )

    PUBLIC_APP_URL: str = "https://odessey-frontend.vercel.app"
    API_PUBLIC_URL: str = "https://odessey-backend.onrender.com"

    ALLOWED_PED_EMAIL_DOMAINS: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    @property
    def allowed_ped_email_domains_list(self) -> list[str]:
        return [
            domain.strip()
            for domain in self.ALLOWED_PED_EMAIL_DOMAINS.split(",")
            if domain.strip()
        ]

    # -----------------------------------------------------
    # STORAGE
    # -----------------------------------------------------

    STORAGE_BACKEND: Literal["local", "supabase"] = "local"

    LOCAL_STORAGE_DIR: Path = Path("./storage")

    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    SUPABASE_BUCKET_STUDENT_PHOTOS: str = "student-photos"
    SUPABASE_BUCKET_BONAFIDES: str = "bonafides"
    SUPABASE_BUCKET_FIXTURES: str = "fixtures"
    SUPABASE_BUCKET_EVENT_MEDIA: str = "event-media"
    SUPABASE_BUCKET_CERTIFICATE_TEMPLATES: str = "certificate-templates"
    SUPABASE_BUCKET_CERTIFICATES: str = "certificates"

    # -----------------------------------------------------
    # BREVO
    # -----------------------------------------------------

    BREVO_API_KEY: str = ""
    BREVO_SENDER_EMAIL: EmailStr = "noreply@example.com"
    BREVO_SENDER_NAME: str = "BNMIT ODYSSEY"
    BREVO_SANDBOX_MODE: bool = False

    # -----------------------------------------------------
    # RAZORPAY
    # -----------------------------------------------------

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_CURRENCY: str = "INR"

    # -----------------------------------------------------
    # TEST SETTINGS
    # -----------------------------------------------------

    TEST_MODE: bool = False
    RETURN_OTP_IN_RESPONSE: bool = False
    ALLOW_TEST_PAYMENT: bool = False

    # -----------------------------------------------------
    # INITIAL ADMIN
    # -----------------------------------------------------

    INITIAL_ADMIN_EMAIL: EmailStr = "admin@bnmit.in"
    INITIAL_ADMIN_PASSWORD: str = "ChangeMe123!"
    INITIAL_ADMIN_NAME: str = "BNMIT Odyssey Super Admin"

    # -----------------------------------------------------
    # DATABASE NORMALIZATION
    # -----------------------------------------------------

    @field_validator("DATABASE_URL")
    @classmethod
    def normalize_db(cls, value: str):
        if value.startswith("postgres://"):
            return value.replace(
                "postgres://",
                "postgresql+asyncpg://",
                1,
            )

        if (
            value.startswith("postgresql://")
            and "+asyncpg" not in value
        ):
            return value.replace(
                "postgresql://",
                "postgresql+asyncpg://",
                1,
            )

        return value

    def validate_production(self):
        if self.ENVIRONMENT != "production":
            return

        errors = []

        if self.SECRET_KEY in {
            "development-only-change-me",
            "development-only-change-me-use-at-least-32-bytes",
            "change-this-to-a-long-random-secret",
        }:
            errors.append("replace SECRET_KEY")

        if (
            self.TEST_MODE
            or self.RETURN_OTP_IN_RESPONSE
            or self.ALLOW_TEST_PAYMENT
        ):
            errors.append("disable test flags")

        if (
            self.STORAGE_BACKEND == "supabase"
            and not (
                self.SUPABASE_URL
                and self.SUPABASE_SERVICE_ROLE_KEY
            )
        ):
            errors.append("configure Supabase")

        if errors:
            raise RuntimeError(
                "Unsafe production configuration: "
                + "; ".join(errors)
            )


@lru_cache
def get_settings():
    settings = Settings()
    settings.validate_production()
    return settings


settings = get_settings()