from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import get_settings

settings = get_settings()

connect_args: dict = {}
if settings.is_sqlite:
    connect_args = {"check_same_thread": False}
elif "postgresql" in settings.database_url:
    # RDS often needs SSL; tolerate self-signed / verify-full mismatches for now
    connect_args = {"sslmode": "require"}

_url = (
    settings.database_url.split("?")[0]
    if "sslmode=" in settings.database_url
    else settings.database_url
)

_engine_kwargs: dict = {
    "connect_args": connect_args,
    "pool_pre_ping": True,
}
if not settings.is_sqlite:
    _engine_kwargs.update(
        {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_recycle": 1800,
        }
    )

engine = create_engine(_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
