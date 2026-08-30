import asyncio

import httpx

from app.core.config import settings


async def main():
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY first.")
    buckets = [
        settings.SUPABASE_BUCKET_STUDENT_PHOTOS,
        settings.SUPABASE_BUCKET_BONAFIDES,
        settings.SUPABASE_BUCKET_FIXTURES,
        settings.SUPABASE_BUCKET_EVENT_MEDIA,
        settings.SUPABASE_BUCKET_CERTIFICATE_TEMPLATES,
        settings.SUPABASE_BUCKET_CERTIFICATES,
    ]
    headers = {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        for bucket in buckets:
            response = await client.post(
                f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/bucket",
                headers=headers,
                json={"id": bucket, "name": bucket, "public": False},
            )
            if response.status_code in {200, 201}:
                print(f"Created private bucket: {bucket}")
            elif response.status_code == 409 or "already exists" in response.text.lower():
                print(f"Bucket already exists: {bucket}")
            else:
                print(f"Could not create {bucket}: {response.status_code} {response.text}")


if __name__ == "__main__":
    asyncio.run(main())
