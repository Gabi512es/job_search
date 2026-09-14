"""Supabase implementation of the Store protocol.

Same interface as JsonStore, backed by Postgres. Both are kept: JsonStore for
local development and the test suite, this one for the deployed backend.

    store = SupabaseStore.from_env()          # reads SUPABASE_URL + a key
    store = SupabaseStore(url, key)           # or pass them explicitly

Schema and RLS policies: supabase/schema.sql.

Which key to use
----------------
The backend runs jobs *on behalf of* a user, outside any browser session, so it
uses the **service-role** key. That key bypasses RLS, which means the isolation
between users in this process depends on the `user_id` filter in every query
below, not on the database.

That is a deliberate trade-off and it has a sharp edge: a missing filter here
would leak one user's results to another. So every method filters explicitly,
`_guard` refuses a blank user id, and writes assert that each row belongs to
the user being written for. The RLS policies still matter - they are what
protects the frontend, which connects with the anon key and a user JWT.

Passing an anon key plus a user's access token works too (`SupabaseStore(url,
anon_key, access_token=jwt)`); then RLS is enforced by Postgres as well.
"""

from __future__ import annotations

import os
from typing import Any

from jobscout.profile import Verdict
from jobscout.store.base import Application, JobResult, RunRecord

# Supabase rejects very large request bodies; job_results rows carry a full
# cover letter, so inserts are chunked.
INSERT_CHUNK = 100


class SupabaseStoreError(RuntimeError):
    """A Supabase call failed, with the operation that caused it."""


def _require_client():
    """Import supabase lazily so the package is optional for local work."""
    try:
        from supabase import create_client  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SupabaseStoreError(
            "supabase-py is not installed. `pip install supabase` to use "
            "SupabaseStore; JsonStore needs no extra dependency."
        ) from exc
    from supabase import create_client
    return create_client


class SupabaseStore:
    """Store backed by Supabase Postgres."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        access_token: str | None = None,
        client: Any = None,
    ):
        if not client:
            if not url or not key:
                raise SupabaseStoreError(
                    "SupabaseStore needs a project URL and a key. Set "
                    "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, or pass them in."
                )
            create_client = _require_client()
            client = create_client(url, key)
            if access_token:
                # Anon key + user JWT: RLS is then enforced by Postgres too.
                client.postgrest.auth(access_token)
        self.client = client

    @classmethod
    def from_env(cls, access_token: str | None = None) -> SupabaseStore:
        """Build from environment variables. No credential is ever hardcoded.

        SUPABASE_URL               https://<ref>.supabase.co
        SUPABASE_SERVICE_ROLE_KEY  server-side key (bypasses RLS)
        SUPABASE_ANON_KEY          fallback, used with a user access token
        """
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
               or os.environ.get("SUPABASE_ANON_KEY", "").strip())
        return cls(url, key, access_token=access_token)

    # -- plumbing -----------------------------------------------------------

    @staticmethod
    def _guard(user_id: str) -> str:
        """Refuse a blank user id.

        With the service-role key an empty filter would match every row, so
        this is the difference between "no results" and "everyone's results".
        """
        if not user_id or not str(user_id).strip():
            raise SupabaseStoreError("user_id is required and cannot be blank")
        return str(user_id)

    def _table(self, name: str):
        return self.client.table(name)

    @staticmethod
    def _rows(response) -> list[dict]:
        data = getattr(response, "data", None)
        return data if isinstance(data, list) else []

    def _run(self, operation: str, query):
        try:
            return query.execute()
        except Exception as exc:
            raise SupabaseStoreError(f"{operation} failed: {exc}") from exc

    # -- seen_jobs ----------------------------------------------------------

    def seen_keys(self, user_id: str) -> set[str]:
        user_id = self._guard(user_id)
        response = self._run(
            "seen_keys",
            self._table("seen_jobs").select("job_key").eq("user_id", user_id),
        )
        return {r["job_key"] for r in self._rows(response) if r.get("job_key")}

    def mark_seen(self, user_id: str, keys: set[str]) -> None:
        user_id = self._guard(user_id)
        if not keys:
            return
        rows = [{"user_id": user_id, "job_key": k} for k in sorted(keys)]
        for chunk in _chunks(rows, INSERT_CHUNK):
            # Re-marking an already-seen job must not fail the run.
            self._run(
                "mark_seen",
                self._table("seen_jobs").upsert(
                    chunk, on_conflict="user_id,job_key", ignore_duplicates=True
                ),
            )

    # -- job_results --------------------------------------------------------

    def known_urls(self, user_id: str) -> set[str]:
        user_id = self._guard(user_id)
        response = self._run(
            "known_urls",
            self._table("job_results").select("url").eq("user_id", user_id),
        )
        return {r["url"] for r in self._rows(response) if r.get("url")}

    def save_results(self, user_id: str, results: list[JobResult]) -> int:
        """Insert results, skipping URLs this user already has.

        Ownership is checked for the whole batch before anything is written -
        the same ordering bug that let a foreign row through in JsonStore.
        """
        user_id = self._guard(user_id)
        foreign = sorted({r.user_id for r in results if r.user_id != user_id})
        if foreign:
            raise SupabaseStoreError(
                f"results belong to {foreign}, not {user_id!r}"
            )
        if not results:
            return 0

        known = self.known_urls(user_id)
        fresh, seen_in_batch = [], set()
        for result in results:
            if not result.url or result.url in known or result.url in seen_in_batch:
                continue
            seen_in_batch.add(result.url)
            fresh.append(_to_row(result))
        if not fresh:
            return 0

        inserted = 0
        for chunk in _chunks(fresh, INSERT_CHUNK):
            # upsert + ignore_duplicates leans on the unique (user_id, url)
            # constraint, so a concurrent run cannot create a duplicate even
            # though known_urls() was read a moment earlier.
            response = self._run(
                "save_results",
                self._table("job_results").upsert(
                    chunk, on_conflict="user_id,url", ignore_duplicates=True
                ),
            )
            inserted += len(self._rows(response))
        return inserted

    def get_results(
        self, user_id: str, verdicts: list[Verdict] | None = None
    ) -> list[JobResult]:
        user_id = self._guard(user_id)
        query = self._table("job_results").select("*").eq("user_id", user_id)
        if verdicts:
            query = query.in_("verdict", list(verdicts))
        response = self._run("get_results", query.order("score", desc=True))
        return [_from_row(r) for r in self._rows(response)]

    # -- runs ---------------------------------------------------------------

    def save_run(self, record: RunRecord) -> None:
        self._guard(record.user_id)
        payload = record.model_dump()
        payload = {k: (v or None) if k.endswith("_at") else v
                   for k, v in payload.items()}
        self._run(
            "save_run",
            self._table("runs").upsert(payload, on_conflict="run_id"),
        )

    def get_run(self, user_id: str, run_id: str) -> RunRecord | None:
        user_id = self._guard(user_id)
        response = self._run(
            "get_run",
            self._table("runs").select("*")
                .eq("user_id", user_id).eq("run_id", run_id).limit(1),
        )
        rows = self._rows(response)
        if not rows:
            return None
        row = {k: ("" if v is None else v) for k, v in rows[0].items()}
        return RunRecord.model_validate(
            {k: v for k, v in row.items() if k in RunRecord.model_fields}
        )

    # -- applications -------------------------------------------------------

    def get_applications(self, user_id: str) -> dict[str, Application]:
        """Keyed by job URL, like JsonStore.

        The table references job_results(id), so the URL comes from a join.
        Keeping the foreign key rather than a copied URL means an application
        cannot outlive the result it tracks.
        """
        user_id = self._guard(user_id)
        response = self._run(
            "get_applications",
            self._table("applications")
                .select("*, job_results!inner(url)")
                .eq("user_id", user_id),
        )
        out: dict[str, Application] = {}
        for row in self._rows(response):
            related = row.get("job_results") or {}
            url = related.get("url") if isinstance(related, dict) else None
            if not url:
                continue
            out[url] = Application(
                user_id=row["user_id"], job_url=url,
                status=row.get("status") or "",
                priority=row.get("priority") or "",
                target_salary=row.get("target_salary") or "",
                notes=row.get("notes") or "",
                updated_at=str(row.get("updated_at") or ""),
            )
        return out

    def save_application(self, application: Application) -> None:
        user_id = self._guard(application.user_id)
        response = self._run(
            "resolve job_result for application",
            self._table("job_results").select("id")
                .eq("user_id", user_id).eq("url", application.job_url).limit(1),
        )
        rows = self._rows(response)
        if not rows:
            raise SupabaseStoreError(
                f"no job_result for {application.job_url!r} and user {user_id!r}; "
                f"save the result before tracking an application on it"
            )
        self._run(
            "save_application",
            self._table("applications").upsert(
                {
                    "user_id": user_id,
                    "job_result_id": rows[0]["id"],
                    "status": application.status,
                    "priority": application.priority,
                    "target_salary": application.target_salary,
                    "notes": application.notes,
                },
                on_conflict="user_id,job_result_id",
            ),
        )


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------

# Columns the database owns; never sent on a write.
_DB_MANAGED = {"id", "created_at"}


def _to_row(result: JobResult) -> dict:
    row = result.model_dump()
    row["evaluated_at"] = row.get("evaluated_at") or None  # timestamptz, not ""
    return {k: v for k, v in row.items() if k not in _DB_MANAGED}


def _from_row(row: dict) -> JobResult:
    payload = {k: v for k, v in row.items() if k in JobResult.model_fields}
    for key in ("title", "company", "url", "source", "published", "location",
                "one_liner", "generated_text", "salary_range_market",
                "run_id", "job_key", "evaluated_at"):
        if payload.get(key) is None:
            payload[key] = ""
    for key in ("match_signals", "gaps", "red_flags"):
        payload[key] = payload.get(key) or []
    for key in ("breakdown", "flags", "extra"):
        payload[key] = payload.get(key) or {}
    for key in ("score", "base_score"):
        payload[key] = float(payload.get(key) or 0)
    return JobResult.model_validate(payload)


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]
