"""Per-app runtime (settings + DB pool) initialised lazily, once per serverless instance."""
import logging
import threading
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from statbrief.auth.users import DemoEmailConflict, InvalidEmail, WeakPassword, upsert_demo_user
from statbrief.config import Settings
from statbrief.storage.db import bootstrap, create_pool

logger = logging.getLogger(__name__)

# Misconfigured DEMO_USER_* values: the app keeps serving without the demo account (see D-022).
_DEMO_CONFIG_ERRORS = (InvalidEmail, WeakPassword, DemoEmailConflict)


@dataclass
class Runtime:
    """Live resources shared by all requests handled by one app instance."""

    settings: Settings
    pool: ConnectionPool
    demo_error: str | None = None  # user-safe reason the demo account could not be seeded

    def close(self) -> None:
        """Release the connection pool."""
        self.pool.close()


class RuntimeHolder:
    """Thread-safe lazy holder so cold starts initialise exactly once."""

    def __init__(self, settings: Settings):
        """Remember settings; nothing is opened until `get()`."""
        self._settings = settings
        self._runtime: Runtime | None = None
        self._lock = threading.Lock()

    def get(self) -> Runtime:
        """Open the pool, migrate, set up LangGraph tables and seed the demo user on first call."""
        if self._runtime is None:
            with self._lock:
                if self._runtime is None:
                    bootstrap(self._settings)
                    pool = create_pool(self._settings)
                    s = self._settings
                    demo_error = None
                    try:
                        if s.demo_user_email and s.demo_user_password:
                            upsert_demo_user(pool, s.demo_user_email, s.demo_user_password.get_secret_value(),
                                             s.password_min_length)
                    except _DEMO_CONFIG_ERRORS as exc:
                        # A bad demo setting must not take the whole site down: log, report via
                        # /healthz, and continue without the demo account.
                        demo_error = str(exc)
                        logger.error("Demo user not seeded (check DEMO_USER_EMAIL/DEMO_USER_PASSWORD): %s",
                                     demo_error)
                    except BaseException:
                        # Infrastructure failure: never leak the pool on retries.
                        pool.close()
                        raise
                    self._runtime = Runtime(settings=s, pool=pool, demo_error=demo_error)
        return self._runtime

    def close(self) -> None:
        """Close the runtime if it was opened."""
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
