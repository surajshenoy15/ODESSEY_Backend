from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import create_tables
from app.routers import (
    admin,
    attendance,
    auth,
    certificates,
    files,
    payments,
    ped,
    public,
    reports,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.AUTO_CREATE_TABLES:
        await create_tables()

    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    description=(
        "BNMIT ODYSSEY backend: dynamic sports/cultural events, "
        "PED OTP, registration, Razorpay, admin approval, QR attendance, "
        "fixtures, live streams, certificates and reports."
    ),
    lifespan=lifespan,
)

print("CORS ORIGINS:", settings.cors_origins_list)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["System"])
async def health():
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
    }


for router in (
    auth.router,
    public.router,
    ped.router,
    payments.router,
    admin.router,
    attendance.router,
    certificates.router,
    reports.router,
    files.router,
):
    app.include_router(
        router,
        prefix=settings.API_V1_PREFIX,
    )