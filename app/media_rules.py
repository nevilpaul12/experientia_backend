"""Map Prisma serviceType strings ↔ capture rules."""

from __future__ import annotations

# DB stores human-readable serviceType (existing data)
SERVICE_TYPES = [
    "Auto Hood",
    "Gym",
    "No Parking Boards",
    "Pole Boards",
    "Shop Branding",
    "Other",
]

PHOTOS_REQUIRED: dict[str, int] = {
    "Auto Hood": 3,
    "Gym": 2,
    "No Parking Boards": 1,
    "Pole Boards": 1,
    "Shop Branding": 1,
    "Other": 3,
}

CAPTURE_SLOTS: dict[str, list[str]] = {
    "Auto Hood": ["front", "side", "back"],
    "Gym": ["photo_1", "photo_2"],
    "No Parking Boards": ["photo_1"],
    "Pole Boards": ["photo_1"],
    "Shop Branding": ["photo_1"],
    "Other": ["photo_1", "photo_2", "photo_3"],
}

SLOT_LABELS: dict[str, str] = {
    "front": "Front view",
    "side": "Side view",
    "back": "Back view",
    "photo_1": "Photo 1",
    "photo_2": "Photo 2",
    "photo_3": "Photo 3",
}

# "driver" | "gym" | None
DETAIL_FORM: dict[str, str | None] = {
    "Auto Hood": "driver",
    "Gym": "gym",
    "No Parking Boards": None,
    "Pole Boards": None,
    "Shop Branding": None,
    "Other": None,
}

DEFAULT_RADIUS_KM = 3.0


def campaign_radius_km(campaign) -> float:
    """Per-campaign geofence radius in km (falls back to default)."""
    raw = getattr(campaign, "radiusKm", None)
    if raw is None:
        return DEFAULT_RADIUS_KM
    try:
        r = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RADIUS_KM
    return r if r > 0 else DEFAULT_RADIUS_KM


def normalize_service_type(value: str | None) -> str:
    if not value:
        return "Other"
    for s in SERVICE_TYPES:
        if s.lower() == value.lower():
            return s
    # fuzzy
    v = value.lower()
    if "auto" in v and "hood" in v:
        return "Auto Hood"
    if "gym" in v:
        return "Gym"
    if "parking" in v:
        return "No Parking Boards"
    if "pole" in v:
        return "Pole Boards"
    if "shop" in v:
        return "Shop Branding"
    return value if value in PHOTOS_REQUIRED else "Other"


def photos_for(service_type: str | None) -> int:
    return PHOTOS_REQUIRED.get(normalize_service_type(service_type), 3)


def slots_for(service_type: str | None) -> list[str]:
    st = normalize_service_type(service_type)
    return CAPTURE_SLOTS.get(st) or [f"photo_{i}" for i in range(1, photos_for(st) + 1)]


def detail_form_for(service_type: str | None) -> str | None:
    return DETAIL_FORM.get(normalize_service_type(service_type))


def slot_label(slot: str) -> str:
    return SLOT_LABELS.get(slot, slot.replace("_", " ").title())
