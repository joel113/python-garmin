"""Explicit dependencies shared by CLI commands, requests, and workers."""
from dataclasses import dataclass

from garmin_library.config import Settings
from garmin_library.database import connect


@dataclass(frozen=True)
class AppContext:
    settings: Settings

    def connect(self):
        """Return a new connection owned by the caller, never shared across threads."""
        return connect(self.settings.database_path)
