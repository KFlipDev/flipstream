"""The RQ job: download, encode, save to the share. One worker, one job at a time.

States (pipeline page, fig. 2): queued -> downloading -> encoding -> saving ->
done | failed. The worker writes the state and progress into the job's meta;
the job page polls it.

Retries: network errors and an unreachable share raise, and RQ runs the job
again, up to 3 times. Anything else (a block, a missing video, an ffmpeg error)
fails at once with its message.
"""

import logging
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yt_dlp
from redis import Redis
from rq import Queue, Retry
from rq.job import Job, JobStatus, get_current_job
from yt_dlp.utils import DownloadError

from . import media, youtube
from .config import Settings

log = logging.getLogger("flipstream.jobs")

QUEUE = "flipstream"
RETRY = Retry(max=3, interval=[30, 120, 300])
JOB_TIMEOUT = 3600  # seconds; a 2-hour mix takes a few minutes
KEEP_RECORD = 7 * 24 * 3600  # how long the job page keeps working after the end
STATES = ["queued", "downloading", "encoding", "saving", "done", "failed"]


class ShareUnavailable(youtube.TransientError):
    pass


@dataclass
class JobRequest:
    url: str
    title: str
    artist: str
    genre: str
    tags: str
    description: str
    year: str = ""


def queue(conn: Redis) -> Queue:
    return Queue(QUEUE, connection=conn)


def enqueue(conn: Redis, req: JobRequest) -> Job:
    job = queue(conn).enqueue(
        process,
        asdict(req),
        retry=RETRY,
        job_timeout=JOB_TIMEOUT,
        result_ttl=KEEP_RECORD,
        failure_ttl=KEEP_RECORD,
        meta={"state": "queued", "title": req.title},
        description=req.title[:120],
    )
    return job


# --- worker side ------------------------------------------------------------


class Reporter:
    """Writes state and progress to the job's meta, at most once a second."""

    def __init__(self, job: Job | None):
        self.job = job
        self._last = 0.0

    def state(self, state: str, **extra: Any) -> None:
        if not self.job:
            return
        self.job.meta.update({"state": state, "progress": None, **extra})
        self.job.save_meta()
        self._last = time.monotonic()

    def progress(self, value: dict[str, Any] | float, force: bool = False) -> None:
        if not self.job:
            return
        now = time.monotonic()
        if not force and now - self._last < 1.0:
            return
        self.job.meta["progress"] = value
        self.job.save_meta()
        self._last = now


def _download(url: str, scratch: Path, report: Reporter) -> tuple[Path, Path | None, dict]:
    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            report.progress(
                {
                    "done_mb": round((d.get("downloaded_bytes") or 0) / 1e6, 1),
                    "total_mb": round(total / 1e6, 1),
                }
            )

    opts = youtube.BASE_OPTS | {
        "format": "bestaudio/best",
        "outtmpl": str(scratch / "audio.%(ext)s"),
        "writethumbnail": True,
        "overwrites": True,
        "quiet": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    youtube.to_video_info(url, info)  # same checks as the form (playlist, live, length)
    audio = Path(info["requested_downloads"][0]["filepath"])
    images = (".jpg", ".jpeg", ".webp", ".png")
    thumbs = [p for p in scratch.iterdir() if p.suffix.lower() in images]
    return audio, (thumbs[0] if thumbs else None), info


def _check_share(settings: Settings) -> Path:
    try:
        out = settings.output_dir
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".flipstream-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        return out
    except OSError as exc:
        raise ShareUnavailable(f"share unreachable: {exc.strerror or exc}") from exc


def _fail_now(job: Job | None, message: str) -> None:
    """Record the message and turn retries off, so RQ marks the job failed."""
    if job:
        job.retries_left = 0
        job.meta.update({"state": "failed", "message": message, "progress": None})
        job.save_meta()


def run(req: JobRequest, settings: Settings, job: Job | None = None) -> dict[str, Any]:
    report = Reporter(job)
    scratch = settings.scratch_dir / (job.id if job else "local")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        report.state("downloading", message=None)
        try:
            audio, thumb, info = _download(req.url, scratch, report)
        except DownloadError as exc:
            msg = youtube.clean_message(exc.msg or str(exc))
            if youtube.is_transient(exc):
                raise youtube.TransientError(msg) from exc
            raise youtube.LookupFailed(msg) from exc

        report.state("encoding")
        cover = media.square_cover(thumb, scratch / "cover.jpg") if thumb else None
        tags = media.Tags(
            title=req.title,
            artist=req.artist,
            genre=req.genre,
            comment=req.description[:1000],
            year=req.year or str(info.get("upload_date") or "")[:4],
        )
        mp3 = media.encode_mp3(
            audio,
            cover,
            tags,
            scratch / "out.mp3",
            float(info.get("duration") or 0),
            lambda f: report.progress(round(f * 100)),
        )

        report.state("saving")
        out_dir = _check_share(settings)
        name = media.mp3_name(req.title)
        try:
            media.copy_with_progress(mp3, out_dir / name, lambda f: report.progress(round(f * 100)))
        except OSError as exc:
            raise ShareUnavailable(f"share unreachable: {exc.strerror or exc}") from exc

        size_mb = round(mp3.stat().st_size / 1e6, 1)
        report.state("done", filename=name, size_mb=size_mb, message=None)
        log.info("saved %s (%s MB)", name, size_mb)
        return {"filename": name, "size_mb": size_mb}
    except youtube.TransientError as exc:
        # Leave retries on; the page shows the message while RQ waits.
        if job:
            job.meta.update({"message": str(exc), "progress": None})
            job.save_meta()
        raise
    except (youtube.LookupFailed, media.EncodeFailed) as exc:
        _fail_now(job, str(exc))
        raise
    except Exception as exc:
        _fail_now(job, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def process(req: dict[str, Any]) -> dict[str, Any]:
    """RQ entry point (runs in the worker's work horse)."""
    return run(JobRequest(**req), Settings.from_env(), get_current_job())


# --- web side -----------------------------------------------------------------


@dataclass
class JobView:
    id: str
    state: str
    title: str
    message: str | None = None
    progress: Any = None
    filename: str | None = None
    size_mb: float | None = None
    attempt: int = 1
    retry_wait: bool = False

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed")

    @property
    def step(self) -> int:
        return STATES.index(self.state) if self.state in STATES else 0


def view(conn: Redis, job_id: str) -> JobView | None:
    try:
        job = Job.fetch(job_id, connection=conn)
    except Exception:  # NoSuchJobError, or an id that isn't one
        return None
    meta = job.get_meta(refresh=True)
    status = job.get_status(refresh=True)
    state = meta.get("state", "queued")
    retries_left = job.retries_left if job.retries_left is not None else RETRY.max
    v = JobView(
        id=job.id,
        state=state,
        title=meta.get("title") or job.description or "",
        message=meta.get("message"),
        progress=meta.get("progress"),
        filename=meta.get("filename"),
        size_mb=meta.get("size_mb"),
        attempt=RETRY.max - retries_left + 1,
    )
    if status in (JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED):
        v.state = "failed"
        if not v.message:
            v.message = _last_line(job)
    elif status == JobStatus.FINISHED:
        v.state = "done"
    elif status in (JobStatus.SCHEDULED, JobStatus.QUEUED) and v.attempt > 1:
        v.state, v.retry_wait = "queued", True
    return v


def _last_line(job: Job) -> str:
    try:
        exc = job.latest_result().exc_string if job.latest_result() else ""
    except Exception:
        exc = ""
    lines = [ln for ln in (exc or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "failed"
