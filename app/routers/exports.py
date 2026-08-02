"""Global export job listing (Downloads menu)."""

from fastapi import APIRouter

from app.auth import ExportUser
from app.services.pdf_jobs import list_user_jobs

router = APIRouter(prefix="/api/exports", tags=["exports"])


@router.get("")
def list_my_exports(user: ExportUser):
    jobs = list_user_jobs(str(user.id))
    return [j.to_dict() for j in jobs[:50]]
