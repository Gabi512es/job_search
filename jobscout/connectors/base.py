"""The common connector interface, plus raw-dump storage.

Every source implements the same two methods (ARCHITECTURE.md § 3):

    plan(cfg)           -> list[ConnectorPlan]   no network, no spend
    fetch(cfg, secrets) -> list[JobPosting]      the real work

Free sources return `[]` from plan(). A connector never talks to the cost
guard: the pipeline collects every plan, asks the guard once, and only then
calls fetch(). A connector therefore cannot spend money without having been
declared first.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from jobscout.jobs import JobPosting
from jobscout.profile import SourceConfig

from cost_guard import ConnectorPlan


def _find_env_file() -> Path | None:
    """Nearest .env walking up from this file, so it is found whether the
    caller runs from the repo root or from a notebook in a subdirectory."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".env"
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class Secrets:
    """Server-side credentials. Not per-user: these are the operator's keys,
    which is exactly why a per-user cost cap exists (ARCHITECTURE.md § 5.1)."""

    anthropic_api_key: str = ""
    apify_token: str = ""

    @classmethod
    def from_env(cls, env_file: Path | str | None = None) -> Secrets:
        """Read credentials from the environment, falling back to a .env file.

        The environment wins, so a deployment sets real variables and nothing
        looks for a file. The fallback exists because the notebooks and the
        local scripts all keep their keys in .env, and reading it here means
        that logic is not repeated in each of them.
        """
        values = dict(os.environ)
        path = Path(env_file) if env_file else _find_env_file()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values.setdefault(key.strip(), value.strip())

        return cls(
            anthropic_api_key=values.get("ANTHROPIC_API_KEY", "").strip(),
            apify_token=values.get("APIFY_TOKEN", "").strip(),
        )


class SourceConnector(Protocol):
    """Two methods, no inheritance. Adding a source is adding one file."""

    type: str

    def plan(self, cfg: SourceConfig) -> list[ConnectorPlan]:
        """What this connector intends to do, priced without doing it.

        Returns [] when the run costs nothing: free sources, and any Apify
        connector replaying a saved dump.
        """
        ...

    def fetch(self, cfg: SourceConfig, secrets: Secrets) -> list[JobPosting]:
        """Execute. Only called after the cost guard returned OK."""
        ...


# ---------------------------------------------------------------------------
# Raw dumps — generalised from the InfoJobs-only version
# ---------------------------------------------------------------------------
# Saving the raw dataset *before* mapping and filtering means a mapping bug can
# be fixed and replayed for free, instead of re-paying Apify. This was already
# the pattern in the Camila notebook; here every Apify connector gets it.


class DumpStore:
    """Saves and replays raw connector payloads under one directory.

    The directory is passed in rather than read from a module constant, so two
    users' dumps cannot collide and nothing depends on the current working
    directory.
    """

    def __init__(self, base_dir: Path | str = "dumps"):
        self.base_dir = Path(base_dir)

    def path_for(self, connector: str, profile_id: str) -> Path:
        return self.base_dir / f"{connector}__{profile_id}.json"

    def save(
        self,
        connector: str,
        profile_id: str,
        items: list[dict],
        meta: dict | None = None,
    ) -> Path:
        path = self.path_for(connector, profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "connector": connector,
            "profile_id": profile_id,
            "saved_at": datetime.now().isoformat(),
            "items_count": len(items),
            **(meta or {}),
            "items": items,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def load(self, connector: str, profile_id: str) -> tuple[list[dict], dict]:
        """Return (items, metadata). Raises if the dump does not exist.

        Failing loudly matters: silently returning [] would look like "the
        source found nothing today" rather than "you asked to replay a dump
        that was never saved".
        """
        path = self.path_for(connector, profile_id)
        if not path.exists():
            raise FileNotFoundError(
                f"reuse_dump is set for {connector!r} but no dump exists at {path}. "
                f"Run once with reuse_dump=false to create it."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.pop("items", [])
        return items, payload

    def exists(self, connector: str, profile_id: str) -> bool:
        return self.path_for(connector, profile_id).exists()


# Browser-ish headers shared by the HTTP-scraping connectors.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
