"""Long-term memory (AC 43): a user's report defaults in a LangGraph store, namespace ("prefs", user_id).

Saved when a report is approved; read only to pre-fill the new-report form. Never read as data.
"""
from langgraph.store.base import BaseStore

from statbrief.domain.preferences import UserPreferences

PREFS_NAMESPACE = "prefs"
PREFS_KEY = "report_defaults"


class PreferencesRepo:
    """A user's report defaults in a LangGraph store."""

    def __init__(self, store: BaseStore) -> None:
        """Bind to a store."""
        self.store = store

    def save(self, user_id: str, prefs: UserPreferences) -> None:
        """Replace the user's defaults."""
        self.store.put((PREFS_NAMESPACE, user_id), PREFS_KEY, prefs.model_dump(mode="json"))

    def get(self, user_id: str) -> UserPreferences | None:
        """The user's defaults, or None (malformed stored values count as none)."""
        item = self.store.get((PREFS_NAMESPACE, user_id), PREFS_KEY)
        if item is None:
            return None
        try:
            return UserPreferences.model_validate(item.value)
        except ValueError:
            return None
