import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    Float,
    Integer,
    DateTime,
    ForeignKey,
    Boolean,
    Enum,
    DateTime,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CampaignRole(str, enum.Enum):
    CAMPAIGN_MANAGER = "CAMPAIGN_MANAGER"
    SUPERVISOR = "SUPERVISOR"
    EXECUTOR = "EXECUTOR"
    BRAND_VIEWER = "BRAND_VIEWER"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class Organization(Base):
    __tablename__ = "Organization"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="organization")
    brands: Mapped[list["Brand"]] = relationship(back_populates="organization")
    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "User"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organizationId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Organization.id"), nullable=False
    )
    firstName: Mapped[str] = mapped_column(Text, nullable=False)
    lastName: Mapped[str] = mapped_column(Text, nullable=False, default="")
    phone: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    lastLoginAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="users")
    memberships: Mapped[list["CampaignMember"]] = relationship(back_populates="user")
    tasks: Mapped[list["Task"]] = relationship(back_populates="executor")


class Brand(Base):
    __tablename__ = "Brand"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organizationId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Organization.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="brands")
    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="brand")
    members: Mapped[list["BrandMember"]] = relationship(
        back_populates="brand", cascade="all, delete-orphan"
    )


class BrandMember(Base):
    """Brand-side viewers (supervisors) — view-only access to that brand's campaigns."""

    __tablename__ = "BrandMember"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brandId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Brand.id"), nullable=False, index=True
    )
    userId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("User.id"), nullable=False, index=True
    )
    assignedBy: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, default="SUPERVISOR")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    brand: Mapped["Brand"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()


class Campaign(Base):
    __tablename__ = "Campaign"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organizationId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Organization.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="ACTIVE")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    startDate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    endDate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    serviceType: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    totalTasks: Mapped[int] = mapped_column(Integer, default=0)
    brandId: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Brand.id"), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="campaigns")
    brand: Mapped["Brand | None"] = relationship(back_populates="campaigns")
    members: Mapped[list["CampaignMember"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignMember(Base):
    __tablename__ = "CampaignMember"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaignId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Campaign.id"), nullable=False
    )
    userId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
    )
    assignedBy: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[CampaignRole] = mapped_column(
        Enum(CampaignRole, name="CampaignRole", create_type=False),
        nullable=False,
    )
    assignedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    campaign: Mapped["Campaign"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")


class Task(Base):
    __tablename__ = "Task"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaignId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("Campaign.id"), nullable=False
    )
    executorUserId: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("User.id"), nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="TaskStatus", create_type=False),
        default=TaskStatus.PENDING,
    )
    assignedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    startedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text, nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # images[], location{}, driver{}, gym{}
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="tasks")
    executor: Mapped["User"] = relationship(back_populates="tasks")
