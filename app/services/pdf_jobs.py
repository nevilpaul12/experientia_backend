"""Background PDF / ZIP export queue — jobs keep running across page navigation."""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.database import SessionLocal
from app.models import Campaign, Task
from app.services.pdf import build_campaign_export


class JobStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    ready = "ready"
    failed = "failed"


@dataclass
class ExportJob:
    id: str
    campaign_id: str
    user_id: str
    campaign_name: str
    status: JobStatus = JobStatus.queued
    total: int = 0
    done: int = 0
    error: str | None = None
    file_path: str | None = None
    filename: str | None = None
    content_type: str = "application/pdf"
    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    queue_position: int | None = None

    def to_dict(self) -> dict:
        pct = int((self.done / self.total) * 100) if self.total else 0
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "status": self.status.value,
            "total": self.total,
            "done": self.done,
            "percent": pct,
            "error": self.error,
            "filename": self.filename,
            "download_ready": self.status == JobStatus.ready and bool(self.file_path),
            "queue_position": self.queue_position,
            "created_at": self.created_at.isoformat() + "Z",
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
        }


_jobs: dict[str, ExportJob] = {}
_lock = threading.Lock()
_work_q: queue.Queue[str] = queue.Queue()
_JOB_TTL = timedelta(hours=12)
_worker_started = False
_worker_lock = threading.Lock()


def _exports_dir() -> Path:
    path = Path(get_settings().upload_dir) / "pdf-exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(name: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or ""))[:40]
    return cleaned or fallback


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="pdf-export-worker")
        t.start()
        _worker_started = True


def _refresh_queue_positions() -> None:
    """Update queue_position for queued jobs (1 = next up)."""
    queued = sorted(
        (j for j in _jobs.values() if j.status == JobStatus.queued),
        key=lambda j: j.created_at,
    )
    for i, j in enumerate(queued, start=1):
        j.queue_position = i
    for j in _jobs.values():
        if j.status != JobStatus.queued:
            j.queue_position = None


def get_job(job_id: str) -> ExportJob | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        if job.finished_at and datetime.utcnow() - job.finished_at > _JOB_TTL:
            _cleanup_job(job)
            _jobs.pop(job_id, None)
            return None
        return job


def list_campaign_jobs(campaign_id: str, user_id: str) -> list[ExportJob]:
    with _lock:
        _purge_expired_unlocked()
        return [
            j
            for j in _jobs.values()
            if j.campaign_id == campaign_id and j.user_id == user_id
        ]


def list_user_jobs(user_id: str) -> list[ExportJob]:
    with _lock:
        _purge_expired_unlocked()
        jobs = [j for j in _jobs.values() if j.user_id == user_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)


def _purge_expired_unlocked() -> None:
    now = datetime.utcnow()
    dead = [
        jid
        for jid, j in _jobs.items()
        if j.finished_at and now - j.finished_at > _JOB_TTL
    ]
    for jid in dead:
        _cleanup_job(_jobs[jid])
        _jobs.pop(jid, None)


def _cleanup_job(job: ExportJob) -> None:
    if job.file_path:
        try:
            Path(job.file_path).unlink(missing_ok=True)
        except OSError:
            pass


def start_export_job(campaign_id: uuid.UUID, user_id: uuid.UUID, campaign_name: str) -> ExportJob:
    """Enqueue an export. Never cancels existing jobs — they keep running."""
    _ensure_worker()
    job = ExportJob(
        id=str(uuid.uuid4()),
        campaign_id=str(campaign_id),
        user_id=str(user_id),
        campaign_name=campaign_name,
        status=JobStatus.queued,
    )
    with _lock:
        _jobs[job.id] = job
        _refresh_queue_positions()
    _work_q.put(job.id)
    return job


def _worker_loop() -> None:
    while True:
        job_id = _work_q.get()
        try:
            _run_job(job_id)
        finally:
            _work_q.task_done()
            with _lock:
                _refresh_queue_positions()


def _run_job(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    if job.status not in (JobStatus.queued, JobStatus.processing):
        return

    campaign_uuid = uuid.UUID(job.campaign_id)
    db = SessionLocal()
    try:
        job.status = JobStatus.processing
        job.queue_position = None
        campaign = (
            db.query(Campaign)
            .options(joinedload(Campaign.brand))
            .filter(Campaign.id == campaign_uuid)
            .first()
        )
        if not campaign:
            raise RuntimeError("Campaign not found")

        tasks = (
            db.query(Task)
            .filter(Task.campaignId == campaign_uuid)
            .order_by(Task.createdAt.asc())
            .all()
        )
        for t in tasks:
            t.campaign = campaign

        job.total = len(tasks)
        job.campaign_name = campaign.name
        stem = _safe_name(campaign.name, str(campaign_uuid)[:8])
        out_dir = _exports_dir() / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        def on_progress(done: int, total: int) -> None:
            job.done = done
            job.total = total

        result_path, filename, content_type = build_campaign_export(
            campaign,
            tasks,
            out_dir=out_dir,
            stem=stem,
            on_progress=on_progress,
        )

        final = _exports_dir() / f"{job_id}-{filename}"
        Path(result_path).replace(final)
        try:
            for p in out_dir.iterdir():
                p.unlink(missing_ok=True)
            out_dir.rmdir()
        except OSError:
            pass

        job.file_path = str(final)
        job.filename = filename
        job.content_type = content_type
        job.done = job.total
        job.status = JobStatus.ready
        job.finished_at = datetime.utcnow()
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed
        job.error = str(exc) or exc.__class__.__name__
        job.finished_at = datetime.utcnow()
    finally:
        db.close()
