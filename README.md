# BNMIT ODYSSEY FastAPI Backend

FastAPI application for public event data, PED OTP identity, team registration, Razorpay payment, admin approval, signed QR attendance, fixtures, live streams, certificates, Brevo communication, reports and audit history.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m app.scripts.seed_admin
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `/docs` for Swagger/OpenAPI.

## Supabase

Use a Supabase PostgreSQL URL in `DATABASE_URL`, set `STORAGE_BACKEND=supabase`, configure `SUPABASE_URL` and the backend-only service-role key, then run:

```bash
alembic upgrade head
python -m app.scripts.setup_supabase_storage
```

## Local tests

```bash
python -m pytest -q
```

The root `postman/` folder contains the complete API collection and sample uploads.
