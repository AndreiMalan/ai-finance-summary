"""Per-app runtime (settings + DB pool) initialised lazily, once per serverless instance."""
import threading
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from statbrief.auth.users import upsert_demo_user
from statbrief.config import Settings
from statbrief.storage.db import bootstrap, create_pool


@dataclass
class Runtime:
    """Live resources shared by all requests handled by one app instance."""

    settings: Settings
    pool: ConnectionPool

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
                    if s.demo_user_email and s.demo_user_password:
                        upsert_demo_user(pool, s.demo_user_email, s.demo_user_password.get_secret_value(),
                                         s.password_min_length)
                    self._runtime = Runtime(settings=s, pool=pool)
        return self._runtime

    def close(self) -> None:
        """Close the runtime if it was opened."""
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
