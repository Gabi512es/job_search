"""HTTP entry point for the job search engine.

    uvicorn api:app --host 0.0.0.0 --port $PORT

Runs are ASYNCHRONOUS. Measured durations: scoring 25 postings takes ~25s, but
a live LinkedIn fetch is ~25 minutes for its twelve Apify actors, and a full
run reaches ~35 minutes. Railway's proxy and every browser give up long before
that, so POST /run returns 202 with a run_id straight away and the work
continues in the background. GET /run/{run_id} polls.

Run state is written through the Store, not held in memory, so a poll still
works after a redeploy and two instances see the same thing.

    GET  /health            liveness, no auth
    GET  /                  what this service is
    POST /estimate          what a run would cost. Spends nothing.
    POST /run               start a run. SPENDS MONEY.
    GET  /run/{run_id}      status of a run
    POST /run/{id}/select   resume a run held for a volume choice. SPENDS HAIKU.
    GET  /results           the scored offers (this is the deliverable)

Every route except /health and / requires the X-API-Key header. This endpoint
starts paid Haiku and Apify calls, so an unauthenticated one would let anyone
spend the operator's credits.
"""

from __future__ import annotations

import os
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from jobscout.collect import plan_run
from jobscout.connectors import Secrets
from jobscout.connectors.base import DumpStore
from jobscout.pipeline import AWAITING_SELECTION, RunOptions, run as run_pipeline
from jobscout.profile import (
    EngineProfileError, UserProfile, load_profile, profile_from_engine,
)
from jobscout.store.base import RunRecord
from jobscout.store.json_store import JsonStore

from cost_guard import check_run_cost

REPO = Path(__file__).resolve().parent
PROFILES_DIR = REPO / "profiles"
# Where raw Apify payloads are kept. Configurable because the resume flow
# below replays them: on a host with an ephemeral filesystem this should point
# at a mounted disk, or a run held for selection loses its payload on restart
# and can only refuse to resume.
DUMPS_DIR = Path(os.environ.get("DUMPS_DIR", str(REPO / "dumps")))

API_KEY = os.environ.get("JOBSCOUT_API_KEY", "").strip()
STORE_BACKEND = os.environ.get("STORE_BACKEND", "json").strip().lower()
STORE_DIR = os.environ.get("STORE_DIR", str(REPO / "store_data")).strip()

# Comma-separated list of origins allowed to call this API from a browser.
# Lovable runs on its own domain, so it has to be listed explicitly.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]

app = FastAPI(
    title="Job Scout engine",
    version="1.0.0",
    description="Scores job postings against a user profile. See ARCHITECTURE.md.",
)

app.add_middleware(
    CORSMiddleware,
    # No wildcard fallback: an unset ALLOWED_ORIGINS blocks browser calls
    # rather than opening the API to every site.
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Shared-secret check on every route that can spend money or read results.

    When JOBSCOUT_API_KEY is unset the service refuses these routes outright,
    rather than serving them openly: a deployment that forgot the variable
    would otherwise hand anyone the ability to run up an Anthropic bill.
    """
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail=("JOBSCOUT_API_KEY is not configured on the server. "
                    "Protected routes are disabled until it is set."),
        )
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


Protected = Depends(require_api_key)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def get_store():
    """The Store implementation this deployment uses.

    JsonStore writes to disk, which on Railway is ephemeral - fine for a first
    deploy, not for real use. Set STORE_BACKEND=lovable to persist through
    Lovable's engine routes instead; that needs ENGINE_API_KEY.

    The Lovable backend now serves runs and results too (get-run,
    get-job-results, get-known-urls). save_application is the one Store method
    with no route behind it yet, and it raises rather than dropping the write.
    """
    if STORE_BACKEND in ("supabase", "lovable"):
        from jobscout.store.supabase_store import LovableEngineStore
        return LovableEngineStore.from_env()
    return JsonStore(STORE_DIR)


def dumps_dir_for(user_id: str) -> Path:
    """Where this user's raw Apify payloads live.

    Per user, because the dump filename is keyed on profile_id alone and two
    accounts can easily both store a profile called "gabriel". Sharing one
    directory would let one user's run overwrite another's payload, and the
    selection flow below replays that payload rather than re-paying for it -
    so a collision would silently score the wrong person's postings.
    """
    safe = user_id.replace("/", "_").replace("\\", "_").lstrip(".") or "anonymous"
    return DUMPS_DIR / safe


def profile_from_file(profile_id: str) -> UserProfile:
    """Load profiles/<id>.json, with a 404 rather than a stack trace."""
    if not profile_id or "/" in profile_id or "\\" in profile_id or profile_id.startswith("."):
        raise HTTPException(status_code=400, detail=f"invalid profile_id: {profile_id!r}")
    path = PROFILES_DIR / f"{profile_id}.json"
    if not path.exists():
        available = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        raise HTTPException(
            status_code=404,
            detail=f"no profile {profile_id!r}. Available: {available}",
        )
    try:
        return load_profile(path, base_dir=REPO)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"profile {profile_id!r} is invalid: {exc}"
        ) from exc


def resolve_profile(profile_id: str, user_id: str | None) -> tuple[UserProfile, str]:
    """The profile to run and the user it belongs to.

    The two backends are deliberately not merged. STORE_BACKEND=json reads
    profiles/*.json exactly as before, so local runs and the existing tests are
    untouched. STORE_BACKEND=lovable reads the profile out of Supabase through
    get-profile, keyed by user: the deployment has no profiles directory worth
    reading and no CV file at all, so profile_id means nothing there.
    """
    if STORE_BACKEND in ("supabase", "lovable"):
        if not user_id:
            raise HTTPException(
                status_code=400,
                detail=("user_id is required when STORE_BACKEND=lovable: the "
                        "profile is read from Supabase for that user, not from "
                        "a local file. profile_id is ignored in this mode."),
            )
        try:
            row = get_store().get_profile(user_id)
        except HTTPException:
            raise
        except Exception as exc:
            # The route is unreachable or answered badly. That is the engine's
            # dependency failing, not the caller's request being wrong.
            raise HTTPException(
                status_code=502,
                detail=f"get-profile failed for {user_id!r}: "
                       f"{type(exc).__name__}: {exc}",
            ) from exc
        try:
            return profile_from_engine(row, user_id=user_id), user_id
        except EngineProfileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    profile = profile_from_file(profile_id)
    return profile, (user_id or profile.profile_id)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    profile_id: str = Field(
        default="",
        description=("Which profiles/*.json to run, e.g. 'gabriel'. Used only "
                     "when STORE_BACKEND=json; ignored otherwise."),
    )
    user_id: str | None = Field(
        default=None,
        description=("Row owner, a Supabase auth uuid. Required when "
                     "STORE_BACKEND=lovable, since the profile is read for "
                     "that user. Defaults to profile_id locally."),
    )
    confirmed_fingerprint: str | None = Field(
        default=None,
        description=("Set to the fingerprint from a NEEDS_CONFIRMATION response "
                     "to authorise that exact run."),
    )
    max_jobs_to_score: int | None = Field(
        default=25,
        description=("Hard ceiling on billed scoring calls. None removes it - "
                     "a full run reaches ~450 postings."),
    )
    generate_text: bool = Field(
        default=True, description="Also write cover letters / emails (Sonnet)."
    )
    reuse_dumps: bool = Field(
        default=False,
        description=("Replay saved Apify payloads instead of calling Apify. "
                     "Costs nothing and is much faster."),
    )
    select_after_collect: bool = Field(
        default=False,
        description=("Stop after collection with the real pool size and score "
                     "nothing yet. The run reaches AWAITING_SELECTION; resume "
                     "it with POST /run/{run_id}/select."),
    )


class SelectionRequest(BaseModel):
    """How many of the collected postings to actually score."""

    user_id: str = Field(description="Owner of the run")
    max_jobs_to_score: int | None = Field(
        default=None,
        description=("Absolute number, not a percentage: the pool is "
                     "recollected on resume and free feeds move, so a "
                     "percentage would resolve to a different count than the "
                     "one displayed. None scores the whole pool."),
    )
    generate_text: bool = Field(
        default=True, description="Also write cover letters / emails (Sonnet)."
    )


class RunAccepted(BaseModel):
    run_id: str
    status: Literal["RUNNING"] = "RUNNING"
    profile_id: str
    poll: str


class RunStatus(BaseModel):
    run_id: str
    status: str
    profile_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    cost_decision: str = ""
    cost_estimate: dict[str, Any] = Field(default_factory=dict)
    cost_fingerprint: str = ""
    counts: dict[str, int] = Field(default_factory=dict)


def _apply_reuse_dumps(profile: UserProfile) -> UserProfile:
    return profile.model_copy(update={"sources": [
        s.model_copy(update={"reuse_dump": True})
        if s.type in ("infojobs_apify", "linkedin_apify") else s
        for s in profile.sources
    ]})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Liveness for Railway. Deliberately unauthenticated and side-effect free."""
    secrets = Secrets.from_env()
    return {
        "status": "ok",
        "time": datetime.now().isoformat(),
        "store_backend": STORE_BACKEND,
        "profiles_available": sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        if PROFILES_DIR.exists() else [],
        # Whether each credential is present. Never the values.
        "config": {
            "api_key_set": bool(API_KEY),
            "anthropic_key_set": bool(secrets.anthropic_api_key),
            "apify_token_set": bool(secrets.apify_token),
            "cors_origins": len(ALLOWED_ORIGINS),
        },
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "Job Scout engine",
        "runs_are": "asynchronous — POST /run returns a run_id, then poll GET /run/{run_id}",
        "routes": ["/health", "/estimate", "/run", "/run/{run_id}",
                   "/run/{run_id}/select", "/results"],
        "auth": "X-API-Key header on every route except /health and /",
    }


@app.post("/estimate", dependencies=[Protected])
def estimate(request: RunRequest) -> dict:
    """What this run would cost. Makes no network call and spends nothing.

    Apify cost only. The Haiku and Sonnet spend depends on how many postings
    survive filtering, which is not known until they are fetched.
    """
    profile, user_id = resolve_profile(request.profile_id, request.user_id)
    if request.reuse_dumps:
        profile = _apply_reuse_dumps(profile)

    plans, _ = plan_run(profile, dumps_dir_for(user_id))
    result = check_run_cost(plans, profile.cost_policy,
                            confirmed_fingerprint=request.confirmed_fingerprint)
    payload = result.to_dict()
    payload["note"] = (
        "Apify only. Scoring adds roughly $0.005 per posting in Haiku calls, "
        "capped by max_jobs_to_score."
    )
    return payload


@app.post("/run", status_code=202, dependencies=[Protected])
def start_run(request: RunRequest, background: BackgroundTasks) -> RunAccepted:
    """Start a run. SPENDS MONEY once the cost guard approves.

    Returns immediately with a run_id; poll GET /run/{run_id}. A run held for
    cost confirmation finishes almost at once with NEEDS_CONFIRMATION and the
    fingerprint to send back.
    """
    profile, user_id = resolve_profile(request.profile_id, request.user_id)
    if request.reuse_dumps:
        profile = _apply_reuse_dumps(profile)

    secrets = Secrets.from_env()
    if not secrets.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured on the server.",
        )

    store = get_store()
    run_id = uuid.uuid4().hex[:12]

    # Recorded before the work starts, so the first poll has something to read
    # instead of a 404.
    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=datetime.now().isoformat(), status="RUNNING",
    ))

    opts = RunOptions(
        confirmed_fingerprint=request.confirmed_fingerprint,
        max_jobs_to_score=request.max_jobs_to_score,
        generate_text=request.generate_text,
        select_after_collect=request.select_after_collect,
        dumps_dir=str(dumps_dir_for(user_id)),
    )
    background.add_task(_execute, profile, store, secrets, opts, user_id, run_id)
    return RunAccepted(run_id=run_id, profile_id=profile.profile_id,
                       poll=f"/run/{run_id}")


@app.post("/run/{run_id}/select", status_code=202, dependencies=[Protected])
def select_volume(
    run_id: str, request: SelectionRequest, background: BackgroundTasks
) -> RunAccepted:
    """Resume a run held at AWAITING_SELECTION, scoring the chosen number.

    Spends Haiku, and only Haiku: collection is replayed from the Apify payload
    saved by the first call, so Apify is not paid a second time. If that saved
    payload is gone - the deployment restarted between the two calls, and its
    filesystem is ephemeral - this refuses with 409 rather than silently
    re-scraping and re-charging.
    """
    store = get_store()
    record = store.get_run(request.user_id, run_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r} for {request.user_id!r}")
    if record.status != AWAITING_SELECTION:
        raise HTTPException(
            status_code=409,
            detail=(f"run {run_id!r} is {record.status}, not "
                    f"{AWAITING_SELECTION}. Only a run waiting for a volume "
                    f"choice can be resumed this way."))

    profile, user_id = resolve_profile(record.profile_id, request.user_id)
    secrets = Secrets.from_env()
    if not secrets.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured on the server.")

    # Replaying is what makes this free. Check the payload is actually there
    # before promising a 202: without it the connector would fall back to a
    # live fetch and bill the collection twice.
    dumps_dir = dumps_dir_for(user_id)
    profile = _apply_reuse_dumps(profile)
    dumps = DumpStore(dumps_dir)
    missing = [
        s.type for s in profile.enabled_sources()
        if s.type in ("linkedin_apify", "infojobs_apify")
        and not dumps.path_for(s.type, profile.profile_id).exists()
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(f"the saved payload for {', '.join(missing)} is gone, so "
                    f"resuming would re-scrape and charge for collection "
                    f"again. Start a new run instead."))

    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=record.started_at or datetime.now().isoformat(),
        status="RUNNING", counts=record.counts,
    ))
    opts = RunOptions(
        max_jobs_to_score=request.max_jobs_to_score,
        generate_text=request.generate_text,
        dumps_dir=str(dumps_dir),
    )
    background.add_task(_execute, profile, store, secrets, opts, user_id, run_id)
    return RunAccepted(run_id=run_id, profile_id=profile.profile_id,
                       poll=f"/run/{run_id}")


def _execute(profile, store, secrets, opts: RunOptions,
             user_id: str, run_id: str) -> None:
    """The background job. Any failure is recorded, never swallowed."""
    try:
        run_pipeline(
            profile, store, secrets, opts,
            user_id=user_id, repo_dir=REPO, run_id=run_id,
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            store.save_run(RunRecord(
                run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
                finished_at=datetime.now().isoformat(), status="FAILED",
                counts={}, cost_decision=f"{type(exc).__name__}: {exc}"[:200],
            ))
        except Exception:
            traceback.print_exc()


@app.get("/run/{run_id}", dependencies=[Protected])
def run_status(
    run_id: str,
    user_id: str = Query(..., description="Owner of the run"),
) -> RunStatus:
    """Poll a run.

    status is RUNNING, then one of OK / NEEDS_CONFIRMATION /
    AWAITING_SELECTION / REJECTED_OVER_HARD_CAP / FAILED.

    On NEEDS_CONFIRMATION, resend POST /run with cost_fingerprint as
    confirmed_fingerprint. On AWAITING_SELECTION, collection is done and paid
    for and counts.available_to_score holds the real pool; POST
    /run/{run_id}/select with the number to score.
    """
    record = get_store().get_run(user_id, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for {user_id!r}")
    return RunStatus(**record.model_dump())


@app.get("/results", dependencies=[Protected])
def results(
    user_id: str = Query(..., description="Whose results to read"),
    verdict: list[str] | None = Query(
        default=None, description="Filter: YES, MAYBE, NO. Repeatable."),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """The scored offers. This is the deliverable the frontend renders."""
    rows = get_store().get_results(user_id, verdicts=verdict)
    rows.sort(key=lambda r: -r.score)
    return {
        "user_id": user_id,
        "total": len(rows),
        "returned": min(len(rows), limit),
        "results": [r.model_dump() for r in rows[:limit]],
    }
