"""Flipstream web app: paste form, review form, job page, MP3 download.

Server-rendered templates; the job page polls its status fragment with htmx.
Every page but /healthz needs a valid Cloudflare Access JWT.

How to read this file: create_app() at the bottom builds the app. It stores the
settings, the Redis connection and the other shared objects on app.state. The
access check and the routes above it read them back from request.app.state.
"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import redis
import yt_dlp.version
from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import jobs, youtube
from .auth import HEADER, AccessDenied, AccessVerifier
from .config import Settings

OPEN_PATHS = {"/healthz"}
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
# htmx stops polling a page fragment when it gets this status code.
HTMX_STOP_POLLING = 286

Lookup = Callable[[str], youtube.VideoInfo]

# The routes below register on this router; create_app() adds it to the app.
router = APIRouter()


# --- access check -------------------------------------------------------------


async def require_access(request: Request, call_next):
    """Check every request before its route runs (FastAPI calls this "middleware").

    Refuses a request without a valid Access token and a form posted from another
    site. call_next hands the request on to the route.
    """
    settings: Settings = request.app.state.settings
    verifier: AccessVerifier | None = request.app.state.verifier
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
    if is_cross_site_post(request):
        return JSONResponse({"detail": "cross-site form"}, status_code=403)
    return await call_next(request)


def is_cross_site_post(request: Request) -> bool:
    """Say whether a form was posted from a page on another site.

    A form posted from another site carries no Access cookie (SameSite=Lax),
    but check Origin too: cheap, and it doesn't depend on cookie settings.
    """
    if request.method != "POST":
        return False
    origin = request.headers.get("origin")
    return bool(origin) and urlsplit(origin).netloc != request.headers.get("host")


# --- helpers the routes share ---------------------------------------------------


def render_page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    """Render one template. Every page also gets the signed-in email and the settings."""
    context |= {"email": request.state.email, "settings": request.app.state.settings}
    return TEMPLATES.TemplateResponse(request, name, context, status_code=status_code)


def find_job(request: Request, job_id: str) -> jobs.JobView | None:
    """Return the job, or None if it doesn't exist or Redis isn't answering."""
    try:
        return jobs.view(request.app.state.conn, job_id)
    except redis.RedisError:
        return None


# --- routes -------------------------------------------------------------------


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    """Report the version and whether Redis answers. Open without sign-in, for monitoring."""
    try:
        redis_state = "ok" if request.app.state.conn.ping() else "down"
    except redis.RedisError:
        redis_state = "down"
    return {
        "status": "ok",
        "version": request.app.state.settings.version,
        "yt_dlp": yt_dlp.version.__version__,
        "redis": redis_state,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, url: str = "") -> HTMLResponse:
    return render_page(request, "paste.html", url=url, error=None)


@router.post("/review", response_class=HTMLResponse)
def review(request: Request, url: str = Form("")) -> HTMLResponse:
    """Look the video up and show its details in a form Kevin can edit."""
    # Plain def: FastAPI runs it in a thread, so the yt-dlp lookup doesn't
    # block the event loop.
    lookup: Lookup = request.app.state.lookup
    try:
        info = lookup(url)
    except youtube.LookupFailed as exc:
        return render_page(request, "paste.html", status_code=422, url=url, error=str(exc))
    return render_page(request, "review.html", info=info, error=None)


@router.post("/jobs")
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
    """Queue a job from the review form, then send the browser to the job page.

    Each parameter is one form field. On an error the form is shown again with
    the edits still in it.
    """
    info = youtube.VideoInfo(
        url=url,
        video_id="",
        title=title.strip(),
        artist=artist.strip(),
        duration=duration,
        description=description.strip(),
        tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
        genre=genre.strip(),
        thumbnail=thumbnail if thumbnail.startswith("https://") else "",
    )
    try:
        youtube.check_url(url)
        if not info.title:
            raise youtube.LookupFailed("The title can't be empty: it's the file name.")
    except youtube.LookupFailed as exc:
        return render_page(request, "review.html", status_code=422, info=info, error=str(exc))
    job_request = jobs.JobRequest(
        url=url.strip(),
        title=info.title[:300],
        artist=info.artist[:200],
        genre=info.genre[:100],
        tags=", ".join(info.tags)[:500],
        description=info.description[:5000],
    )
    try:
        job = jobs.enqueue(request.app.state.conn, job_request)
    except redis.RedisError:
        msg = "The job queue (Redis) isn't answering. Your edits are still here; try again."
        return render_page(request, "review.html", status_code=503, info=info, error=msg)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str) -> HTMLResponse:
    job = find_job(request, job_id)
    if job is None:
        return render_page(request, "missing.html", status_code=404)
    return render_page(request, "job.html", job=job)


@router.get("/jobs/{job_id}/status", response_class=HTMLResponse)
def job_status(request: Request, job_id: str) -> HTMLResponse:
    """Return the status fragment the job page polls while the job runs."""
    job = find_job(request, job_id)
    if job is None:
        return render_page(request, "missing.html", status_code=404)
    response = render_page(request, "_status.html", job=job)
    if job.finished:
        response.status_code = HTMX_STOP_POLLING
    return response


@router.get("/jobs/{job_id}/mp3")
def download(request: Request, job_id: str) -> Response:
    """Send the finished MP3 from the share."""
    job = find_job(request, job_id)
    if job is None or job.state != "done" or not job.filename:
        return JSONResponse({"detail": "no MP3 for that job"}, status_code=404)
    out_dir = request.app.state.settings.output_dir.resolve()
    path = (out_dir / job.filename).resolve()
    # The parent check stops a file name that points outside the output folder.
    if path.parent != out_dir or not path.is_file():
        return JSONResponse({"detail": "the MP3 is no longer on the share"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg", filename=job.filename)


# --- app setup ----------------------------------------------------------------


def choose_verifier(settings: Settings, verifier: AccessVerifier | None) -> AccessVerifier | None:
    """Return the verifier to use: none with auth off, else the given one or a new one.

    With Access settings missing this returns None, and require_access then
    refuses every request.
    """
    if settings.auth_mode == "off":
        return None
    if verifier is None and settings.access_aud and settings.team_domain:
        return AccessVerifier(settings.team_domain, settings.access_aud)
    return verifier


def create_app(
    settings: Settings | None = None,
    verifier: AccessVerifier | None = None,
    conn: redis.Redis | None = None,
    lookup: Lookup = youtube.lookup,
) -> FastAPI:
    """Build the app. Each argument has a real default; the tests pass in fakes."""
    settings = settings or Settings.from_env()
    app = FastAPI(title="Flipstream", version=settings.version, docs_url=None, redoc_url=None)
    TEMPLATES.env.globals["version"] = settings.version

    # Shared objects. The access check and the routes read them from request.app.state.
    app.state.settings = settings
    app.state.verifier = choose_verifier(settings, verifier)
    app.state.conn = conn or redis.Redis.from_url(
        settings.redis_url, socket_timeout=5, socket_connect_timeout=2
    )
    app.state.lookup = lookup

    app.middleware("http")(require_access)
    app.include_router(router)
    return app


app = create_app()
