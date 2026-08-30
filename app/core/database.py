from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine_options = {
    "pool_pre_ping": True,
}


if settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
    engine_options.update(
        pool_size=5,
        max_overflow=10,
    )

    if settings.DB_REQUIRE_SSL:
        engine_options["connect_args"] = {
            "ssl": "require",
        }


engine = create_async_engine(
    settings.DATABASE_URL,
    **engine_options,
)


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_tables():
    from app.models import entities  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
