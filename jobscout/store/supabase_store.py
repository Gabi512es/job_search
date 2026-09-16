"""Store backed by Lovable's engine routes, over HTTP.

Lovable Cloud owns the Supabase instance and never hands out the service-role
key, so the engine cannot talk to Postgres directly. It calls five server
routes instead. There is no database connection anywhere in this file.

    store = LovableEngineStore.from_env()

    ENGINE_API_KEY            sent as X-Engine-Key on every request
    LOVABLE_ENGINE_BASE_URL   defaults to the production project URL

Eight routes are wired: the original five plus get-run, get-job-results and
get-known-urls.
They cover eight of the nine Store methods; the rest are handled as follows,
and the reasoning matters more than the code:

    get_applications  returns empty. Only the Excel Tracker reads it, which
                      then shows profile defaults. Returning [] would look like "this user has no
                      offers", which is worse than an error.
    save_application  RAISES. Silently dropping what a user typed is the one
                      failure this design exists to prevent.

MissingRouteError names the route Lovable would need to add, so the gap is
actionable rather than mysterious.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from jobscout.profile import Verdict
from jobscout.store.base import Application, JobResult, RunRecord

# Production stays the default on purpose: a default pointing at preview would
# become a trap the day production is published. While production has no build,
# LOVABLE_ENGINE_BASE_URL must be set to DEV_BASE_URL explicitly, which keeps
# the temporary choice visible in the configuration instead of hidden here.
DEFAULT_BASE_URL = (
    "https://project--932434d1-f765-4be8-a763-24175ab20d98.lovable.app"
)
DEV_BASE_URL = (
    "https://project--932434d1-f765-4be8-a763-24175ab20d98-dev.lovable.app"
)

# A job_results row carries a full cover letter (~3.5 kB), so 250 rows would be
# a megabyte in one body. Chunked to keep each request small.
RESULTS_CHUNK = 50

# The most get-job-results will return. MEASURED 2026-09-16: the route rejects
# anything above 500 with {"error": "limit must be a number between 1 and 500"},
# and offset/cursor/page/skip are all silently ignored, so there is no way to
# page past it.
#
# This now only constrains get_results(), which feeds a display. Deduplication
# moved to the dedicated get-known-urls route, which pages internally - that
# cap would otherwise have become a hard ceiling after two full runs.
RESULTS_LIMIT = 500

REQUEST_TIMEOUT = 30.0
# Retries only on transient failures, and only because every write route is an
# upsert: replaying one cannot duplicate anything.
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 1.5


class EngineStoreError(RuntimeError):
    """A call to a Lovable engine route failed."""


class MissingRouteError(EngineStoreError):
    """The operation needs a route Lovable has not exposed yet."""


def _clean(value: Any) -> Any:
    """Empty timestamp strings must travel as null, not ''."""
    return None if value == "" else value


class LovableEngineStore:
    """Implements the Store protocol against Lovable's five engine routes."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        *,
        client: Any = None,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = client            # injectable for tests
        self._warned_known_urls = False

        if client is None and not self.api_key:
            raise EngineStoreError(
                "ENGINE_API_KEY is not set. The Lovable engine routes reject "
                "every request without the X-Engine-Key header."
            )

    @classmethod
    def from_env(cls) -> LovableEngineStore:
        return cls(
            base_url=os.environ.get("LOVABLE_ENGINE_BASE_URL", "").strip(),
            api_key=os.environ.get("ENGINE_API_KEY", "").strip(),
        )

    # -- transport ----------------------------------------------------------

    @staticmethod
    def _guard(user_id: str) -> str:
        if not user_id or not str(user_id).strip():
            raise EngineStoreError("user_id is required and cannot be blank")
        return str(user_id)

    def _post(self, route: str, payload: dict,
              none_on_404: bool = False) -> dict | None:
        """POST one engine route and return its JSON body.

        Every failure mode becomes an EngineStoreError naming the route, so a
        caller never sees a bare httpx exception with no context.

        `none_on_404` is for routes where "not found" is a legitimate answer
        rather than a failure - get-run on an id that does not exist. It only
        applies when the body is JSON: a 404 carrying HTML means the request
        fell through to the web app, so the route itself is missing, and that
        must still raise.
        """
        url = f"{self.base_url}/api/public/engine/{route}"
        headers = {"X-Engine-Key": self.api_key, "Content-Type": "application/json"}
        last: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if self._client is not None:
                    response = self._client.post(url, json=payload, headers=headers,
                                                 timeout=self.timeout)
                else:
                    response = httpx.post(url, json=payload, headers=headers,
                                          timeout=self.timeout)
            except httpx.TimeoutException as exc:
                last = exc
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise EngineStoreError(
                    f"{route}: timed out after {self.timeout}s and "
                    f"{MAX_ATTEMPTS} attempts"
                ) from exc
            except httpx.RequestError as exc:
                last = exc
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise EngineStoreError(
                    f"{route}: could not reach {self.base_url} "
                    f"({type(exc).__name__}: {exc})"
                ) from exc

            status = response.status_code

            if status == 401:
                raise EngineStoreError(
                    f"{route}: 401, the X-Engine-Key header was rejected. "
                    f"Check ENGINE_API_KEY matches the value Lovable expects."
                )
            if status == 404:
                # MEASURED 2026-09-15: an unknown user answers with JSON
                # ({"error": "Unknown user_id"} / {"error": "Profile not
                # found"}); a route that does not exist answers with the app's
                # HTML. The status is identical, the body is not.
                text = _body_text(response)
                looks_like_html = text.lstrip().startswith("<")
                if none_on_404 and not looks_like_html:
                    return None
                cause = ("this route does not exist at "
                         f"{self.base_url} (the server returned HTML, which "
                         f"means the request fell through to the web app)"
                         if looks_like_html
                         else "the user or record is unknown to Lovable")
                raise EngineStoreError(f"{route}: 404, {cause}. Body: {text}")
            if status == 400:
                raise EngineStoreError(
                    f"{route}: 400, the payload was rejected. "
                    f"Body: {_body_text(response)}"
                )
            if status >= 500:
                last = EngineStoreError(f"{route}: {status}")
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise EngineStoreError(
                    f"{route}: server error {status} after {MAX_ATTEMPTS} "
                    f"attempts. Body: {_body_text(response)}"
                )
            if status >= 300:
                raise EngineStoreError(
                    f"{route}: unexpected status {status}. "
                    f"Body: {_body_text(response)}"
                )

            try:
                body = response.json()
            except Exception as exc:
                raise EngineStoreError(
                    f"{route}: response was not JSON. Body: {_body_text(response)}"
                ) from exc
            if not isinstance(body, dict):
                raise EngineStoreError(
                    f"{route}: expected a JSON object, got {type(body).__name__}"
                )
            return body

        raise EngineStoreError(f"{route}: failed ({last})")   # pragma: no cover

    # -- seen_jobs ----------------------------------------------------------

    def seen_keys(self, user_id: str) -> set[str]:
        """Cannot be answered by this backend. Raises rather than returning [].

        MEASURED 2026-09-15 against the preview project. The seen-jobs route
        FILTERS on the keys it is given; job_keys=[] returns nothing at all:

            write  job_keys=["PROBE"]          -> {"added": 1}
            read   job_keys=["PROBE"]          -> {"seen": ["PROBE"]}
            read   job_keys=[]                 -> {"seen": []}

        So "give me the whole cache" is not expressible. Returning an empty set
        would silently report "nothing seen", and EVERY posting would be
        re-scored on every run at roughly $0.005 each. Use seen_among().

        If Lovable ever changes job_keys=[] to mean "everything", this method
        can be implemented - but re-run tools/probe_engine_routes.py first and
        confirm it, because getting this wrong costs money quietly rather than
        failing loudly.
        """
        raise MissingRouteError(
            "seen_keys() cannot be answered by the Lovable engine routes: "
            "seen-jobs filters on the keys given and job_keys=[] returns "
            "nothing (measured 2026-09-15). Use seen_among(user_id, keys), "
            "which is what the pipeline calls."
        )

    def seen_among(self, user_id: str, keys: set[str]) -> set[str]:
        """Which of `keys` this user has already seen.

        This is what the route is actually built for, and what the pipeline
        uses. It also scales with the number of candidates rather than the size
        of the cache.
        """
        user_id = self._guard(user_id)
        if not keys:
            return set()
        body = self._post("seen-jobs", {"user_id": user_id, "mode": "read",
                                        "job_keys": sorted(keys)})
        return set(body.get("seen") or [])

    def mark_seen(self, user_id: str, keys: set[str]) -> None:
        user_id = self._guard(user_id)
        if not keys:
            return
        self._post("seen-jobs", {"user_id": user_id, "mode": "write",
                                 "job_keys": sorted(keys)})

    # -- job_results --------------------------------------------------------

    def known_urls(self, user_id: str) -> set[str]:
        """Every URL already stored for this user, from the get-known-urls route.

        This is the second deduplication gate: collect() filters on job_key
        first, and this catches a posting whose key changed but whose URL did
        not. Under-reporting here means re-scoring, which costs money.

        The dedicated route exists because get-job-results caps at 500 with no
        pagination, which would have become a hard ceiling after two full runs.
        It pages internally and returns the complete list, and it returns bare
        strings rather than whole rows - no cover letters pulled down just to
        read the URLs off them.

        MEASURED 2026-09-16 against -dev with 20 stored results: the body has
        exactly two keys, 'urls' (list of 20 strings) and 'count' (20), and
        those URLs match the ones get-job-results reports for the same user.
        The field is 'urls', not 'known_urls'. An unknown user_id gives a JSON
        404 {"error": "Unknown user_id"}; a missing one gives a 400.
        """
        user_id = self._guard(user_id)
        body = self._post("get-known-urls", {"user_id": user_id})

        urls = body.get("urls")
        if not isinstance(urls, list):
            raise EngineStoreError(
                f"get-known-urls: expected a 'urls' list, got "
                f"{type(urls).__name__}. Treating that as 'no URLs' would "
                f"re-score everything, so it is an error."
            )

        # The route reports its own count. If it exceeds what arrived, the list
        # was truncated somewhere, and the missing URLs are postings that would
        # be scored and billed a second time.
        count = body.get("count")
        if isinstance(count, int) and count > len(urls):
            raise EngineStoreError(
                f"get-known-urls: reported count={count} but returned "
                f"{len(urls)} URLs. The list is truncated, and the postings it "
                f"omits would be scored and billed again."
            )

        return {u for u in urls if isinstance(u, str) and u}

    def save_results(self, user_id: str, results: list[JobResult]) -> int:
        """Upsert on (user_id, url), server-side. Returns what the route reports.

        MEASURED 2026-09-15: `inserted` counts rows UPSERTED, not rows newly
        created. Saving the same URL twice reported inserted=1 both times and
        returned the same row id. So this number is "rows written", and it does
        not tell you how many were new.
        """
        user_id = self._guard(user_id)
        foreign = sorted({r.user_id for r in results if r.user_id != user_id})
        if foreign:
            raise EngineStoreError(
                f"results belong to {foreign}, not {user_id!r}"
            )
        if not results:
            return 0

        # The route takes user_id once at the top level, so it is stripped from
        # each row. Duplicate URLs inside one batch are collapsed here rather
        # than relying on the server to pick a winner.
        rows, seen_urls = [], set()
        for result in results:
            if not result.url or result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            rows.append(_to_payload(result))

        inserted = 0
        for chunk in _chunks(rows, RESULTS_CHUNK):
            body = self._post("save-job-results",
                              {"user_id": user_id, "results": chunk})
            reported = body.get("inserted")
            inserted += int(reported) if isinstance(reported, (int, float)) \
                else len(body.get("results") or [])
        return inserted

    def _fetch_results(
        self, user_id: str, verdicts: list[Verdict] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Raw rows from get-job-results. Optional fields are omitted entirely
        rather than sent as null, so the route applies its own defaults."""
        payload: dict[str, Any] = {"user_id": user_id}
        if verdicts:
            payload["verdicts"] = list(verdicts)
        if limit is not None:
            payload["limit"] = limit
        body = self._post("get-job-results", payload)
        rows = body.get("results")
        if not isinstance(rows, list):
            raise EngineStoreError(
                f"get-job-results: expected a 'results' list, got "
                f"{type(rows).__name__}"
            )
        return rows

    def get_results(
        self, user_id: str, verdicts: list[Verdict] | None = None
    ) -> list[JobResult]:
        """The scored offers, whole rows. Capped at 500 by the route.

        Truncation here is a display concern, not a billing one - unlike
        known_urls, which is why that moved to its own route.
        """
        user_id = self._guard(user_id)
        rows = self._fetch_results(user_id, verdicts=verdicts,
                                   limit=RESULTS_LIMIT)
        return [_from_row(r) for r in rows if isinstance(r, dict)]

    # -- runs ---------------------------------------------------------------

    def save_run(self, record: RunRecord) -> None:
        self._guard(record.user_id)
        payload = {
            "run_id": record.run_id,
            "user_id": record.user_id,
            "profile_id": record.profile_id,
            "status": record.status,
            "cost_decision": record.cost_decision,
            "cost_estimate": record.cost_estimate,
            "cost_fingerprint": record.cost_fingerprint,
            "counts": record.counts,
            "started_at": _clean(record.started_at),
            "finished_at": _clean(record.finished_at),
        }
        self._post("update-run", payload)

    def get_run(self, user_id: str, run_id: str) -> RunRecord | None:
        """Read one run through the get-run route.

        Returns None when the run does not exist: a poll asking about an
        unknown id is a normal question with a negative answer, not a failure.
        A 404 carrying HTML still raises, because that means the route itself
        is missing rather than the run.
        """
        user_id = self._guard(user_id)
        body = self._post("get-run", {"user_id": user_id, "run_id": run_id},
                          none_on_404=True)
        if body is None:
            return None
        row = body.get("run") or {}
        if not row:
            return None
        # The row carries columns the model does not have (created_at), and
        # nullable timestamps the model wants as "".
        return RunRecord(**{
            field: ("" if row.get(field) is None else row.get(field))
            for field in RunRecord.model_fields if field in row
        })

    # -- applications -------------------------------------------------------

    def get_applications(self, user_id: str) -> dict[str, Application]:
        """No route exposes these. Returns empty.

        Only the Excel Tracker reads applications; with none, it falls back to
        the profile's default status and computed priority. Nothing a user
        typed is lost, because nothing was read.
        """
        self._guard(user_id)
        return {}

    def save_application(self, application: Application) -> None:
        raise MissingRouteError(
            "Cannot save an application: no Lovable route accepts one. "
            "Dropping it silently is the one failure this table was split out "
            "to prevent, so this raises instead. Ask Lovable for "
            "POST /api/public/engine/save-application "
            "{user_id, job_url, status, priority, target_salary, notes}."
        )

    # -- routes with no Store equivalent ------------------------------------

    def get_profile(self, user_id: str) -> dict:
        """The user's profile as Lovable stores it.

        Not a Store method: the engine still loads its scoring profile from
        profiles/*.json. This is here so the onboarding fields Lovable collects
        (target_titles, work_types, search_mode, cv_storage_path) can be read
        when that mapping is built.
        """
        user_id = self._guard(user_id)
        body = self._post("get-profile", {"user_id": user_id})
        return body.get("profile") or {}

    def save_raw_dump(
        self,
        user_id: str,
        connector: str,
        *,
        actor_id: str = "",
        apify_run_id: str = "",
        items_count: int = 0,
        storage_path: str = "",
        run_id: str = "",
    ) -> dict:
        """Record that a paid connector payload was saved, so it can be replayed."""
        user_id = self._guard(user_id)
        body = self._post("save-raw-dump", {
            "user_id": user_id,
            "connector": connector,
            "actor_id": actor_id,
            "apify_run_id": apify_run_id,
            "items_count": items_count,
            "storage_path": storage_path,
            "run_id": run_id,
        })
        return body.get("raw_dump") or {}


# ---------------------------------------------------------------------------
# Payload mapping
# ---------------------------------------------------------------------------

# Exactly the fields save-job-results accepts. user_id is sent once at the top
# level of the request, not repeated on each row.
RESULT_FIELDS = (
    "url", "title", "company", "location", "source", "published", "job_key",
    "run_id", "score", "base_score", "verdict", "breakdown", "match_signals",
    "gaps", "red_flags", "flags", "extra", "generated_text", "one_liner",
    "salary_range_market", "evaluated_at",
)


def _to_payload(result: JobResult) -> dict:
    row = result.model_dump()
    payload = {field: row[field] for field in RESULT_FIELDS}
    payload["evaluated_at"] = _clean(payload["evaluated_at"])
    return payload


def _from_row(row: dict) -> JobResult:
    """Map one get-job-results row back onto the model.

    Tolerant of nulls: Postgres returns null where the model wants "" or an
    empty container, and carries columns the model does not have (id,
    created_at), which are dropped.
    """
    payload = {k: v for k, v in row.items() if k in JobResult.model_fields}
    for key in ("user_id", "run_id", "job_key", "title", "company", "url",
                "source", "published", "location", "one_liner",
                "generated_text", "salary_range_market", "evaluated_at"):
        if payload.get(key) is None:
            payload[key] = ""
    for key in ("match_signals", "gaps", "red_flags"):
        payload[key] = payload.get(key) or []
    for key in ("breakdown", "flags", "extra"):
        payload[key] = payload.get(key) or {}
    for key in ("score", "base_score"):
        payload[key] = float(payload.get(key) or 0)
    payload["verdict"] = payload.get("verdict") or "NO"
    return JobResult.model_validate(payload)


def _body_text(response) -> str:
    try:
        return str(response.text)[:300]
    except Exception:
        return "<unreadable>"


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


# api.py and the tests import SupabaseStore. The name is now inaccurate - there
# is no Supabase connection here - but it is kept as an alias so nothing else
# has to change in the same commit.
SupabaseStore = LovableEngineStore
SupabaseStoreError = EngineStoreError
