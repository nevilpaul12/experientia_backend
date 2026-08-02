from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import get_settings
from app.database import engine
from app.routers import auth, users_brands, campaigns, tasks, uploads, exports
from app.services.storage import storage

settings = get_settings()

app = FastAPI(title=settings.app_name, version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

upload_path = Path(settings.upload_dir)
upload_path.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")

app.include_router(auth.router)
app.include_router(users_brands.router)
app.include_router(campaigns.router)
app.include_router(tasks.router)
app.include_router(uploads.router)
app.include_router(exports.router)


def _ensure_brand_member_table() -> None:
    """Brand supervisors table — not in original Prisma schema."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS "BrandMember" (
            id UUID PRIMARY KEY,
            "brandId" UUID NOT NULL REFERENCES "Brand"(id) ON DELETE CASCADE,
            "userId" UUID NOT NULL REFERENCES "User"(id) ON DELETE CASCADE,
            "assignedBy" UUID NOT NULL,
            role TEXT NOT NULL DEFAULT 'SUPERVISOR',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        'CREATE INDEX IF NOT EXISTS "BrandMember_brandId_idx" ON "BrandMember" ("brandId")',
        'CREATE INDEX IF NOT EXISTS "BrandMember_userId_idx" ON "BrandMember" ("userId")',
    ]
    try:
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    except Exception as exc:  # noqa: BLE001
        print(f"[experientia] BrandMember table ensure skipped: {exc}")


_ensure_brand_member_table()

if settings.use_s3:
    storage.ensure_bucket_cors()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "schema": "prisma",
        "database": "sqlite" if settings.is_sqlite else "postgres",
        "s3": settings.use_s3,
        "bucket": settings.s3_bucket if settings.use_s3 else None,
    }
