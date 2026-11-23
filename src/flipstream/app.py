"""Flipstream web app: paste form, review form, job page, MP3 download.

Server-rendered templates; the job page polls its status fragment with htmx.
Every page but /healthz needs a valid Cloudflare Access JWT.
"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import redis
import yt_dlp.version
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import jobs, youtube
from .auth import HEADER, AccessDenied, AccessVerifier
from .config import Settings

OPEN_PATHS = {"/healthz"}
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

Lookup = Callable[[str], youtube.VideoInfo]


def create_app(
    settings: Settings | None = None,
    verifier: AccessVerifier | None = None,
    conn: redis.Redis | None = None,
    lookup: Lookup = youtube.lookup,
) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Flipstream", version=settings.version, docs_url=None, redoc_url=None)
    conn = conn or redis.Redis.from_url(
        settings.redis_url, socket_timeout=5, socket_connect_timeout=2
    )
    TEMPLATES.env.globals["version"] = settings.version

    if settings.auth_mode == "off":
        verifier = None
    elif verifier is None and settings.access_aud and settings.team_domain:
        verifier = AccessVerifier(settings.team_domain, settings.access_aud)

    @app.middleware("http")
    async def require_access(request: Request, call_next):
        request.state.email = None
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        if settings.auth_mode != "off":
            if verifier is None:
                # Fail closed: Access settings missing means nobody gets in.
                return JSONResponse({"detail": "sign-in not configured"}, status_code=503)
            try:
                claims = verifier.verify(request.headers.get(HEADER))
            except AccessDenied:
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            request.state.email = claims.get("email")
        # A form posted from another site carries no Access cookie (SameSite=Lax),
        # but check Origin too: cheap, and it doesn't depend on cookie settings.
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-site form"}, status_code=403)
        return await call_next(request)

    def page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx |= {"email": request.state.email, "settings": settings}
        return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        try:
            redis_state = "ok" if conn.ping() else "down"
        except redis.RedisError:
            redis_state = "down"
        return {
            "status": "ok",
            "version": settings.version,
            "yt_dlp": yt_dlp.version.__version__,
            "redis": redis_state,
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, url: str = "") -> HTMLResponse:
        return page(request, "paste.html", url=url, error=None)

    @app.post("/review", response_class=HTMLResponse)
    def review(request: Request, url: str = Form("")) -> HTMLResponse:
        # Plain def: FastAPI runs it in a thread, so the yt-dlp lookup doesn't
        # block the event loop.
        try:
            info = lookup(url)
        except youtube.LookupFailed as exc:
            return page(request, "paste.html", status_code=422, url=url, error=str(exc))
        return page(request, "review.html", info=info, error=None)

    @app.post("/jobs")
    def create_job(
        request: Request,
        url: str = Form(...),
        title: str = Form(...),
        artist: str = Form(""),
        genre: str = Form(""),
        tags: str = Form(""),
        description: str = Form(""),
        thumbnail: str = Form(""),
        duration: int = Form(0),
    ) -> Response:
        info = youtube.VideoInfo(
            url=url,
            video_id="",
            title=title.strip(),
            artist=artist.strip(),
            duration=duration,
            description=description.strip(),
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            genre=genre.strip(),
            thumbnail=thumbnail if thumbnail.startswith("https://") else "",
        )
        try:
            youtube.check_url(url)
            if not info.title:
                raise youtube.LookupFailed("The title can't be empty: it's the file name.")
        except youtube.LookupFailed as exc:
            return page(request, "review.html", status_code=422, info=info, error=str(exc))
        req = jobs.JobRequest(
            url=url.strip(),
            title=info.title[:300],
            artist=info.artist[:200],
            genre=info.genre[:100],
            tags=", ".join(info.tags)[:500],
            description=info.description[:5000],
        )
        try:
            job = jobs.enqueue(conn, req)
        except redis.RedisError:
            msg = "The job queue (Redis) isn't answering. Your edits are still here; try again."
            return page(request, "review.html", status_code=503, info=info, error=msg)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    def find(job_id: str) -> jobs.JobView | None:
        try:
            return jobs.view(conn, job_id)
        except redis.RedisError:
            return None

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> HTMLResponse:
        job = find(job_id)
        if job is None:
            return page(request, "missing.html", status_code=404)
        return page(request, "job.html", job=job)

    @app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
    def job_status(request: Request, job_id: str) -> HTMLResponse:
        job = find(job_id)
        if job is None:
            return page(request, "missing.html", status_code=404)
        resp = page(request, "_status.html", job=job)
        if job.finished:
            # htmx stops polling on 286.
            resp.status_code = 286
        return resp

    @app.get("/jobs/{job_id}/mp3")
    def download(job_id: str) -> Response:
        job = find(job_id)
        if job is None or job.state != "done" or not job.filename:
            return JSONResponse({"detail": "no MP3 for that job"}, status_code=404)
        out_dir = settings.output_dir.resolve()
        path = (out_dir / job.filename).resolve()
        if path.parent != out_dir or not path.is_file():
            return JSONResponse({"detail": "the MP3 is no longer on the share"}, status_code=404)
        return FileResponse(path, media_type="audio/mpeg", filename=job.filename)

    return app


app = create_app()
