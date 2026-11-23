"""Paste -> review -> job -> download, with a fake lookup and fakeredis."""

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from flipstream import jobs, youtube
from flipstream.app import create_app
from flipstream.config import Settings

URL = "https://www.youtube.com/watch?v=abc"
INFO = youtube.VideoInfo(
    url=URL,
    video_id="abc",
    title="Sunset House Mix",
    artist="DJ K",
    duration=7471,
    description="Two hours.",
    tags=["house", "sunset"],
    genre="House",
    thumbnail="https://i.ytimg.com/vi/abc/maxresdefault.jpg",
)


def fake_lookup(url):
    if "missing" in url:
        raise youtube.LookupFailed("[youtube] missing: Video unavailable")
    return INFO


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        version="sha-test",
        access_aud="",
        team_domain="",
        auth_mode="off",
        share_root=tmp_path / "share",
        scratch_dir=tmp_path / "scratch",
    )
    settings.output_dir.mkdir(parents=True)
    conn = fakeredis.FakeStrictRedis()
    client = TestClient(create_app(settings, conn=conn, lookup=fake_lookup))
    return client, conn, settings


FORM = {
    "url": URL,
    "title": "Sunset House Mix",
    "artist": "DJ K",
    "genre": "House",
    "tags": "house, sunset",
    "description": "Two hours.",
    "thumbnail": INFO.thumbnail,
    "duration": "7471",
}


def test_paste_page(setup):
    r = setup[0].get("/")
    assert r.status_code == 200 and 'name="url"' in r.text


def test_review_prefills_the_form(setup):
    r = setup[0].post("/review", data={"url": URL})
    assert r.status_code == 200
    for text in (
        'value="Sunset House Mix"',
        'value="DJ K"',
        'value="House"',
        'value="house, sunset"',
        "Two hours.",
        "maxresdefault.jpg",
        "2:04:31",
        "v1.1 · needs SoundCloud Artist Pro",
    ):
        assert text in r.text


def test_review_shows_yt_dlp_error(setup):
    r = setup[0].post("/review", data={"url": URL + "missing"})
    assert r.status_code == 422
    assert "Video unavailable" in r.text


def test_submit_queues_a_job_and_shows_it(setup):
    client, conn, _ = setup
    r = client.post("/jobs", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    job_id = r.headers["location"].rsplit("/", 1)[1]
    job = jobs.queue(conn).fetch_job(job_id)
    assert job.args[0]["title"] == "Sunset House Mix"
    assert job.args[0]["tags"] == "house, sunset"
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200 and "every 2s" in page.text and "queued" in page.text
    assert client.get(f"/jobs/{job_id}/status").status_code == 200


def test_empty_title_or_foreign_url_refused(setup):
    client = setup[0]
    assert client.post("/jobs", data=FORM | {"title": "  "}).status_code == 422
    assert client.post("/jobs", data=FORM | {"url": "https://vimeo.com/1"}).status_code == 422


def test_redis_down_keeps_the_form(setup, monkeypatch):
    def down(*a, **k):
        raise redis.ConnectionError("refused")

    monkeypatch.setattr(jobs, "enqueue", down)
    r = setup[0].post("/jobs", data=FORM | {"title": "My edited title"})
    assert r.status_code == 503
    assert "(Redis) isn&#39;t answering" in r.text and 'value="My edited title"' in r.text


def test_cross_site_post_refused(setup):
    r = setup[0].post("/jobs", data=FORM, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def finish(conn, job_id, filename):
    job = jobs.queue(conn).fetch_job(job_id)
    job.meta.update({"state": "done", "filename": filename, "size_mb": 1.0})
    job.save_meta()
    return job


def test_done_job_stops_polling_and_downloads(setup):
    client, conn, settings = setup
    job_id = client.post("/jobs", data=FORM, follow_redirects=False).headers["location"][6:]
    (settings.output_dir / "Sunset House Mix.mp3").write_bytes(b"ID3data")
    finish(conn, job_id, "Sunset House Mix.mp3")

    status = client.get(f"/jobs/{job_id}/status")
    assert status.status_code == 286  # htmx: stop polling
    assert "Download MP3" in status.text
    page = client.get(f"/jobs/{job_id}")
    assert "every 2s" not in page.text

    mp3 = client.get(f"/jobs/{job_id}/mp3")
    assert mp3.status_code == 200 and mp3.content == b"ID3data"
    assert mp3.headers["content-type"] == "audio/mpeg"
    assert "attachment" in mp3.headers["content-disposition"]


def test_download_cannot_leave_the_share_folder(setup):
    client, conn, settings = setup
    job_id = client.post("/jobs", data=FORM, follow_redirects=False).headers["location"][6:]
    (settings.share_root / "secret.txt").write_text("x")
    finish(conn, job_id, "../secret.txt")
    assert client.get(f"/jobs/{job_id}/mp3").status_code == 404


def test_unknown_job_is_404(setup):
    assert setup[0].get("/jobs/does-not-exist").status_code == 404
    assert setup[0].get("/jobs/does-not-exist/mp3").status_code == 404
