"""Serialize Prisma models → API DTOs."""

from __future__ import annotations

from app.models import Campaign, Task, User, CampaignMember, CampaignRole, TaskStatus, Brand
from app.schemas import (
    UserOut,
    BrandOut,
    TaskOut,
    ProofImageOut,
    CampaignListItem,
    CampaignOut,
    MemberOut,
)
from app.media_rules import (
    photos_for,
    slots_for,
    detail_form_for,
    normalize_service_type,
    DEFAULT_RADIUS_KM,
)
from app.services.storage import storage
from app.services.geo import within_radius


def brand_out(b: Brand | None) -> BrandOut | None:
    if not b:
        return None
    return BrandOut(
        id=b.id,
        name=b.name,
        description=b.description,
        image=storage.viewable_url(b.image) if b.image else None,
    )


def user_out(u: User, role: str = "executor") -> UserOut:
    return UserOut(
        id=u.id,
        first_name=u.firstName,
        last_name=u.lastName,
        phone=u.phone,
        organization_id=u.organizationId,
        role=role,
        full_name=f"{u.firstName} {u.lastName}".strip(),
    )


def member_out(m: CampaignMember, assigner_name: str | None = None) -> MemberOut:
    u = m.user
    return MemberOut(
        id=m.id,
        user_id=m.userId,
        full_name=(f"{u.firstName} {u.lastName}".strip() if u else "—"),
        phone=u.phone if u else "",
        role=m.role.value if hasattr(m.role, "value") else str(m.role),
        location=m.location,
        assigned_by=assigner_name,
        active=m.active,
    )


def _meta(task: Task) -> dict:
    return task.metadata_ or {}


def _image_count(meta: dict) -> int:
    n = 0
    for img in meta.get("images") or []:
        if isinstance(img, str):
            n += 1
        elif isinstance(img, dict) and img.get("url"):
            n += 1
    return n


def _images(
    meta: dict,
    limit: int | None = None,
    *,
    fallback_lat: float | None = None,
    fallback_lng: float | None = None,
) -> list[ProofImageOut]:
    out = []
    for img in meta.get("images") or []:
        if isinstance(img, str):
            out.append(
                ProofImageOut(
                    url=storage.viewable_url(img),
                    slot="photo_1",
                    latitude=fallback_lat,
                    longitude=fallback_lng,
                )
            )
        elif isinstance(img, dict) and img.get("url"):
            lat = img.get("latitude")
            lng = img.get("longitude")
            if lat is None:
                lat = fallback_lat
            if lng is None:
                lng = fallback_lng
            out.append(
                ProofImageOut(
                    url=storage.viewable_url(img.get("url"), img.get("key")),
                    slot=img.get("slot") or "photo_1",
                    latitude=lat,
                    longitude=lng,
                    captured_at=img.get("capturedAt") or img.get("captured_at"),
                )
            )
        if limit is not None and len(out) >= limit:
            break
    return out


def _details_complete(meta: dict, form: str | None) -> bool:
    if form == "driver":
        d = meta.get("driver") or {}
        return bool(d.get("name") and d.get("phone") and d.get("vehicleNumber"))
    if form == "gym":
        g = meta.get("gym") or {}
        return bool(g.get("name") and g.get("location"))
    return True


def resolve_sequence_number(task: Task, sequence_number: int = 0) -> int:
    """Prefer explicit metadata sequence; treat 0 as missing."""
    meta = _meta(task)
    raw = sequence_number or meta.get("sequenceNumber") or meta.get("sequence_number")
    try:
        n = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else 0


def task_out(task: Task, sequence_number: int = 0, image_limit: int | None = 1) -> TaskOut:
    """image_limit=1 keeps campaign grids fast; None = all images."""
    campaign = task.campaign
    service = normalize_service_type(campaign.serviceType if campaign else None)
    form = detail_form_for(service)
    meta = _meta(task)
    loc = meta.get("location") or {}
    driver = meta.get("driver") or {}
    gym = meta.get("gym") or {}
    target = meta.get("target") or {}
    lat = target.get("latitude")
    lng = target.get("longitude")
    if lat is None:
        lat = loc.get("latitude")
    if lng is None:
        lng = loc.get("longitude")
    if lat is None and campaign:
        lat = campaign.latitude
    if lng is None and campaign:
        lng = campaign.longitude

    # Older proofs may lack per-image GPS — inherit task location so map view works.
    images = _images(meta, limit=image_limit, fallback_lat=lat, fallback_lng=lng)

    target_lat = target.get("latitude")
    target_lng = target.get("longitude")
    within_geofence = None
    if (
        target_lat is not None
        and target_lng is not None
        and campaign
        and campaign.latitude is not None
        and campaign.longitude is not None
    ):
        within_geofence = within_radius(
            campaign.latitude,
            campaign.longitude,
            target_lat,
            target_lng,
            DEFAULT_RADIUS_KM,
        )

    return TaskOut(
        id=task.id,
        campaign_id=task.campaignId,
        campaign_name=campaign.name if campaign else None,
        sequence_number=resolve_sequence_number(task, sequence_number),
        executor_user_id=task.executorUserId,
        executor_name=(
            f"{task.executor.firstName} {task.executor.lastName}".strip()
            if task.executor
            else None
        ),
        status=task.status.value if hasattr(task.status, "value") else str(task.status),
        latitude=lat,
        longitude=lng,
        within_geofence=within_geofence,
        required_photos=photos_for(service),
        service_type=service,
        detail_form=form,
        capture_slots=slots_for(service),
        details_complete=_details_complete(meta, form),
        proof_images=images,
        proof_image_count=_image_count(meta),
        driver_name=driver.get("name"),
        driver_phone=driver.get("phone"),
        vehicle_number=driver.get("vehicleNumber"),
        gym_name=gym.get("name"),
        gym_location=gym.get("location"),
        notes=task.notes,
        flagged=task.flagged,
        created_at=task.createdAt,
        completed_at=task.completedAt,
    )


def campaign_list_item_from_counts(
    c: Campaign,
    task_count: int,
    completed: int,
    pending: int,
) -> CampaignListItem:
    brand = brand_out(c.brand)
    # Prefer campaign logo, else brand logo (both proxied for private S3)
    display_logo = None
    if c.logo:
        display_logo = storage.viewable_url(c.logo)
    elif brand and brand.image:
        display_logo = brand.image

    return CampaignListItem(
        id=c.id,
        name=c.name,
        brand_id=c.brandId,
        brand=brand,
        service_type=normalize_service_type(c.serviceType),
        status=c.status,
        center_latitude=c.latitude,
        center_longitude=c.longitude,
        radius_km=DEFAULT_RADIUS_KM,
        address=c.address,
        total_tasks=c.totalTasks,
        created_at=c.createdAt,
        start_date=c.startDate,
        end_date=c.endDate,
        logo=display_logo,
        task_count=task_count,
        completed_task_count=completed,
        pending_task_count=pending,
        proof_image_count=completed,  # approx — avoid scanning all JSON on list
    )


def campaign_out(
    c: Campaign,
    tasks: list[Task] | None = None,
    *,
    task_count: int | None = None,
    completed: int | None = None,
    pending: int | None = None,
    page: int = 1,
    page_size: int = 24,
    tasks_total: int | None = None,
    image_limit: int | None = 1,
) -> CampaignOut:
    all_tasks = c.tasks or []
    if task_count is None:
        task_count = len(all_tasks)
    if completed is None:
        completed = sum(
            1
            for t in all_tasks
            if (t.status == TaskStatus.ACCEPTED)
            or (hasattr(t.status, "value") and t.status.value == "ACCEPTED")
        )
    if pending is None:
        pending = max(task_count - completed, 0)

    base = campaign_list_item_from_counts(c, task_count, completed, pending)

    use_tasks = tasks if tasks is not None else sorted(
        all_tasks, key=lambda t: (t.createdAt, str(t.id))
    )
    task_dtos = [
        task_out(
            t,
            sequence_number=(t.metadata_ or {}).get("sequenceNumber") or i,
            image_limit=image_limit,
        )
        for i, t in enumerate(use_tasks, start=1)
    ]

    executors = []
    members = []
    seen = set()
    name_by_id: dict = {}
    for m in c.members or []:
        if m.user:
            name_by_id[m.userId] = f"{m.user.firstName} {m.user.lastName}".strip()
    for m in c.members or []:
        if not m.active:
            continue
        members.append(member_out(m, assigner_name=name_by_id.get(m.assignedBy)))
        if m.role == CampaignRole.EXECUTOR and m.userId not in seen:
            seen.add(m.userId)
            if m.user:
                executors.append(user_out(m.user, role="executor"))

    return CampaignOut(
        **base.model_dump(),
        description=c.description or "",
        photos_per_task=photos_for(c.serviceType),
        organization_id=c.organizationId,
        tasks=task_dtos,
        executors=executors,
        members=members,
        tasks_page=page,
        tasks_page_size=page_size,
        tasks_total=tasks_total if tasks_total is not None else task_count,
    )
