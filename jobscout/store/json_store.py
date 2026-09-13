"""Local JSON implementation of Store, for development and tests.

One directory per user, so the layout mirrors the per-user isolation Supabase
will enforce with row-level security, and nothing can leak between users by
accident during development.

    <base_dir>/<user_id>/seen_jobs.json
                        /job_results.json
                        /runs.json
                        /applications.json

Not built for concurrency: a real deployment uses SupabaseStore. This exists so
the pipeline can be exercised end to end before any database is wired up.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jobscout.profile import Verdict
from jobscout.store.base import Application, JobResult, RunRecord


class JsonStore:
    """Store backed by JSON files under one directory."""

    def __init__(self, base_dir: Path | str = "store_data"):
        self.base_dir = Path(base_dir)

    # -- plumbing -----------------------------------------------------------

    def _dir(self, user_id: str) -> Path:
        # user_id becomes a directory name, so refuse anything path-like
        # rather than letting "../" escape the store.
        if not user_id or "/" in user_id or "\\" in user_id or user_id.startswith("."):
            raise ValueError(f"invalid user_id for a directory name: {user_id!r}")
        path = self.base_dir / user_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _read(self, user_id: str, name: str, default):
        path = self._dir(user_id) / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, user_id: str, name: str, payload) -> None:
        path = self._dir(user_id) / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- seen_jobs ----------------------------------------------------------

    def seen_keys(self, user_id: str) -> set[str]:
        return set(self._read(user_id, "seen_jobs.json", []))

    def mark_seen(self, user_id: str, keys: set[str]) -> None:
        merged = self.seen_keys(user_id) | set(keys)
        self._write(user_id, "seen_jobs.json", sorted(merged))

    # -- job_results --------------------------------------------------------

    def known_urls(self, user_id: str) -> set[str]:
        return {r["url"] for r in self._read(user_id, "job_results.json", []) if r.get("url")}

    def save_results(self, user_id: str, results: list[JobResult]) -> int:
        # Ownership is checked for every result BEFORE anything is written or
        # skipped. Checking it inside the loop after the duplicate test let a
        # foreign row through unnoticed whenever its URL happened to be one
        # this user already had.
        foreign = [r.user_id for r in results if r.user_id != user_id]
        if foreign:
            raise ValueError(
                f"results belong to {sorted(set(foreign))}, not {user_id!r}"
            )

        rows = self._read(user_id, "job_results.json", [])
        existing = {r["url"] for r in rows if r.get("url")}

        added = 0
        for result in results:
            # Enforces the unique (user_id, url) constraint from § 5.3 here, so
            # the JSON store and Supabase behave the same way.
            if result.url and result.url in existing:
                continue
            rows.append(result.model_dump())
            existing.add(result.url)
            added += 1

        self._write(user_id, "job_results.json", rows)
        return added

    def get_results(
        self, user_id: str, verdicts: list[Verdict] | None = None
    ) -> list[JobResult]:
        rows = [JobResult.model_validate(r)
                for r in self._read(user_id, "job_results.json", [])]
        if verdicts:
            rows = [r for r in rows if r.verdict in verdicts]
        return rows

    # -- runs ---------------------------------------------------------------

    def save_run(self, record: RunRecord) -> None:
        rows = self._read(record.user_id, "runs.json", [])
        rows = [r for r in rows if r.get("run_id") != record.run_id]
        rows.append(record.model_dump())
        self._write(record.user_id, "runs.json", rows)

    def get_run(self, user_id: str, run_id: str) -> RunRecord | None:
        for row in self._read(user_id, "runs.json", []):
            if row.get("run_id") == run_id:
                return RunRecord.model_validate(row)
        return None

    # -- applications -------------------------------------------------------

    def get_applications(self, user_id: str) -> dict[str, Application]:
        return {
            row["job_url"]: Application.model_validate(row)
            for row in self._read(user_id, "applications.json", [])
        }

    def save_application(self, application: Application) -> None:
        rows = self._read(application.user_id, "applications.json", [])
        rows = [r for r in rows if r.get("job_url") != application.job_url]
        application = application.model_copy(
            update={"updated_at": application.updated_at or datetime.now().isoformat()}
        )
        rows.append(application.model_dump())
        self._write(application.user_id, "applications.json", rows)
