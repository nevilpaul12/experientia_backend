from uuid import UUID, uuid4
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, File, Form, UploadFile, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import joinedload

from app.auth import CurrentUser, DbSession, user_is_manager, ManagerUser
from app.models import Task
from app.schemas import PresignRequest, PresignResponse, BrandPresignRequest
from app.services.storage import storage
from app.config import get_settings

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
settings = get_settings()

ALLOWED_VIEW_PREFIXES = ("gig-management/", "proofs/")


@router.get("/view")
def view_object(key: str = Query(..., min_length=3)):
    """Stream a private S3 object (used for proof / brand logo <img> tags)."""
    key = unquote(key)
    if ".." in key or key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid key")
    if not key.startswith(ALLOWED_VIEW_PREFIXES):
        raise HTTPException(status_code=400, detail="Invalid key prefix")
    data = storage.get_bytes(key, "")
    if not data:
        raise HTTPException(status_code=404, detail="Not found")
    content_type = "image/jpeg"
    lower = key.lower()
    if lower.endswith(".png"):
        content_type = "image/png"
    elif lower.endswith(".webp"):
        content_type = "image/webp"
    elif lower.endswith(".gif"):
        content_type = "image/gif"
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
            # Allow <img> loads from the CloudFront frontend origin.
            "Access-Control-Allow-Origin": "*",
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )


@router.post("/presign", response_model=PresignResponse)
def presign(payload: PresignRequest, db: DbSession, user: CurrentUser):
    task = (
        db.query(Task)
        .options(joinedload(Task.campaign))
        .filter(Task.id == payload.task_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not user_is_manager(db, user) and task.executorUserId != user.id:
        raise HTTPException(status_code=403, detail="Not allowed")

    key = storage.make_key(str(payload.task_id), payload.filename)
    upload_url, use_direct = storage.presign_put(key, payload.content_type)
    return PresignResponse(
        upload_url=upload_url,
        storage_key=key,
        public_url=storage.public_url(key),
        use_direct_put=use_direct,
    )


@router.post("/presign-brand", response_model=PresignResponse)
def presign_brand(payload: BrandPresignRequest, user: ManagerUser):
    """Presign an S3 PUT for a brand logo."""
    ct = (payload.content_type or "image/jpeg").lower()
    if not ct.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads allowed")
    key = storage.make_brand_key(
        str(payload.brand_id) if payload.brand_id else None,
        payload.filename,
    )
    upload_url, use_direct = storage.presign_put(key, payload.content_type or "image/jpeg")
    return PresignResponse(
        upload_url=upload_url,
        storage_key=key,
        public_url=storage.public_url(key),
        use_direct_put=use_direct,
    )


@router.post("/local")
async def upload_local(
    user: CurrentUser,
    file: UploadFile = File(...),
    storage_key: str = Form(...),
):
    if ".." in storage_key or storage_key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid key")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 25MB)")
    # Prefer S3 even for "local" endpoint when configured
    if settings.use_s3:
        storage.upload_bytes(storage_key, data, file.content_type or "image/jpeg")
        return {"storage_key": storage_key, "url": storage.public_url(storage_key)}
    url = storage.save_local(storage_key, data)
    return {"storage_key": storage_key, "url": url}


@router.get("/file/{file_path:path}")
def get_file(file_path: str):
    path = storage.resolve_local_path(file_path)
    if not path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)
