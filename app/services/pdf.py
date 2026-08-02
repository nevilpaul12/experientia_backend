"""Campaign / task PDF export — cover map page + per-task photo layout."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image as PILImage, ImageDraw
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.media_rules import normalize_service_type
from app.models import Campaign, Task, TaskStatus
from app.services.storage import storage

CHUNK_SIZE = 80
ZIP_THRESHOLD = 100

# Design tokens matching the attached template
BLUE = colors.Color(0.20, 0.47, 0.96)  # #3478F6-ish
RED = colors.Color(0.90, 0.18, 0.18)
INK = colors.Color(0.12, 0.14, 0.18)
MUTED = colors.Color(0.45, 0.48, 0.55)
LINE = colors.Color(0.88, 0.90, 0.93)
CARD_BG = colors.white


def _fit_image(data: bytes, max_w: float, max_h: float, *, quality: int = 70) -> Image | None:
    try:
        pil = PILImage.open(io.BytesIO(data))
        if pil.mode in ("RGBA", "P"):
            pil = pil.convert("RGB")
        max_px = 1400
        if max(pil.size) > max_px:
            resample = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS", PILImage.LANCZOS)
            pil.thumbnail((max_px, max_px), resample)
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=quality, optimize=True)
        buf.seek(0)
        img = Image(buf)
        iw, ih = img.imageWidth, img.imageHeight
        scale = min(max_w / iw, max_h / ih, 1.0)
        img.drawWidth = iw * scale
        img.drawHeight = ih * scale
        return img
    except Exception:
        return None


def _format_report_date(value: datetime | None = None) -> str:
    d = value or datetime.utcnow()
    return f"{d.day} {d.strftime('%B %Y')}"


def _format_short_date(value: datetime | None) -> str:
    if not value:
        return "—"
    return f"{value.day} {value.strftime('%b %Y')}"


def _task_status(task: Task) -> str:
    return task.status.value if hasattr(task.status, "value") else str(task.status)


def _is_completed(task: Task) -> bool:
    return _task_status(task) == TaskStatus.ACCEPTED.value


def _task_coords(task: Task, campaign: Campaign) -> tuple[float | None, float | None]:
    meta = task.metadata_ or {}
    target = meta.get("target") or {}
    loc = meta.get("location") or {}
    lat = target.get("latitude")
    lng = target.get("longitude")
    if lat is None:
        lat = loc.get("latitude")
    if lng is None:
        lng = loc.get("longitude")
    if lat is None and campaign.latitude is not None:
        lat = campaign.latitude
    if lng is None and campaign.longitude is not None:
        lng = campaign.longitude
    try:
        return (float(lat) if lat is not None else None, float(lng) if lng is not None else None)
    except (TypeError, ValueError):
        return None, None


def _task_address(task: Task, campaign: Campaign) -> str:
    meta = task.metadata_ or {}
    loc = meta.get("location") or {}
    if loc.get("address"):
        return str(loc["address"])
    if campaign.address:
        return campaign.address
    lat, lng = _task_coords(task, campaign)
    if lat is not None and lng is not None:
        return f"{lat:.6f}, {lng:.6f}"
    return "—"


def _reference_id(task: Task) -> str:
    return str(task.id).replace("-", "")[:8].upper()


def _fetch_url_bytes(url: str, timeout: float = 12.0) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ExperientiaPDF/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _static_map_image(
    center_lat: float,
    center_lng: float,
    *,
    width: int,
    height: int,
    zoom: int,
    markers: list[tuple[float, float]] | None = None,
    max_w: float,
    max_h: float,
) -> Image | None:
    """OSM static map (same provider used in the app UI)."""
    params = [
        ("center", f"{center_lat},{center_lng}"),
        ("zoom", str(zoom)),
        ("size", f"{width}x{height}"),
    ]
    # Cap markers to keep URL reasonable
    for mlat, mlng in (markers or [])[:45]:
        params.append(("markers", f"{mlat},{mlng},lightblue1"))
    url = "https://staticmap.openstreetmap.de/staticmap.php?" + urllib.parse.urlencode(params)
    data = _fetch_url_bytes(url)
    if not data:
        return _map_placeholder(max_w, max_h, center_lat, center_lng)
    return _fit_image(data, max_w, max_h, quality=80) or _map_placeholder(
        max_w, max_h, center_lat, center_lng
    )


def _map_placeholder(max_w: float, max_h: float, lat: float, lng: float) -> Image:
    """Soft grey map stand-in when the static map service is unreachable."""
    w, h = max(320, int(max_w)), max(200, int(max_h))
    pil = PILImage.new("RGB", (w, h), (236, 241, 247))
    draw = ImageDraw.Draw(pil)
    # Grid
    for x in range(0, w, 40):
        draw.line([(x, 0), (x, h)], fill=(220, 227, 236))
    for y in range(0, h, 40):
        draw.line([(0, y), (w, y)], fill=(220, 227, 236))
    cx, cy = w // 2, h // 2
    draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=(52, 120, 246))
    draw.text((12, h - 28), f"{lat:.4f}, {lng:.4f}", fill=(100, 116, 139))
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    img = Image(buf)
    img.drawWidth = max_w
    img.drawHeight = max_h
    return img


def _brand_logo_flow(campaign: Campaign, size: float = 18 * mm) -> Flowable:
    key = None
    url = None
    if campaign.logo:
        key = campaign.logo if not str(campaign.logo).startswith("http") else None
        url = campaign.logo
    elif campaign.brand and campaign.brand.image:
        key = (
            campaign.brand.image
            if not str(campaign.brand.image).startswith("http")
            and not str(campaign.brand.image).startswith("/")
            else None
        )
        url = campaign.brand.image

    data = storage.get_bytes(key, url or "") if (key or url) else None
    if data:
        try:
            pil = PILImage.open(io.BytesIO(data)).convert("RGBA")
            # Circular crop
            side = min(pil.size)
            left = (pil.width - side) // 2
            top = (pil.height - side) // 2
            pil = pil.crop((left, top, left + side, top + side))
            mask = PILImage.new("L", (side, side), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, side, side), fill=255)
            output = PILImage.new("RGBA", (side, side), (255, 255, 255, 0))
            output.paste(pil, (0, 0))
            output.putalpha(mask)
            bg = PILImage.new("RGB", (side, side), (255, 255, 255))
            bg.paste(output, mask=output.split()[-1])
            buf = io.BytesIO()
            bg.save(buf, format="JPEG", quality=90)
            buf.seek(0)
            img = Image(buf)
            img.drawWidth = size
            img.drawHeight = size
            return img
        except Exception:
            pass
    return _default_logo(size)


def _default_logo(size: float) -> Image:
    px = max(64, int(size * 2))
    pil = PILImage.new("RGB", (px, px), (255, 255, 255))
    draw = ImageDraw.Draw(pil)
    pad = 2
    draw.ellipse([pad, pad, px - pad, px - pad], fill=(52, 120, 246))
    # Simple "E" mark
    draw.rectangle([px * 0.32, px * 0.28, px * 0.42, px * 0.72], fill=(255, 255, 255))
    draw.rectangle([px * 0.32, px * 0.28, px * 0.68, px * 0.38], fill=(255, 255, 255))
    draw.rectangle([px * 0.32, px * 0.45, px * 0.62, px * 0.55], fill=(255, 255, 255))
    draw.rectangle([px * 0.32, px * 0.62, px * 0.68, px * 0.72], fill=(255, 255, 255))
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    img = Image(buf)
    img.drawWidth = size
    img.drawHeight = size
    return img


class RoundedRect(Flowable):
    """Filled rounded rectangle used as photo placeholder."""

    def __init__(self, width: float, height: float, fill=colors.Color(0.08, 0.12, 0.2)):
        super().__init__()
        self.width = width
        self.height = height
        self.fill = fill

    def draw(self):
        self.canv.setFillColor(self.fill)
        self.canv.roundRect(0, 0, self.width, self.height, 6, fill=1, stroke=0)


class LinkButton(Flowable):
    """Blue full-width button with clickable Google Maps link."""

    def __init__(self, width: float, height: float, label: str, url: str):
        super().__init__()
        self.width = width
        self.height = height
        self.label = label
        self.url = url

    def draw(self):
        c = self.canv
        c.setFillColor(BLUE)
        c.roundRect(0, 0, self.width, self.height, 6, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        text_w = c.stringWidth(self.label, "Helvetica-Bold", 10)
        c.drawString((self.width - text_w) / 2, self.height / 2 - 3.5, self.label)
        # Clickable area
        c.linkURL(self.url, (0, 0, self.width, self.height), relative=1)


def _styles():
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "CoverTitle",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=26,
            alignment=1,
            textColor=INK,
            spaceBefore=10,
            spaceAfter=14,
        ),
        "gen_date": ParagraphStyle(
            "GenDate",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=MUTED,
            alignment=2,
        ),
        "crumb": ParagraphStyle(
            "Crumb",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=MUTED,
            alignment=2,
        ),
        "label": ParagraphStyle(
            "FieldLabel",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.5,
            textColor=MUTED,
            spaceBefore=0,
            spaceAfter=2,
        ),
        "label_accent": ParagraphStyle(
            "FieldLabelAccent",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.5,
            textColor=RED,
            spaceBefore=6,
            spaceAfter=2,
        ),
        "value": ParagraphStyle(
            "FieldValue",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=INK,
            leading=15,
        ),
        "addr": ParagraphStyle(
            "Addr",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=INK,
        ),
        "stat_value": ParagraphStyle(
            "StatValue",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            alignment=1,
            textColor=INK,
        ),
        "stat_label": ParagraphStyle(
            "StatLabel",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            alignment=1,
            textColor=RED,
        ),
        "muted": ParagraphStyle(
            "Muted",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=MUTED,
        ),
    }


def _draw_chrome(canvas, _doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(BLUE)
    canvas.rect(0, h - 3.2 * mm, w, 3.2 * mm, fill=1, stroke=0)
    canvas.rect(0, 0, w, 3.2 * mm, fill=1, stroke=0)
    canvas.restoreState()


def _header_row(campaign: Campaign, right_text: str, page_w: float) -> Table:
    styles = _styles()
    logo = _brand_logo_flow(campaign, 14 * mm)
    right = Paragraph(right_text, styles["gen_date"] if "Report generated" in right_text else styles["crumb"])
    row = Table([[logo, right]], colWidths=[22 * mm, page_w - 22 * mm])
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return row


def _hairline(page_w: float) -> Table:
    line = Table([[""]], colWidths=[page_w])
    line.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return line


def _stat_card(value: str, label: str, width: float) -> Table:
    styles = _styles()
    inner = Table(
        [[Paragraph(value, styles["stat_value"])], [Paragraph(label, styles["stat_label"])]],
        colWidths=[width],
    )
    inner.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return inner


def _cover_flow(campaign: Campaign, tasks: list[Task], page_w: float) -> list:
    styles = _styles()
    service = normalize_service_type(campaign.serviceType)
    total = len(tasks)
    verified = sum(1 for t in tasks if _is_completed(t))
    pct = f"{round((verified / total) * 100)}%" if total else "0%"
    generated = _format_report_date()

    flow: list = []
    flow.append(_header_row(campaign, f"Report generated on {generated}", page_w))
    flow.append(_hairline(page_w))
    flow.append(Spacer(1, 6 * mm))
    flow.append(Paragraph(campaign.name or "Campaign report", styles["cover_title"]))
    flow.append(Spacer(1, 4 * mm))

    # Cover map with completed (or all) pins
    pins: list[tuple[float, float]] = []
    for t in tasks:
        lat, lng = _task_coords(t, campaign)
        if lat is not None and lng is not None:
            pins.append((lat, lng))
    center_lat = campaign.latitude
    center_lng = campaign.longitude
    if (center_lat is None or center_lng is None) and pins:
        center_lat = sum(p[0] for p in pins) / len(pins)
        center_lng = sum(p[1] for p in pins) / len(pins)
    if center_lat is not None and center_lng is not None:
        map_img = _static_map_image(
            float(center_lat),
            float(center_lng),
            width=900,
            height=520,
            zoom=13 if len(pins) > 1 else 15,
            markers=pins or [(float(center_lat), float(center_lng))],
            max_w=page_w,
            max_h=105 * mm,
        )
        if map_img:
            flow.append(map_img)
    else:
        flow.append(Paragraph("No map coordinates available for this campaign.", styles["muted"]))

    flow.append(Spacer(1, 8 * mm))

    gap = 3 * mm
    card_w = (page_w - 3 * gap) / 4
    stats = Table(
        [[
            _stat_card(service or "—", "Service Type", card_w),
            _stat_card(str(total), "Locations", card_w),
            _stat_card(str(verified), "Verified", card_w),
            _stat_card(pct, "Completion", card_w),
        ]],
        colWidths=[card_w, card_w, card_w, card_w],
    )
    stats.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-2, -1), gap),
                ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    flow.append(stats)
    return flow


def _photo_stack(task: Task, col_w: float) -> list:
    meta = task.metadata_ or {}
    images = meta.get("images") or []
    blocks: list = []
    slot_h = 48 * mm if len(images) <= 2 else 36 * mm
    if not images:
        blocks.append(RoundedRect(col_w, 70 * mm))
        return blocks

    for idx, img in enumerate(images):
        if isinstance(img, str):
            url, key = img, None
        else:
            url = img.get("url", "")
            key = img.get("key")
        data = storage.get_bytes(key, url)
        fitted = _fit_image(data, col_w, slot_h) if data else None
        if fitted:
            # Constrain width to column
            fitted.drawWidth = col_w
            # Keep aspect within slot_h
            if fitted.drawHeight > slot_h:
                scale = slot_h / fitted.drawHeight
                fitted.drawHeight = slot_h
                fitted.drawWidth = fitted.drawWidth * scale
            blocks.append(fitted)
        else:
            blocks.append(RoundedRect(col_w, slot_h))
        if idx < len(images) - 1:
            blocks.append(Spacer(1, 2.5 * mm))
    return blocks


def _task_page_flow(
    task: Task,
    campaign: Campaign,
    sequence: int,
    total: int,
    page_w: float,
) -> list:
    styles = _styles()
    meta = task.metadata_ or {}
    seq = meta.get("sequenceNumber") or sequence
    service = normalize_service_type(campaign.serviceType)
    completed_at = task.completedAt
    lat, lng = _task_coords(task, campaign)
    address = _task_address(task, campaign)
    ref = _reference_id(task)

    crumb = f"{campaign.name} · {service} · {seq}/{total}"
    flow: list = []
    flow.append(_header_row(campaign, crumb, page_w))
    flow.append(_hairline(page_w))
    flow.append(Spacer(1, 4 * mm))

    left_w = page_w * 0.40
    right_w = page_w * 0.58
    gap = page_w - left_w - right_w

    # Left photos
    left_bits = _photo_stack(task, left_w)
    left_table = Table([[b] for b in left_bits], colWidths=[left_w])
    left_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    # Right details
    right: list = []
    date_val = _format_short_date(completed_at) if _is_completed(task) else "—"
    meta_pair = Table(
        [[
            Paragraph("COMPLETED", styles["label"]),
            Paragraph("REFERENCE", styles["label"]),
        ], [
            Paragraph(date_val, styles["value"]),
            Paragraph(ref, styles["value"]),
        ]],
        colWidths=[right_w / 2, right_w / 2],
    )
    meta_pair.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (0, 0), 1),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    right.append(meta_pair)
    right.append(Paragraph("LOCATION", styles["label_accent"]))
    right.append(Paragraph(address.replace("\n", "<br/>"), styles["addr"]))
    right.append(Spacer(1, 4 * mm))

    if lat is not None and lng is not None:
        map_img = _static_map_image(
            lat,
            lng,
            width=640,
            height=360,
            zoom=16,
            markers=[(lat, lng)],
            max_w=right_w,
            max_h=52 * mm,
        )
        if map_img:
            map_img.drawWidth = right_w
            right.append(map_img)
        right.append(Spacer(1, 3 * mm))
        maps_url = f"https://www.google.com/maps?q={lat},{lng}"
        right.append(LinkButton(right_w, 11 * mm, "Open in Google Maps", maps_url))
    else:
        right.append(Paragraph("No GPS coordinates for this task.", styles["muted"]))

    right_table = Table([[b] for b in right], colWidths=[right_w])
    right_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    body = Table([[left_table, right_table]], colWidths=[left_w + gap, right_w])
    body.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), gap),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    flow.append(body)

    # Extra task details (driver/gym) below if present
    driver = meta.get("driver") or {}
    gym = meta.get("gym") or {}
    extras = []
    if driver.get("name") or driver.get("phone") or driver.get("vehicleNumber"):
        extras.append(
            f"<b>Driver:</b> {driver.get('name') or '—'} · {driver.get('phone') or '—'} · "
            f"{driver.get('vehicleNumber') or '—'}"
        )
    if gym.get("name") or gym.get("location"):
        extras.append(f"<b>Gym:</b> {gym.get('name') or '—'} · {gym.get('location') or '—'}")
    if extras:
        flow.append(Spacer(1, 6 * mm))
        for line in extras:
            flow.append(Paragraph(line, styles["addr"]))

    return flow


def _ordered_tasks(tasks: list[Task]) -> list[Task]:
    return sorted(
        tasks,
        key=lambda t: (
            (t.metadata_ or {}).get("sequenceNumber") or 10**9,
            t.createdAt,
            str(t.id),
        ),
    )


def _write_pdf_chunk(
    campaign: Campaign,
    tasks: list[Task],
    path: Path,
    *,
    sequence_offset: int = 0,
    total_override: int | None = None,
    cover_tasks: list[Task] | None = None,
    include_cover: bool = True,
    on_task_done: Callable[[int], None] | None = None,
) -> None:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    page_w = A4[0] - 28 * mm
    total = total_override if total_override is not None else len(tasks)
    story: list = []

    if include_cover:
        story.extend(_cover_flow(campaign, cover_tasks if cover_tasks is not None else tasks, page_w))

    for i, task in enumerate(tasks):
        if story:
            story.append(PageBreak())
        story.extend(
            _task_page_flow(task, campaign, sequence_offset + i + 1, total, page_w)
        )
        if on_task_done:
            on_task_done(i + 1)

    if not story:
        styles = _styles()
        story.append(_header_row(campaign, f"Report generated on {_format_report_date()}", page_w))
        story.append(_hairline(page_w))
        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph(campaign.name or "Campaign", styles["cover_title"]))
        story.append(Paragraph("No tasks to export.", styles["muted"]))

    doc.build(story, onFirstPage=_draw_chrome, onLaterPages=_draw_chrome)
    path.write_bytes(buffer.getvalue())


def build_campaign_pdf(campaign: Campaign, tasks: list[Task]) -> bytes:
    """In-memory single PDF (small campaigns / legacy sync endpoint)."""
    import tempfile

    ordered = _ordered_tasks(tasks)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        path = Path(fh.name)
    try:
        _write_pdf_chunk(campaign, ordered, path, include_cover=True, total_override=len(ordered))
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def build_campaign_export(
    campaign: Campaign,
    tasks: list[Task],
    *,
    out_dir: Path,
    stem: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[str, str, str]:
    """
    Build export on disk.
    Returns (file_path, download_filename, content_type).
    Large campaigns → ZIP of chunked PDFs so generation stays bounded.
    """
    ordered = _ordered_tasks(tasks)
    total = len(ordered)
    out_dir.mkdir(parents=True, exist_ok=True)

    if total == 0:
        empty = out_dir / f"{stem}.pdf"
        _write_pdf_chunk(campaign, [], empty, include_cover=True, total_override=0)
        if on_progress:
            on_progress(0, 0)
        return str(empty), f"experientia-{stem}.pdf", "application/pdf"

    if total < ZIP_THRESHOLD:
        pdf_path = out_dir / f"{stem}.pdf"

        def bump(n: int) -> None:
            if on_progress:
                on_progress(n, total)

        _write_pdf_chunk(
            campaign,
            ordered,
            pdf_path,
            include_cover=True,
            total_override=total,
            on_task_done=bump,
        )
        if on_progress:
            on_progress(total, total)
        return str(pdf_path), f"experientia-{stem}.pdf", "application/pdf"

    zip_path = out_dir / f"{stem}.zip"
    part = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for start in range(0, total, CHUNK_SIZE):
            part += 1
            chunk = ordered[start : start + CHUNK_SIZE]
            part_name = f"{stem}-part-{part:03d}-tasks-{start + 1}-{start + len(chunk)}.pdf"
            part_path = out_dir / part_name

            def bump(n: int, base: int = start) -> None:
                if on_progress:
                    on_progress(base + n, total)

            _write_pdf_chunk(
                campaign,
                chunk,
                part_path,
                sequence_offset=start,
                total_override=total,
                cover_tasks=ordered if start == 0 else None,
                include_cover=(start == 0),
                on_task_done=bump,
            )
            zf.write(part_path, arcname=part_name)
            part_path.unlink(missing_ok=True)
            if on_progress:
                on_progress(start + len(chunk), total)

        readme = (
            f"Experientia campaign export\n"
            f"Campaign: {campaign.name}\n"
            f"Tasks: {total}\n"
            f"Parts: {part} (up to {CHUNK_SIZE} tasks each)\n"
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"\nPart 1 includes the cover map. Open each PDF part separately.\n"
        )
        zf.writestr("README.txt", readme)

    return str(zip_path), f"experientia-{stem}.zip", "application/zip"


def build_task_pdf(task: Task, campaign: Campaign) -> bytes:
    """Single-task export — cover + one task page."""
    return build_campaign_pdf(campaign, [task])
