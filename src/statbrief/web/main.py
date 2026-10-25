"""FastAPI application factory."""
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from statbrief.config import Settings, get_settings
from statbrief.web.deps import LoginRequired, get_runtime
from statbrief.web.runtime import RuntimeHolder


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; resources open lazily on first request (serverless-friendly)."""
    holder = RuntimeHolder(settings or get_settings())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Close the pool on shutdown (when the host runs ASGI lifespan)."""
        yield
        holder.close()

    app = FastAPI(title="StatBrief", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime_holder = holder
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        """Redirect to login; HTMX requests get an HX-Redirect header instead."""
        target = "/login?next=" + quote(request.url.path)
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.get("/healthz")
    def healthz(request: Request) -> JSONResponse:
        """Liveness + database check."""
        with get_runtime(request).pool.connection() as c:
            ok = c.execute("SELECT 1 AS one").fetchone()["one"] == 1
        return JSONResponse({"ok": True, "db": ok})

    from statbrief.web.routes import auth, diag, pages

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(diag.router)
    return app
