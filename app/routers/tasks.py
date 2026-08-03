from datetime import datetime
from uuid import UUID
import io
import copy

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified

from app.auth import CurrentUser, ManagerUser, DbSession, user_is_manager, user_is_supervisor, can_view_campaign
from app.models import Task, Campaign, User, TaskStatus
from app.schemas import TaskOut, TaskDetailsSubmit, ProofImageCreate, ProofImageOut, ExecutorCampaignSummary, ExecutorTasksPage
from app.serializers import task_out, resolve_sequence_number
from app.media_rules import normalize_service_type
from app.services.pdf import build_task_pdf
from app.services.geo import within_radius, haversine_km
from app.media_rules import slots_for, detail_form_for, slot_label, DEFAULT_RADIUS_KM, photos_for
from app.services.storage import storage

router = APIRouter(prefix="/api", tags=["tasks"])


def _load_task(db, task_id: UUID) -> Task | None:
    return (
        db.query(Task)
        .options(
            joinedload(Task.executor),
            joinedload(Task.campaign).joinedload(Campaign.brand),
        )
        .filter(Task.id == task_id)
        .first()
    )


def _sequence_for_task(db, task: Task) -> int:
    """Resolve Task #N — metadata first, else 1-based order in the campaign."""
    seq = resolve_sequence_number(task)
    if seq > 0:
        return seq
    siblings = (
        db.query(Task.id)
        .filter(Task.campaignId == task.campaignId)
        .order_by(Task.createdAt.asc(), Task.id.asc())
        .all()
    )
    for i, (tid,) in enumerate(siblings, start=1):
        if tid == task.id:
            return i
    return 0


def _backfill_image_gps(task: Task) -> bool:
    """Copy task location onto proof images that were saved without GPS."""
    meta = _meta(task)
    loc = meta.get("location") or {}
    target = meta.get("target") or {}
    lat = loc.get("latitude") if loc.get("latitude") is not None else target.get("latitude")
    lng = loc.get("longitude") if loc.get("longitude") is not None else target.get("longitude")
    if lat is None and task.campaign:
        lat = task.campaign.latitude
    if lng is None and task.campaign:
        lng = task.campaign.longitude
    if lat is None or lng is None:
        return False

    images = meta.get("images") or []
    changed = False
    new_images = []
    for img in images:
        if isinstance(img, str):
            new_images.append(
                {
                    "url": img,
                    "slot": "photo_1",
                    "latitude": lat,
                    "longitude": lng,
                }
            )
            changed = True
            continue
        if not isinstance(img, dict):
            new_images.append(img)
            continue
        entry = dict(img)
        if entry.get("latitude") is None:
            entry["latitude"] = lat
            changed = True
        if entry.get("longitude") is None:
            entry["longitude"] = lng
            changed = True
        new_images.append(entry)
    if changed:
        meta["images"] = new_images
        if not meta.get("sequenceNumber"):
            # leave sequence to caller; GPS-only backfill here
            pass
        _set_meta(task, meta)
    return changed


def _can_access(db, user: User, task: Task) -> bool:
    if task.campaign and task.campaign.organizationId != user.organizationId:
        return False
    if user_is_manager(db, user):
        return True
    if task.campaign and can_view_campaign(db, user, task.campaign):
        return True
    return task.executorUserId == user.id


def _can_edit_task(db, user: User, task: Task) -> bool:
    """Supervisors are view-only; managers + assigned executors can edit."""
    if user_is_supervisor(db, user):
        return False
    if user_is_manager(db, user):
        return True
    return task.executorUserId == user.id


def _meta(task: Task) -> dict:
    return copy.deepcopy(task.metadata_) if task.metadata_ else {}


def _set_meta(task: Task, meta: dict) -> None:
    task.metadata_ = meta
    flag_modified(task, "metadata_")
    task.updatedAt = datetime.utcnow()


def _photos_done(meta: dict, service_type: str | None) -> bool:
    slots = slots_for(service_type)
    have = {img.get("slot") for img in (meta.get("images") or []) if isinstance(img, dict)}
    # legacy images without slot count toward required total
    legacy = [img for img in (meta.get("images") or []) if isinstance(img, str) or (isinstance(img, dict) and not img.get("slot"))]
    if all(s in have for s in slots):
        return True
    return len(meta.get("images") or []) >= photos_for(service_type) and not slots


def _details_done(meta: dict, form: str | None) -> bool:
    if form == "driver":
        d = meta.get("driver") or {}
        return bool(d.get("name") and d.get("phone") and d.get("vehicleNumber"))
    if form == "gym":
        g = meta.get("gym") or {}
        return bool(g.get("name") and g.get("location"))
    return True


def _maybe_complete(task: Task) -> None:
    service = task.campaign.serviceType if task.campaign else None
    form = detail_form_for(service)
    meta = _meta(task)
    if _photos_done(meta, service) and _details_done(meta, form):
        task.status = TaskStatus.ACCEPTED
        task.completedAt = datetime.utcnow()
    elif meta.get("images"):
        if not task.startedAt:
            task.startedAt = datetime.utcnow()


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: UUID, db: DbSession, user: CurrentUser):
    task = _load_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _can_access(db, user, task):
        raise HTTPException(status_code=403, detail="Not allowed")

    seq = _sequence_for_task(db, task)
    meta = _meta(task)
    dirty = False
    if seq > 0 and not meta.get("sequenceNumber"):
        meta["sequenceNumber"] = seq
        _set_meta(task, meta)
        dirty = True
    if _backfill_image_gps(task):
        dirty = True
    if dirty:
        db.commit()
        task = _load_task(db, task_id)

    return task_out(task, sequence_number=seq, image_limit=None)


@router.post("/tasks/{task_id}/details", response_model=TaskOut)
def submit_task_details(
    task_id: UUID, payload: TaskDetailsSubmit, db: DbSession, user: CurrentUser
):
    task = _load_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _can_edit_task(db, user, task):
        raise HTTPException(status_code=403, detail="View-only access — edits not allowed")

    form = detail_form_for(task.campaign.serviceType)
    meta = _meta(task)
    if form == "driver":
        if not (payload.driver_name and payload.driver_phone and payload.vehicle_number):
            raise HTTPException(status_code=400, detail="Driver name, phone, and vehicle number are required")
        meta["driver"] = {
            "name": payload.driver_name.strip(),
            "phone": payload.driver_phone.strip(),
            "vehicleNumber": payload.vehicle_number.strip(),
        }
    elif form == "gym":
        if not (payload.gym_name and payload.gym_location):
            raise HTTPException(status_code=400, detail="Gym name and location are required")
        meta["gym"] = {
            "name": payload.gym_name.strip(),
            "location": payload.gym_location.strip(),
        }
    else:
        raise HTTPException(status_code=400, detail="This service type has no details form")

    _set_meta(task, meta)
    _maybe_complete(task)
    db.commit()
    refreshed = _load_task(db, task_id)
    return task_out(refreshed, sequence_number=_sequence_for_task(db, refreshed), image_limit=None)


@router.post("/tasks/{task_id}/proofs", response_model=ProofImageOut)
def add_proof(task_id: UUID, payload: ProofImageCreate, db: DbSession, user: CurrentUser):
    task = _load_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _can_edit_task(db, user, task):
        raise HTTPException(status_code=403, detail="View-only access — edits not allowed")

    campaign = task.campaign
    service = campaign.serviceType
    slots = slots_for(service)
    if payload.slot not in slots:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid slot '{payload.slot}'. Expected: {', '.join(slots)}",
        )

    is_manager = user_is_manager(db, user)
    meta = _meta(task)
    images = [img for img in (meta.get("images") or []) if isinstance(img, dict)]
    have = {img.get("slot") for img in images}
    idx = slots.index(payload.slot)

    # Executors must capture in order; managers may fill/replace any slot.
    if not is_manager:
        for prior in slots[:idx]:
            if prior not in have:
                raise HTTPException(
                    status_code=400,
                    detail=f"Capture {slot_label(prior)} before {slot_label(payload.slot)}",
                )
        if payload.slot in have:
            raise HTTPException(
                status_code=400, detail=f"{slot_label(payload.slot)} already uploaded"
            )
    elif payload.slot in have:
        images = [img for img in images if img.get("slot") != payload.slot]

    # Managers may upload from the office — skip geofence enforcement.
    if (
        not is_manager
        and campaign.latitude is not None
        and campaign.longitude is not None
    ):
        if not within_radius(
            campaign.latitude,
            campaign.longitude,
            payload.latitude,
            payload.longitude,
            DEFAULT_RADIUS_KM,
        ):
            dist = haversine_km(
                campaign.latitude, campaign.longitude, payload.latitude, payload.longitude
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Photo is {dist:.2f} km from campaign center — "
                    f"must be within {DEFAULT_RADIUS_KM} km"
                ),
            )

    captured = (payload.captured_at or datetime.utcnow()).isoformat()
    entry = {
        "url": payload.url,
        "key": payload.storage_key,
        "slot": payload.slot,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "capturedAt": captured,
    }
    images.append(entry)
    meta["images"] = images
    meta["location"] = {
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "accuracy": meta.get("location", {}).get("accuracy"),
    }
    _set_meta(task, meta)
    if not task.startedAt:
        task.startedAt = datetime.utcnow()
    _maybe_complete(task)
    db.commit()
    return ProofImageOut(
        url=storage.viewable_url(entry["url"], entry.get("key")),
        slot=entry["slot"],
        latitude=entry["latitude"],
        longitude=entry["longitude"],
        captured_at=captured,
    )


@router.delete("/tasks/{task_id}/proofs/{slot}")
def delete_proof(task_id: UUID, slot: str, db: DbSession, user: CurrentUser):
    task = _load_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _can_edit_task(db, user, task):
        raise HTTPException(status_code=403, detail="View-only access — edits not allowed")

    meta = _meta(task)
    images = [img for img in (meta.get("images") or []) if isinstance(img, dict)]
    before = len(images)
    images = [img for img in images if img.get("slot") != slot]
    if len(images) == before:
        raise HTTPException(status_code=404, detail="Proof not found")

    meta["images"] = images
    _set_meta(task, meta)
    service = task.campaign.serviceType if task.campaign else None
    if not _photos_done(meta, service) or not _details_done(meta, detail_form_for(service)):
        task.status = TaskStatus.PENDING
        task.completedAt = None
    db.commit()
    return {"ok": True}


@router.get("/tasks/{task_id}/export.pdf")
def export_task_pdf(task_id: UUID, db: DbSession, user: CurrentUser):
    task = _load_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not user_is_manager(db, user):
        raise HTTPException(status_code=403, detail="Manager access required")
    pdf_bytes = build_task_pdf(task, task.campaign)
    filename = f"experientia-task-{task_id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/my/campaigns", response_model=list[ExecutorCampaignSummary])
def my_campaigns(db: DbSession, user: CurrentUser):
    rows = (
        db.query(
            Campaign,
            func.count(Task.id).label("task_count"),
            func.sum(case((Task.status != TaskStatus.ACCEPTED, 1), else_=0)).label(
                "pending_count"
            ),
        )
        .join(Task, Task.campaignId == Campaign.id)
        .filter(Task.executorUserId == user.id, Campaign.isActive.is_(True))
        .group_by(Campaign.id)
        .order_by(Campaign.createdAt.desc())
        .all()
    )
    out: list[ExecutorCampaignSummary] = []
    for campaign, task_count, pending_count in rows:
        location_label = None
        if campaign.address:
            parts = campaign.address.split(",")
            location_label = parts[-1].strip() if parts else campaign.address
        elif campaign.name and " - " in campaign.name:
            location_label = campaign.name.split(" - ", 1)[1].strip()
        out.append(
            ExecutorCampaignSummary(
                id=campaign.id,
                name=campaign.name,
                service_type=normalize_service_type(campaign.serviceType),
                status=campaign.status,
                address=campaign.address,
                location_label=location_label,
                start_date=campaign.startDate,
                end_date=campaign.endDate,
                task_count=int(task_count or 0),
                pending_count=int(pending_count or 0),
            )
        )
    return out


def _executor_task_open(meta: dict, service_type: str | None) -> bool:
    form = detail_form_for(service_type)
    if not _photos_done(meta, service_type):
        return True
    if form and not _details_done(meta, form):
        return True
    return False


def _find_next_open_task_id(tasks: list[Task]) -> UUID | None:
    for task in tasks:
        service = task.campaign.serviceType if task.campaign else None
        if _executor_task_open(_meta(task), service):
            return task.id
    return None


@router.get("/my/tasks", response_model=ExecutorTasksPage)
def my_tasks(
    db: DbSession,
    user: CurrentUser,
    campaign_id: UUID | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(15, ge=1, le=100),
):
    base_q = (
        db.query(Task)
        .options(joinedload(Task.executor), joinedload(Task.campaign))
        .filter(Task.executorUserId == user.id)
    )
    if campaign_id:
        base_q = base_q.filter(Task.campaignId == campaign_id)

    total = base_q.count()
    tasks = (
        base_q.order_by(Task.createdAt.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    all_for_next = (
        base_q.order_by(Task.createdAt.asc()).all()
        if campaign_id
        else []
    )

    return ExecutorTasksPage(
        items=[
            task_out(t, sequence_number=_sequence_for_task(db, t))
            for t in tasks
        ],
        page=page,
        page_size=limit,
        total=total,
        next_open_task_id=_find_next_open_task_id(all_for_next),
    )
