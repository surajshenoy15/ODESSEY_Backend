import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal, create_tables
from app.models.entities import EventConfig

CATEGORIES = ["PU Boys", "PU Girls", "Engineering Boys", "Engineering Girls"]
SPORTS = {
    "Volleyball": (6, 12, 4),
    "Throwball": (7, 12, 3),
    "Table Tennis": (1, 5, 1),
    "Yoga": (1, 10, 0),
}


async def main():
    await create_tables()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        created = 0
        for sport, (minimum, maximum, substitutes) in SPORTS.items():
            for category in CATEGORIES:
                existing = await db.scalar(
                    select(EventConfig).where(
                        EventConfig.sport_name == sport,
                        EventConfig.category == category,
                    )
                )
                if existing:
                    continue
                db.add(
                    EventConfig(
                        sport_name=sport,
                        category=category,
                        fee_paise=100000,
                        team_min_size=minimum,
                        team_max_size=maximum,
                        max_substitutes=substitutes,
                        registration_opens_at=now - timedelta(days=1),
                        registration_closes_at=now + timedelta(days=30),
                        event_date=now + timedelta(days=45),
                        venue="BNM Institute of Technology",
                        reporting_instructions="Report at the registration desk with the approved QR and original college ID cards.",
                    )
                )
                created += 1
        await db.commit()
        print(f"Created {created} event/category configurations.")
        print("Review team sizes, fees, dates and venue in the admin portal before launch.")


if __name__ == "__main__":
    asyncio.run(main())
