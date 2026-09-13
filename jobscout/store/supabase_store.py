"""Supabase implementation of Store — not implemented yet, by design.

Left as a shell so the shape of the target is visible while the pipeline is
still being built against JsonStore. Wiring it up is its own step: it needs a
project, the SQL for the six tables in ARCHITECTURE.md 5.3, and the row-level
security policies that make `user_id` actually mean something.

Every method raises rather than silently doing nothing, so this cannot be
selected by accident and appear to work.
"""

from __future__ import annotations

from jobscout.profile import Verdict
from jobscout.store.base import Application, JobResult, RunRecord

# Tables this store will use. Column lists live in ARCHITECTURE.md 5.3.
TABLES = ("profiles", "runs", "seen_jobs", "job_results", "applications", "raw_dumps")

_NOT_READY = (
    "SupabaseStore is not implemented yet. Use JsonStore for local development. "
    "Implementing it needs: the SQL for {tables}, RLS policies keyed on user_id, "
    "and a service-role client held server-side."
)


class SupabaseStore:
    """Placeholder with the same surface as JsonStore."""

    def __init__(self, url: str = "", service_key: str = ""):
        self.url = url
        self.service_key = service_key

    def _unimplemented(self) -> None:
        raise NotImplementedError(_NOT_READY.format(tables=", ".join(TABLES)))

    def seen_keys(self, user_id: str) -> set[str]:
        self._unimplemented()

    def mark_seen(self, user_id: str, keys: set[str]) -> None:
        self._unimplemented()

    def known_urls(self, user_id: str) -> set[str]:
        self._unimplemented()

    def save_results(self, user_id: str, results: list[JobResult]) -> int:
        self._unimplemented()

    def get_results(
        self, user_id: str, verdicts: list[Verdict] | None = None
    ) -> list[JobResult]:
        self._unimplemented()

    def save_run(self, record: RunRecord) -> None:
        self._unimplemented()

    def get_run(self, user_id: str, run_id: str) -> RunRecord | None:
        self._unimplemented()

    def get_applications(self, user_id: str) -> dict[str, Application]:
        self._unimplemented()

    def save_application(self, application: Application) -> None:
        self._unimplemented()
