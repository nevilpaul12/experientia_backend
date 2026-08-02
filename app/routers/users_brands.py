from uuid import uuid4, UUID
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import field_validator

from app.auth import CurrentUser, ManagerUser, DbSession, resolve_user_role, get_user_by_phone
from app.models import (
    User,
    Brand,
    BrandMember,
    Campaign,
    CampaignMember,
    CampaignRole,
)
from app.schemas import UserOut, BrandOut, BrandCreate, BrandUpdate, TeamUserCreate, TeamMemberOut
from app.serializers import user_out, brand_out
from app.services.storage import storage

router = APIRouter(prefix="/api", tags=["users-brands"])


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    # Drop leading country / trunk codes common in India
    if len(digits) >= 12 and digits.startswith("91"):
        digits = digits[-10:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[-10:]
    if len(digits) != 10:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid 10-digit phone number (e.g. 9876543210)",
        )
    return digits


@router.get("/users/executors", response_model=list[UserOut])
def list_executors(db: DbSession, user: ManagerUser):
    org_users = (
        db.query(User)
        .filter(User.organizationId == user.organizationId, User.isActive.is_(True))
        .order_by(User.firstName)
        .all()
    )
    executor_ids = {
        m.userId
        for m in db.query(CampaignMember)
        .filter(
            CampaignMember.role == CampaignRole.EXECUTOR,
            CampaignMember.active.is_(True),
        )
        .all()
    }
    # Users who are only supervisors should not appear as field executors
    supervisor_only = {
        m.userId
        for m in db.query(BrandMember)
        .filter(BrandMember.active.is_(True))
        .all()
    }
    picked = [u for u in org_users if u.id in executor_ids and u.id not in supervisor_only]
    if not picked:
        picked = [u for u in org_users if u.id not in supervisor_only] or org_users
    return [user_out(u, role="executor") for u in picked]


@router.get("/users/supervisors", response_model=list[UserOut])
def list_supervisors(db: DbSession, user: ManagerUser, brand_id: UUID | None = None):
    q = (
        db.query(BrandMember, User, Brand)
        .join(User, User.id == BrandMember.userId)
        .join(Brand, Brand.id == BrandMember.brandId)
        .filter(
            Brand.organizationId == user.organizationId,
            BrandMember.active.is_(True),
            User.isActive.is_(True),
        )
    )
    if brand_id:
        q = q.filter(BrandMember.brandId == brand_id)
    rows = q.order_by(User.firstName).all()
    seen = set()
    out = []
    for _bm, u, _b in rows:
        if u.id in seen:
            continue
        seen.add(u.id)
        out.append(user_out(u, role="supervisor"))
    return out


@router.get("/team", response_model=list[TeamMemberOut])
def list_team(db: DbSession, user: ManagerUser):
    org_users = (
        db.query(User)
        .filter(User.organizationId == user.organizationId, User.isActive.is_(True))
        .order_by(User.firstName)
        .all()
    )
    brand_links = {
        bm.userId: bm
        for bm in db.query(BrandMember)
        .filter(BrandMember.active.is_(True))
        .all()
    }
    brands = {
        b.id: b
        for b in db.query(Brand).filter(Brand.organizationId == user.organizationId).all()
    }
    manager_ids = {
        m.userId
        for m in db.query(CampaignMember)
        .filter(
            CampaignMember.role == CampaignRole.CAMPAIGN_MANAGER,
            CampaignMember.active.is_(True),
        )
        .all()
    }
    out: list[TeamMemberOut] = []
    for u in org_users:
        bm = brand_links.get(u.id)
        if u.id in manager_ids:
            role = "manager"
            brand_id = None
            brand_name = None
        elif bm:
            role = "supervisor"
            brand_id = bm.brandId
            brand_name = brands[bm.brandId].name if bm.brandId in brands else None
        else:
            role = "executor"
            brand_id = None
            brand_name = None
        out.append(
            TeamMemberOut(
                id=u.id,
                first_name=u.firstName,
                last_name=u.lastName,
                full_name=f"{u.firstName} {u.lastName}".strip(),
                phone=u.phone,
                role=role,
                brand_id=brand_id,
                brand_name=brand_name,
                active=u.isActive,
            )
        )
    return out


@router.post("/team", response_model=TeamMemberOut)
def create_team_member(payload: TeamUserCreate, db: DbSession, user: ManagerUser):
    role = (payload.role or "executor").strip().lower()
    if role not in ("executor", "supervisor"):
        raise HTTPException(status_code=400, detail="Role must be executor or supervisor")
    if role == "supervisor" and not payload.brand_id:
        raise HTTPException(status_code=400, detail="Supervisors must belong to a brand")

    phone = _normalize_phone(payload.phone)
    existing = get_user_by_phone(db, phone)
    if existing and existing.organizationId != user.organizationId:
        raise HTTPException(status_code=400, detail="Phone already used in another organization")

    brand = None
    if payload.brand_id:
        brand = (
            db.query(Brand)
            .filter(
                Brand.id == payload.brand_id,
                Brand.organizationId == user.organizationId,
                Brand.isActive.is_(True),
            )
            .first()
        )
        if not brand:
            raise HTTPException(status_code=404, detail="Brand not found")

    if existing:
        member = existing
        member.firstName = payload.first_name.strip() or member.firstName
        member.lastName = (payload.last_name or "").strip()
        member.isActive = True
        member.updatedAt = datetime.utcnow()
    else:
        member = User(
            id=uuid4(),
            organizationId=user.organizationId,
            firstName=payload.first_name.strip(),
            lastName=(payload.last_name or "").strip(),
            phone=phone,
            isActive=True,
        )
        db.add(member)
        db.flush()

    if role == "supervisor" and brand:
        # Upsert brand membership
        link = (
            db.query(BrandMember)
            .filter(BrandMember.brandId == brand.id, BrandMember.userId == member.id)
            .first()
        )
        if link:
            link.active = True
            link.assignedBy = user.id
        else:
            db.add(
                BrandMember(
                    id=uuid4(),
                    brandId=brand.id,
                    userId=member.id,
                    assignedBy=user.id,
                    role="SUPERVISOR",
                    active=True,
                )
            )
        # Attach as SUPERVISOR on all existing campaigns for this brand
        campaigns = (
            db.query(Campaign)
            .filter(
                Campaign.brandId == brand.id,
                Campaign.organizationId == user.organizationId,
                Campaign.isActive.is_(True),
            )
            .all()
        )
        for c in campaigns:
            existing_m = (
                db.query(CampaignMember)
                .filter(
                    CampaignMember.campaignId == c.id,
                    CampaignMember.userId == member.id,
                    CampaignMember.role == CampaignRole.SUPERVISOR,
                )
                .first()
            )
            if existing_m:
                existing_m.active = True
            else:
                db.add(
                    CampaignMember(
                        id=uuid4(),
                        campaignId=c.id,
                        userId=member.id,
                        assignedBy=user.id,
                        role=CampaignRole.SUPERVISOR,
                        active=True,
                        location=brand.name,
                    )
                )
    elif role == "executor":
        # Ensure they are not stuck as inactive brand supervisors
        for link in (
            db.query(BrandMember).filter(BrandMember.userId == member.id, BrandMember.active.is_(True)).all()
        ):
            # Keep brand link if intentionally dual-role; for pure executor creation leave as-is
            pass

    db.commit()
    db.refresh(member)
    return TeamMemberOut(
        id=member.id,
        first_name=member.firstName,
        last_name=member.lastName,
        full_name=f"{member.firstName} {member.lastName}".strip(),
        phone=member.phone,
        role=role,
        brand_id=brand.id if brand else None,
        brand_name=brand.name if brand else None,
        active=True,
    )


@router.get("/brands", response_model=list[BrandOut])
def list_brands(db: DbSession, user: CurrentUser):
    brands = (
        db.query(Brand)
        .filter(Brand.organizationId == user.organizationId, Brand.isActive.is_(True))
        .order_by(Brand.name)
        .all()
    )
    return [brand_out(b) for b in brands]


@router.post("/brands", response_model=BrandOut)
def create_brand(payload: BrandCreate, db: DbSession, user: ManagerUser):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Brand name is required")
    image_ref = payload.storage_key or payload.image
    if image_ref and image_ref.startswith("http"):
        # Prefer storing the S3 key when we can derive it
        derived = storage.key_from_url(image_ref)
        image_ref = derived or image_ref
    brand = Brand(
        id=uuid4(),
        organizationId=user.organizationId,
        name=name,
        description=payload.description,
        image=image_ref,
        isActive=True,
    )
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand_out(brand)


@router.patch("/brands/{brand_id}", response_model=BrandOut)
def update_brand(brand_id: UUID, payload: BrandUpdate, db: DbSession, user: ManagerUser):
    brand = (
        db.query(Brand)
        .filter(
            Brand.id == brand_id,
            Brand.organizationId == user.organizationId,
            Brand.isActive.is_(True),
        )
        .first()
    )
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        brand.name = data["name"].strip()
    if "description" in data:
        brand.description = data["description"]
    image_ref = data.get("storage_key") or data.get("image")
    if image_ref is not None:
        if image_ref.startswith("http"):
            derived = storage.key_from_url(image_ref)
            image_ref = derived or image_ref
        brand.image = image_ref or None
    brand.updatedAt = __import__("datetime").datetime.utcnow()
    db.commit()
    db.refresh(brand)
    return brand_out(brand)
