from datetime import datetime, timedelta
from typing import Annotated
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User, CampaignMember, CampaignRole, BrandMember, Campaign

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
settings = get_settings()


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def get_user_by_phone(db: Session, phone: str) -> User | None:
    cleaned = "".join(ch for ch in phone if ch.isdigit())
    users = db.query(User).filter(User.isActive.is_(True)).all()
    for u in users:
        up = "".join(ch for ch in u.phone if ch.isdigit())
        if up == cleaned or up[-10:] == cleaned[-10:]:
            return u
    return None


def verify_login_otp(db: Session, user: User, otp: str) -> None:
    code = "".join(ch for ch in (otp or "") if ch.isdigit())
    if not code:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OTP is required",
        )
    expected = (
        settings.manager_otp if user_is_manager(db, user) else settings.executor_otp
    )
    expected = "".join(ch for ch in expected if ch.isdigit())
    if code != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OTP",
        )


def user_is_manager(db: Session, user: User) -> bool:
    return (
        db.query(CampaignMember)
        .filter(
            CampaignMember.userId == user.id,
            CampaignMember.active.is_(True),
            CampaignMember.role == CampaignRole.CAMPAIGN_MANAGER,
        )
        .first()
        is not None
    )


def user_is_supervisor(db: Session, user: User) -> bool:
    """Brand supervisors — view-only. Managers are not treated as supervisors."""
    if user_is_manager(db, user):
        return False
    if (
        db.query(BrandMember)
        .filter(BrandMember.userId == user.id, BrandMember.active.is_(True))
        .first()
        is not None
    ):
        return True
    return (
        db.query(CampaignMember)
        .filter(
            CampaignMember.userId == user.id,
            CampaignMember.active.is_(True),
            CampaignMember.role == CampaignRole.SUPERVISOR,
        )
        .first()
        is not None
    )


def resolve_user_role(db: Session, user: User) -> str:
    if user_is_manager(db, user):
        return "manager"
    if user_is_supervisor(db, user):
        return "supervisor"
    return "executor"


def supervisor_brand_ids(db: Session, user: User) -> set[uuid.UUID]:
    return {
        r[0]
        for r in db.query(BrandMember.brandId)
        .filter(BrandMember.userId == user.id, BrandMember.active.is_(True))
        .all()
    }


def can_view_campaign(db: Session, user: User, campaign: Campaign) -> bool:
    if user.organizationId != campaign.organizationId:
        return False
    if user_is_manager(db, user):
        return True
    brands = supervisor_brand_ids(db, user)
    if campaign.brandId and campaign.brandId in brands:
        return True
    m = (
        db.query(CampaignMember.id)
        .filter(
            CampaignMember.campaignId == campaign.id,
            CampaignMember.userId == user.id,
            CampaignMember.active.is_(True),
        )
        .first()
    )
    if m:
        return True
    from app.models import Task

    t = (
        db.query(Task.id)
        .filter(Task.campaignId == campaign.id, Task.executorUserId == user.id)
        .first()
    )
    return t is not None


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        sub = payload.get("sub")
        if sub is None:
            raise credentials_exception
        user_id = uuid.UUID(str(sub))
    except (JWTError, ValueError):
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id, User.isActive.is_(True)).first()
    if user is None:
        raise credentials_exception
    return user


async def require_manager(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if not user_is_manager(db, user):
        raise HTTPException(status_code=403, detail="Campaign manager access required")
    return user


async def require_export_access(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Managers and brand supervisors may export PDFs."""
    if user_is_manager(db, user) or user_is_supervisor(db, user):
        return user
    raise HTTPException(status_code=403, detail="Export access required")


CurrentUser = Annotated[User, Depends(get_current_user)]
ManagerUser = Annotated[User, Depends(require_manager)]
ExportUser = Annotated[User, Depends(require_export_access)]
DbSession = Annotated[Session, Depends(get_db)]
