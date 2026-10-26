"""FastAPI application factory."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg_pool import PoolTimeout
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from statbrief.config import Settings, get_settings
from statbrief.web.deps import LoginRequired, get_runtime
from statbrief.web.runtime import RuntimeHolder

logger = logging.getLogger(__name__)


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
        """Liveness + database check; 503 when the database is unreachable; flags a misconfigured demo user."""
        demo_error = None
        try:
            rt = get_runtime(request)
            timeout_s = rt.settings.healthz_db_timeout_s
            with rt.pool.connection(timeout=timeout_s) as c, c.transaction():
                # Bound the query too, not just the pool wait; set_config(..., true) == SET LOCAL
                # but accepts a bound parameter, and resets when the transaction ends.
                c.execute("SELECT set_config('statement_timeout', %s, true)",
                          (str(max(1, round(timeout_s * 1000))),))
                ok = c.execute("SELECT 1 AS one").fetchone()["one"] == 1
            demo_error = rt.demo_error
        # psycopg.Error covers connection failures (and psycopg_pool errors, which subclass it);
        # PoolTimeout is listed explicitly for clarity; TimeoutError is the setup-lock timeout.
        except (psycopg.Error, PoolTimeout, TimeoutError) as exc:
            # Log only the exception class: messages can echo connection details.
            logger.warning("healthz database check failed: %s", type(exc).__name__)
            ok = False
        body = {"ok": ok, "db": ok}
        if demo_error:
            body["demo_user"] = "misconfigured"
        return JSONResponse(body, status_code=200 if ok else 503)

    from statbrief.web.routes import auth, diag, pages

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(diag.router)
    return app
