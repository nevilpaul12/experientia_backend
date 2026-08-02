from datetime import datetime
from uuid import uuid4, UUID
import io
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy import func, case
from sqlalchemy.orm import joinedload

from app.auth import (
    CurrentUser,
    ManagerUser,
    ExportUser,
    DbSession,
    user_is_manager,
    user_is_supervisor,
    supervisor_brand_ids,
    can_view_campaign,
    resolve_user_role,
)
from app.models import (
    Campaign,
    Task,
    Brand,
    BrandMember,
    User,
    CampaignMember,
    CampaignRole,
    TaskStatus,
)
from app.schemas import (
    CampaignCreate,
    CampaignUpdate,
    CampaignOut,
    CampaignListItem,
    CampaignAssignExecutors,
    CampaignAssignSupervisors,
)
from app.serializers import campaign_list_item_from_counts, campaign_out
from app.media_rules import normalize_service_type, DEFAULT_RADIUS_KM
from app.services.geo import random_points_in_radius
from app.services.pdf import build_campaign_pdf
from app.services.pdf_jobs import start_export_job, get_job, list_campaign_jobs

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _load_campaign_base(db, campaign_id: UUID) -> Campaign | None:
    return (
        db.query(Campaign)
        .options(
            joinedload(Campaign.brand),
            joinedload(Campaign.members).joinedload(CampaignMember.user),
        )
        .filter(Campaign.id == campaign_id)
        .first()
    )


def _task_counts(db, campaign_ids: list[UUID]) -> dict[UUID, tuple[int, int, int]]:
    if not campaign_ids:
        return {}
    rows = (
        db.query(
            Task.campaignId,
            func.count(Task.id),
            func.sum(case((Task.status == TaskStatus.ACCEPTED, 1), else_=0)),
        )
        .filter(Task.campaignId.in_(campaign_ids))
        .group_by(Task.campaignId)
        .all()
    )
    out: dict[UUID, tuple[int, int, int]] = {}
    for cid, total, completed in rows:
        total_i = int(total or 0)
        done_i = int(completed or 0)
        out[cid] = (total_i, done_i, max(total_i - done_i, 0))
    return out


def _can_see_membership(db, user: User, campaign_id: UUID) -> bool:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return False
    return can_view_campaign(db, user, campaign)


def _attach_brand_supervisors(db, campaign: Campaign, assigned_by: UUID) -> None:
    if not campaign.brandId:
        return
    links = (
        db.query(BrandMember)
        .filter(BrandMember.brandId == campaign.brandId, BrandMember.active.is_(True))
        .all()
    )
    for link in links:
        existing = (
            db.query(CampaignMember)
            .filter(
                CampaignMember.campaignId == campaign.id,
                CampaignMember.userId == link.userId,
                CampaignMember.role == CampaignRole.SUPERVISOR,
            )
            .first()
        )
        if existing:
            existing.active = True
        else:
            db.add(
                CampaignMember(
                    id=uuid4(),
                    campaignId=campaign.id,
                    userId=link.userId,
                    assignedBy=assigned_by,
                    role=CampaignRole.SUPERVISOR,
                    active=True,
                )
            )


def _sync_campaign_members(
    db,
    campaign: Campaign,
    role: CampaignRole,
    user_ids: list[UUID],
    assigned_by: UUID,
    *,
    location: str | None = None,
) -> None:
    wanted = set(user_ids)
    existing = {
        m.userId: m
        for m in db.query(CampaignMember)
        .filter(CampaignMember.campaignId == campaign.id, CampaignMember.role == role)
        .all()
    }
    for uid, member in existing.items():
        member.active = uid in wanted
    for uid in wanted:
        if uid in existing:
            if location:
                existing[uid].location = location
            continue
        db.add(
            CampaignMember(
                id=uuid4(),
                campaignId=campaign.id,
                userId=uid,
                assignedBy=assigned_by,
                role=role,
                location=location,
                active=True,
            )
        )


@router.get("", response_model=list[CampaignListItem])
def list_campaigns(db: DbSession, user: CurrentUser):
    # Fast path: campaigns + brand only (no task rows)
    q = (
        db.query(Campaign)
        .options(joinedload(Campaign.brand))
        .filter(Campaign.organizationId == user.organizationId, Campaign.isActive.is_(True))
        .order_by(Campaign.createdAt.desc())
    )
    campaigns = q.all()

    if not user_is_manager(db, user):
        allowed = set()
        member_ids = {
            r[0]
            for r in db.query(CampaignMember.campaignId)
            .filter(CampaignMember.userId == user.id, CampaignMember.active.is_(True))
            .all()
        }
        task_ids = {
            r[0]
            for r in db.query(Task.campaignId).filter(Task.executorUserId == user.id).distinct().all()
        }
        brand_ids = supervisor_brand_ids(db, user)
        brand_campaign_ids = set()
        if brand_ids:
            brand_campaign_ids = {
                r[0]
                for r in db.query(Campaign.id)
                .filter(Campaign.brandId.in_(brand_ids), Campaign.isActive.is_(True))
                .all()
            }
        allowed = member_ids | task_ids | brand_campaign_ids
        campaigns = [c for c in campaigns if c.id in allowed]

    counts = _task_counts(db, [c.id for c in campaigns])
    result = []
    for c in campaigns:
        total, done, pending = counts.get(c.id, (0, 0, c.totalTasks or 0))
        if total == 0 and c.totalTasks:
            pending = c.totalTasks
        result.append(campaign_list_item_from_counts(c, total, done, pending))
    return result


@router.post("", response_model=CampaignOut)
def create_campaign(payload: CampaignCreate, db: DbSession, user: ManagerUser):
    service = normalize_service_type(payload.service_type)
    if payload.start_date and payload.end_date and payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if payload.brand_id:
        brand = (
            db.query(Brand)
            .filter(Brand.id == payload.brand_id, Brand.organizationId == user.organizationId)
            .first()
        )
        if not brand:
            raise HTTPException(status_code=404, detail="Brand not found")
    else:
        brand = None

    campaign = Campaign(
        id=uuid4(),
        organizationId=user.organizationId,
        name=payload.name,
        description=payload.description or "",
        status="ACTIVE",
        latitude=payload.center_latitude,
        longitude=payload.center_longitude,
        address=payload.address,
        serviceType=service,
        isActive=True,
        totalTasks=payload.total_tasks,
        brandId=payload.brand_id,
        logo=brand.image if brand and brand.image else None,
        startDate=payload.start_date,
        endDate=payload.end_date,
    )
    db.add(campaign)
    db.flush()

    db.add(
        CampaignMember(
            id=uuid4(),
            campaignId=campaign.id,
            userId=user.id,
            assignedBy=user.id,
            role=CampaignRole.CAMPAIGN_MANAGER,
            active=True,
        )
    )

    executors: list[User] = []
    if payload.executor_ids:
        executors = (
            db.query(User)
            .filter(
                User.id.in_(payload.executor_ids),
                User.organizationId == user.organizationId,
            )
            .all()
        )
        for ex in executors:
            db.add(
                CampaignMember(
                    id=uuid4(),
                    campaignId=campaign.id,
                    userId=ex.id,
                    assignedBy=user.id,
                    role=CampaignRole.EXECUTOR,
                    active=True,
                )
            )

    if executors:
        _generate_tasks(db, campaign, executors)

    _attach_brand_supervisors(db, campaign, user.id)

    db.commit()
    return _get_campaign_detail(campaign.id, db, user)


def _generate_tasks(db, campaign: Campaign, executors: list[User]) -> None:
    if not executors:
        return
    existing = db.query(Task).filter(Task.campaignId == campaign.id).count()
    if existing > 0:
        return
    if campaign.latitude is None or campaign.longitude is None:
        raise HTTPException(status_code=400, detail="Campaign needs center coordinates")

    points = random_points_in_radius(
        campaign.latitude,
        campaign.longitude,
        DEFAULT_RADIUS_KM,
        campaign.totalTasks,
        seed=int(campaign.id.int % (2**31)),
    )
    now = datetime.utcnow()
    for i, (lat, lng) in enumerate(points, start=1):
        ex = executors[(i - 1) % len(executors)]
        db.add(
            Task(
                id=uuid4(),
                campaignId=campaign.id,
                executorUserId=ex.id,
                status=TaskStatus.PENDING,
                assignedAt=now,
                metadata_={
                    "sequenceNumber": i,
                    "target": {"latitude": lat, "longitude": lng},
                    "images": [],
                },
                flagged=False,
            )
        )


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(
    campaign_id: UUID,
    db: DbSession,
    user: CurrentUser,
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=10000),
    status: str | None = None,
    q: str | None = None,
    executor_id: UUID | None = None,
):
    return _get_campaign_detail(
        campaign_id,
        db,
        user,
        page=page,
        limit=limit,
        status=status,
        q=q,
        executor_id=executor_id,
    )


def _get_campaign_detail(
    campaign_id: UUID,
    db: DbSession,
    user: CurrentUser,
    *,
    page: int = 1,
    limit: int = 24,
    status: str | None = None,
    q: str | None = None,
    executor_id: UUID | None = None,
):
    campaign = _load_campaign_base(db, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.organizationId != user.organizationId or not _can_see_membership(
        db, user, campaign_id
    ):
        raise HTTPException(status_code=403, detail="Not allowed")

    counts = _task_counts(db, [campaign_id]).get(campaign_id, (0, 0, 0))
    total, done, pending = counts

    tasks_q = (
        db.query(Task)
        .options(joinedload(Task.executor))
        .filter(Task.campaignId == campaign_id)
    )
    if not user_is_manager(db, user):
        # Supervisors see all tasks (view-only); executors see only theirs
        if not user_is_supervisor(db, user):
            tasks_q = tasks_q.filter(Task.executorUserId == user.id)
    if status:
        try:
            tasks_q = tasks_q.filter(Task.status == TaskStatus(status.upper()))
        except ValueError:
            tasks_q = tasks_q.filter(Task.status == status.upper())
    if executor_id:
        tasks_q = tasks_q.filter(Task.executorUserId == executor_id)

    start = (page - 1) * limit
    ordered = tasks_q.order_by(Task.createdAt.asc())

    # Task ID / sequence search needs metadata JSON — filter in memory only when needed.
    if q and q.strip():
        ql = q.strip().lower()
        all_matching = ordered.all()
        filtered = []
        for t in all_matching:
            seq = str((t.metadata_ or {}).get("sequenceNumber") or "")
            if ql in str(t.id).lower() or ql in seq:
                filtered.append(t)
        tasks_total = len(filtered)
        page_tasks = filtered[start : start + limit]
    else:
        tasks_total = ordered.count()
        page_tasks = ordered.offset(start).limit(limit).all()

    for t in page_tasks:
        t.campaign = campaign

    return campaign_out(
        campaign,
        tasks=page_tasks,
        task_count=total if (user_is_manager(db, user) or user_is_supervisor(db, user)) else tasks_total,
        completed=done,
        pending=pending,
        page=page,
        page_size=limit,
        tasks_total=tasks_total,
        image_limit=1,
    )


@router.patch("/{campaign_id}", response_model=CampaignOut)
def update_campaign(
    campaign_id: UUID, payload: CampaignUpdate, db: DbSession, user: ManagerUser
):
    campaign = _load_campaign_base(db, campaign_id)
    if not campaign or campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Campaign not found")
    data = payload.model_dump(exclude_unset=True)
    brand_id = data.pop("brand_id", None)
    if brand_id is not None:
        if brand_id:
            brand = (
                db.query(Brand)
                .filter(Brand.id == brand_id, Brand.organizationId == user.organizationId)
                .first()
            )
            if not brand:
                raise HTTPException(status_code=404, detail="Brand not found")
            campaign.brandId = brand.id
            campaign.logo = brand.image
        else:
            campaign.brandId = None
    for field, value in data.items():
        setattr(campaign, field, value)
    campaign.updatedAt = datetime.utcnow()
    db.flush()
    if brand_id is not None and campaign.brandId:
        _attach_brand_supervisors(db, campaign, user.id)
    db.commit()
    return _get_campaign_detail(campaign_id, db, user)


@router.post("/{campaign_id}/executors", response_model=CampaignOut)
def assign_campaign_executors(
    campaign_id: UUID, payload: CampaignAssignExecutors, db: DbSession, user: ManagerUser
):
    campaign = _load_campaign_base(db, campaign_id)
    if not campaign or campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Campaign not found")

    executors = (
        db.query(User)
        .filter(
            User.id.in_(payload.executor_ids),
            User.organizationId == user.organizationId,
        )
        .all()
    )
    if not executors:
        raise HTTPException(status_code=400, detail="Select at least one executor")

    _sync_campaign_members(
        db, campaign, CampaignRole.EXECUTOR, payload.executor_ids, user.id
    )

    task_count = db.query(Task).filter(Task.campaignId == campaign.id).count()
    if task_count == 0:
        _generate_tasks(db, campaign, executors)
    else:
        tasks = (
            db.query(Task)
            .filter(Task.campaignId == campaign.id)
            .order_by(Task.createdAt)
            .all()
        )
        for i, t in enumerate(tasks):
            t.executorUserId = executors[i % len(executors)].id

    campaign.updatedAt = datetime.utcnow()
    db.commit()
    return _get_campaign_detail(campaign_id, db, user)


@router.post("/{campaign_id}/supervisors", response_model=CampaignOut)
def assign_campaign_supervisors(
    campaign_id: UUID, payload: CampaignAssignSupervisors, db: DbSession, user: ManagerUser
):
    campaign = _load_campaign_base(db, campaign_id)
    if not campaign or campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not campaign.brandId:
        raise HTTPException(
            status_code=400,
            detail="Assign a brand to this campaign before adding supervisors",
        )

    brand = (
        db.query(Brand)
        .filter(Brand.id == campaign.brandId, Brand.organizationId == user.organizationId)
        .first()
    )
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    if payload.supervisor_ids:
        allowed = {
            row.userId
            for row in db.query(BrandMember)
            .filter(
                BrandMember.brandId == campaign.brandId,
                BrandMember.active.is_(True),
            )
            .all()
        }
        invalid = set(payload.supervisor_ids) - allowed
        if invalid:
            raise HTTPException(
                status_code=400,
                detail="Supervisors must be registered for this brand (add them on Team page)",
            )

    _sync_campaign_members(
        db,
        campaign,
        CampaignRole.SUPERVISOR,
        payload.supervisor_ids,
        user.id,
        location=brand.name,
    )
    campaign.updatedAt = datetime.utcnow()
    db.commit()
    return _get_campaign_detail(campaign_id, db, user)


def _require_exportable_campaign(db, user: User, campaign_id: UUID) -> Campaign:
    campaign = _load_campaign_base(db, campaign_id)
    if not campaign or campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not can_view_campaign(db, user, campaign):
        raise HTTPException(status_code=403, detail="Not allowed")
    if not (user_is_manager(db, user) or user_is_supervisor(db, user)):
        raise HTTPException(status_code=403, detail="Export access required")
    return campaign


@router.post("/{campaign_id}/export")
def start_campaign_export(campaign_id: UUID, db: DbSession, user: ExportUser):
    """Queue a background PDF/ZIP export — returns immediately with job id."""
    campaign = _require_exportable_campaign(db, user, campaign_id)
    total = db.query(Task).filter(Task.campaignId == campaign_id).count()
    if total == 0:
        raise HTTPException(status_code=400, detail="No tasks to export")
    job = start_export_job(campaign_id, user.id, campaign.name)
    return {
        **job.to_dict(),
        "message": (
            "Export started in the background. You can keep using the app — "
            "download when status is ready. Large campaigns export as a ZIP of PDFs."
        ),
    }


@router.get("/{campaign_id}/export/jobs")
def list_export_jobs(campaign_id: UUID, db: DbSession, user: ExportUser):
    _require_exportable_campaign(db, user, campaign_id)
    jobs = sorted(
        list_campaign_jobs(str(campaign_id), str(user.id)),
        key=lambda j: j.created_at,
        reverse=True,
    )
    return [j.to_dict() for j in jobs[:10]]


@router.get("/{campaign_id}/export/jobs/{job_id}")
def get_export_job(campaign_id: UUID, job_id: str, db: DbSession, user: ExportUser):
    _require_exportable_campaign(db, user, campaign_id)
    job = get_job(job_id)
    if not job or job.campaign_id != str(campaign_id) or job.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Export job not found")
    return job.to_dict()


@router.get("/{campaign_id}/export/jobs/{job_id}/download")
def download_export_job(campaign_id: UUID, job_id: str, db: DbSession, user: ExportUser):
    _require_exportable_campaign(db, user, campaign_id)
    job = get_job(job_id)
    if not job or job.campaign_id != str(campaign_id) or job.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Export job not found")
    if job.status.value != "ready" or not job.file_path:
        raise HTTPException(status_code=409, detail="Export not ready yet")
    path = Path(job.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="Export file expired")
    return FileResponse(
        path,
        media_type=job.content_type,
        filename=job.filename or path.name,
    )


@router.get("/{campaign_id}/export.pdf")
def export_campaign_pdf(campaign_id: UUID, db: DbSession, user: ExportUser):
    """Legacy sync export — only sensible for small campaigns (<100 tasks)."""
    campaign = _require_exportable_campaign(db, user, campaign_id)
    count = db.query(Task).filter(Task.campaignId == campaign_id).count()
    if count >= 100:
        raise HTTPException(
            status_code=400,
            detail="Campaign too large for instant download. Use POST /export (background job).",
        )
    tasks = (
        db.query(Task)
        .options(joinedload(Task.executor))
        .filter(Task.campaignId == campaign_id)
        .order_by(Task.createdAt.asc())
        .all()
    )
    for t in tasks:
        t.campaign = campaign
    pdf_bytes = build_campaign_pdf(campaign, tasks)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in campaign.name)[:40]
    filename = f"experientia-{safe_name or campaign_id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
