import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, create_tables
from app.core.security import hash_password
from app.models.entities import Admin


async def main():
    await create_tables()
    async with AsyncSessionLocal() as db:
        email = str(settings.INITIAL_ADMIN_EMAIL).lower()
        admin = await db.scalar(select(Admin).where(Admin.email == email))
        if admin:
            print(f"Admin already exists: {email}")
            return
        admin = Admin(
            name=settings.INITIAL_ADMIN_NAME,
            email=email,
            password_hash=hash_password(settings.INITIAL_ADMIN_PASSWORD),
            role="SUPER_ADMIN",
        )
        db.add(admin)
        await db.commit()
        print(f"Created super admin: {email}")
        print("Change INITIAL_ADMIN_PASSWORD after the first login.")


if __name__ == "__main__":
    asyncio.run(main())
