"""The job and its RQ retries, run in-process with fakeredis and SimpleWorker."""

import sys
from pathlib import Path

import fakeredis
import pytest
from rq import SimpleWorker
from rq.job import JobStatus
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from flipstream import jobs, media
from flipstream.config import Settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    share, scratch = tmp_path / "share", tmp_path / "scratch"
    share.mkdir()
    monkeypatch.setenv("FLIPSTREAM_SHARE_ROOT", str(share))
    monkeypatch.setenv("FLIPSTREAM_SCRATCH", str(scratch))
    settings = Settings.from_env()
    return settings, fakeredis.FakeStrictRedis()


REQ = jobs.JobRequest(
    url="https://youtu.be/abc",
    title="Sunset: Mix",
    artist="DJ K",
    genre="House",
    tags="house",
    description="two hours",
)


def fake_download(audio_bytes=b"audio"):
    def _download(url, scratch: Path, report):
        report.progress({"done_mb": 1.0, "total_mb": 2.0}, force=True)
        audio = scratch / "audio.webm"
        audio.write_bytes(audio_bytes)
        return audio, None, {"duration": 10, "upload_date": "20261101"}

    return _download


def fake_encode(audio, cover, tags, out, duration, progress=None):
    out.write_bytes(b"ID3" + tags.title.encode())
    if progress:
        progress(1.0)
    return out


def raising(exc):
    def _download(*a, **k):
        raise exc

    return _download


def wrapped(exc):
    try:
        raise exc
    except Exception:
        return DownloadError(f"ERROR: {exc}", sys.exc_info())


def work(conn):
    SimpleWorker([jobs.QUEUE], connection=conn).work(burst=True)


def test_job_saves_mp3_and_cleans_scratch(env, monkeypatch):
    settings, conn = env
    monkeypatch.setattr(jobs, "_download", fake_download())
    monkeypatch.setattr(media, "encode_mp3", fake_encode)
    job = jobs.enqueue(conn, REQ)
    assert jobs.view(conn, job.id).state == "queued"
    work(conn)

    v = jobs.view(conn, job.id)
    assert v.state == "done" and v.finished
    assert v.filename == "Sunset Mix.mp3"
    assert (settings.output_dir / "Sunset Mix.mp3").read_bytes() == b"ID3Sunset: Mix"
    assert not any(settings.scratch_dir.iterdir())
    assert not list(settings.output_dir.glob("*.part"))


def test_network_error_is_retried(env, monkeypatch):
    _, conn = env
    monkeypatch.setattr(jobs, "_download", raising(wrapped(TransportError("connection reset"))))
    job = jobs.enqueue(conn, REQ)
    work(conn)
    job.refresh()
    assert job.get_status() == JobStatus.SCHEDULED
    assert job.retries_left == 2
    v = jobs.view(conn, job.id)
    assert v.state == "queued" and v.retry_wait and v.attempt == 2
    assert "connection reset" in v.message


def test_missing_video_fails_without_retry(env, monkeypatch):
    settings, conn = env
    exc = wrapped(ExtractorError("[youtube] abc: Video unavailable", expected=True))
    monkeypatch.setattr(jobs, "_download", raising(exc))
    job = jobs.enqueue(conn, REQ)
    work(conn)
    job.refresh()
    assert job.get_status() == JobStatus.FAILED
    v = jobs.view(conn, job.id)
    assert v.state == "failed"
    assert "Video unavailable" in v.message
    assert not any(settings.scratch_dir.iterdir())


def test_unreachable_share_is_retried(env, monkeypatch):
    settings, conn = env
    monkeypatch.setattr(jobs, "_download", fake_download())
    monkeypatch.setattr(media, "encode_mp3", fake_encode)
    settings.share_root.rmdir()
    settings.share_root.write_text("not a folder")  # mkdir/write fails like a dead mount
    job = jobs.enqueue(conn, REQ)
    work(conn)
    job.refresh()
    assert job.get_status() == JobStatus.SCHEDULED
    assert "share unreachable" in jobs.view(conn, job.id).message


def test_ffmpeg_error_fails_without_retry(env, monkeypatch):
    _, conn = env
    monkeypatch.setattr(jobs, "_download", fake_download())

    def broken(*a, **k):
        raise media.EncodeFailed("ffmpeg: Invalid data found when processing input")

    monkeypatch.setattr(media, "encode_mp3", broken)
    job = jobs.enqueue(conn, REQ)
    work(conn)
    v = jobs.view(conn, job.id)
    assert v.state == "failed" and "Invalid data" in v.message


def test_unknown_job_is_none(env):
    assert jobs.view(env[1], "nope") is None
