"""yt2sc web app. DEV-43: health page behind Cloudflare Access."""

from html import escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .auth import HEADER, AccessDenied, AccessVerifier
from .config import Settings

OPEN_PATHS = {"/healthz"}
VICE_NIGHT = "https://cdn.jsdelivr.net/gh/KFlipDev/design-system@v1.3.1/vice-night.css"


def create_app(settings: Settings | None = None, verifier: AccessVerifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="yt2sc", version=settings.version, docs_url=None, redoc_url=None)

    if settings.auth_mode == "off":
        verifier = None
    elif verifier is None and settings.access_aud and settings.team_domain:
        verifier = AccessVerifier(settings.team_domain, settings.access_aud)

    @app.middleware("http")
    async def require_access(request: Request, call_next):
        if request.url.path in OPEN_PATHS or settings.auth_mode == "off":
            request.state.email = None
            return await call_next(request)
        if verifier is None:
            # Fail closed: Access settings missing means nobody gets in.
            return JSONResponse({"detail": "sign-in not configured"}, status_code=503)
        try:
            claims = verifier.verify(request.headers.get(HEADER))
        except AccessDenied:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        request.state.email = claims.get("email")
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": settings.version}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> str:
        who = escape(request.state.email or "local development")
        version = escape(settings.version)
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt2sc</title>
<link rel="stylesheet" href="{VICE_NIGHT}">
<style>
  body {{ margin:0; padding:0 16px 48px; background:var(--bg); color:var(--ink);
         font-family:var(--f-body); }}
  body::before {{ content:""; display:block; height:6px; margin:0 -16px 48px;
                 background:var(--sunset); }}
  main {{ max-width:560px; margin:0 auto; background:var(--depth-3); border:1px solid var(--line);
         border-radius:var(--r); padding:24px; }}
  .mark {{ font-family:var(--f-mark); letter-spacing:.2em; text-transform:uppercase;
          color:var(--muted); font-size:12px; margin:0; }}
  h1 {{ font-family:var(--f-head); margin:4px 0 20px; font-size:34px; }}
  h1 span {{ color:var(--pink); }}
  dt {{ font-weight:600; color:var(--teal); }} dd {{ margin:0 0 12px; }}
  code {{ font-family:var(--f-mono); }}
</style></head>
<body><main>
<p class="mark">yt2sc</p>
<h1>Up and <span>running</span></h1>
<dl>
  <dt>Signed in as</dt><dd>{who}</dd>
  <dt>Version</dt><dd><code>{version}</code></dd>
</dl>
</main></body></html>"""

    return app


app = create_app()
