"""Immutable settings and explicit environment loading at CLI startup."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
import os

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path, environ=None) -> None:
    """Load simple KEY=VALUE entries without overriding the supplied environment."""
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and name not in environ:
            environ[name] = value


@dataclass(frozen=True)
class Settings:
    root: Path = PROJECT_ROOT
    database_path: Path = PROJECT_ROOT / "activities.sqlite3"
    activities_path: Path = PROJECT_ROOT / "storage" / "activities"
    allowed_hosts: str = ""
    api_key: str | None = field(default=None, repr=False)

    @property
    def web_path(self) -> Path:
        return self.root / "web"

    @property
    def routes_path(self) -> Path:
        return self.database_path.parent / "routes"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *, root: Path = PROJECT_ROOT):
        environ = os.environ if environ is None else environ
        root = root.expanduser().resolve()

        def configured_path(name, default):
            value = Path(environ.get(name) or default).expanduser()
            return value if value.is_absolute() else root / value

        return cls(root=root,
                   database_path=configured_path("GARMIN_DB_PATH", root / "activities.sqlite3"),
                   activities_path=configured_path("GARMIN_ACTIVITIES_PATH", root / "storage/activities"),
                   allowed_hosts=environ.get("GARMIN_ALLOWED_HOSTS", ""),
                   api_key=environ.get("GARMIN_API_KEY"))
