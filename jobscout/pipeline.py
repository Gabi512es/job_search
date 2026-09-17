"""The whole run, start to finish. One function, explicit arguments, no globals.

    report = run(profile, store, secrets, opts)

Sequence (ARCHITECTURE.md 3.1 and 5.1):

    plan every enabled source      no network, no spend
    check_run_cost                 one decision for the whole run
    -> not OK: stop here, nothing fetched, nothing spent
    fetch                          connectors run
    deduplicate / exclude / skip   free, and it is what keeps the Haiku bill down
    score                          BILLED: one Haiku call per surviving job
    generate application text      BILLED: one Sonnet call per eligible result
    write job_results              the actual deliverable
    export .xlsx                   optional convenience

Everything the old notebooks kept in module-level globals - the client, the
seen set, the cache path, the profile - is a parameter here, so two users can
run at the same time without touching each other's state.
"""

from __future__ import annotations

import concurrent.futures
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jobscout.collect import CollectionResult, collect
from jobscout.filters import interleave_by_source
from jobscout.connectors import Secrets
from jobscout.jobs import JobPosting
from jobscout.profile import UserProfile, Verdict
from jobscout.scoring import ScoredJob, score_job
from jobscout.store.base import JobResult, RunRecord, Store, to_job_result
from jobscout.writing import generate_text, should_generate

from cost_guard import CostDecision, RunEstimate

# A run that has collected and is waiting for the caller to choose how many
# postings to score. Not a CostDecision: the cost guard has already said OK and
# the Apify money is spent. What is still unspent is the Haiku scoring, which
# is what the caller is choosing the size of.
AWAITING_SELECTION = "AWAITING_SELECTION"

_print_lock = threading.Lock()


def _log(*args) -> None:
    with _print_lock:
        print(*args, flush=True)


class RunOptions(BaseModel):
    """Per-run switches. None of these belong to the profile: they are about
    how this particular execution behaves, not about who the candidate is."""

    use_seen_cache: bool = True
    generate_text: bool = True
    scoring_workers: int = 10
    writing_workers: int = 5
    confirmed_fingerprint: str | None = None

    # Safety rail for development: refuse to score more than this many jobs in
    # one run. A full LinkedIn + RSS run reaches ~450 postings, which is real
    # money in Haiku calls; a typo in a keyword list should not spend it.
    max_jobs_to_score: int | None = None

    # Stop after collection, before any billed scoring call, and record the
    # real pool size so the caller can choose a volume from it. Resuming is
    # free: the paid Apify payload is replayed from its dump.
    select_after_collect: bool = False

    export_xlsx_path: str | None = None
    export_verdicts: list[Verdict] = ["YES", "MAYBE"]

    dumps_dir: str = "dumps"


class RunReport(BaseModel):
    """What the caller gets back. Serialises straight to JSON for an API."""

    run_id: str
    profile_id: str
    status: str
    reason: str = ""
    cost: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    results: list[JobResult] = Field(default_factory=list)
    export_path: str = ""

    @property
    def ran(self) -> bool:
        return self.status == CostDecision.OK.value


def _awaiting_report(
    run_id: str, profile: UserProfile, store: Store, user_id: str,
    started: str, collection: CollectionResult, counts: dict[str, int],
) -> RunReport:
    """Collected, nothing scored, waiting for the caller to choose a volume.

    Everything paid at this point is the Apify collection, and that money is
    spent whatever the caller then picks: the choice only governs Haiku calls,
    of which none has been made.

    The postings themselves are deliberately NOT persisted. Resuming re-runs
    collection with the saved Apify payload replayed from its dump, which costs
    nothing, plus a fresh read of the free feeds. That avoids inventing a
    storage route for pending postings, at one cost worth stating: the feeds
    move between the two calls, so the pool can shift by a few postings. The
    caller therefore sends an absolute number, not the percentage it showed.
    """
    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=started, finished_at=datetime.now().isoformat(),
        status=AWAITING_SELECTION,
        cost_decision=collection.cost.decision.value,
        cost_estimate=collection.cost.to_dict(),
        cost_fingerprint=collection.cost.fingerprint,
        counts=counts,
    ))
    _log(f"[pipeline] awaiting selection: {counts['available_to_score']} "
         f"postings collected, none scored")
    return RunReport(
        run_id=run_id, profile_id=profile.profile_id,
        status=AWAITING_SELECTION,
        reason=(f"{counts['available_to_score']} postings collected and not "
                f"yet scored. Choose how many to score."),
        cost=collection.cost.to_dict(), counts=counts,
    )


def _held_report(
    run_id: str, profile: UserProfile, estimate: RunEstimate, store: Store,
    user_id: str,
) -> RunReport:
    """A run the cost guard would not let start. Recorded, so the confirmation
    can arrive in a later request without keeping the estimate in memory."""
    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=datetime.now().isoformat(),
        finished_at=datetime.now().isoformat(),
        status=estimate.decision.value,
        cost_decision=estimate.decision.value,
        cost_estimate=estimate.to_dict(),
        cost_fingerprint=estimate.fingerprint,
    ))
    return RunReport(
        run_id=run_id, profile_id=profile.profile_id,
        status=estimate.decision.value, reason=estimate.reason,
        cost=estimate.to_dict(),
    )


def run(
    profile: UserProfile,
    store: Store,
    secrets: Secrets,
    opts: RunOptions | None = None,
    *,
    user_id: str | None = None,
    cv_text: str | None = None,
    client: Any = None,
    repo_dir: Path | str = ".",
    run_id: str | None = None,
) -> RunReport:
    """Execute one job search.

    `client` is injectable so the pipeline can be exercised end to end without
    calling the API. Left as None, an Anthropic client is built from `secrets`
    when scoring is actually reached.
    """
    opts = opts or RunOptions()
    user_id = user_id or profile.profile_id
    # Supplied by the HTTP API, which must return the id before the work
    # starts; generated here for direct callers.
    run_id = run_id or uuid.uuid4().hex[:12]
    started = datetime.now().isoformat()

    cv_text = cv_text if cv_text is not None else profile.load_cv(Path(repo_dir))

    # ---- 1. collect: plan -> cost guard -> fetch -------------------------
    # Bound to the store rather than materialised: the Lovable backend cannot
    # enumerate its seen-jobs cache, only answer "which of these do you know".
    seen_among = (
        (lambda keys: store.seen_among(user_id, keys))
        if opts.use_seen_cache else None
    )
    collection = collect(
        profile, secrets,
        seen_among=seen_among,
        confirmed_fingerprint=opts.confirmed_fingerprint,
        dumps_dir=opts.dumps_dir,
    )
    if not collection.ran:
        _log(f"[pipeline] {collection.decision.value}: {collection.cost.reason}")
        return _held_report(run_id, profile, collection.cost, store, user_id)

    counts = dict(collection.counts)
    jobs = collection.jobs

    # Already-scored URLs are skipped before any billed call. This is what the
    # unique (user_id, url) constraint buys us.
    known = store.known_urls(user_id)
    before = len(jobs)
    jobs = [j for j in jobs if j.url not in known]
    counts["already_scored"] = before - len(jobs)

    # The real pool: everything that survived deduplication, the profile's
    # exclusions and both already-scored gates. This is the number a
    # "score 25% / 50% / 100%" selector has to be built on, and it is named
    # explicitly because counts["to_score"] below is overwritten with the
    # post-cap number - the pool used to be recoverable only by adding
    # to_score and capped_out back together.
    counts["available_to_score"] = len(jobs)

    # Ordered before the cap, and unconditionally rather than only when a cap
    # is set: the cap below is a plain prefix, so without this the order the
    # connectors happened to run in decides which sources get scored at all.
    # One code path is also one thing to reason about.
    jobs = interleave_by_source(jobs)

    # Second pause. The first one (the cost guard) stops a run before it
    # spends; this one stops it after the collection is paid for and before
    # the scoring is. The pool is only knowable here: deduplicating and
    # excluding needs the postings in hand.
    if opts.select_after_collect:
        return _awaiting_report(run_id, profile, store, user_id, started,
                                collection, counts)

    if opts.max_jobs_to_score is not None and len(jobs) > opts.max_jobs_to_score:
        _log(f"[pipeline] capping {len(jobs)} jobs at "
             f"{opts.max_jobs_to_score} (max_jobs_to_score), "
             f"interleaved so every source is represented")
        counts["capped_out"] = len(jobs) - opts.max_jobs_to_score
        jobs = jobs[:opts.max_jobs_to_score]

    counts["to_score"] = len(jobs)
    _log(f"[pipeline] {len(jobs)} jobs to score")

    if not jobs:
        return _finish(run_id, profile, store, user_id, started, collection,
                       counts, [], opts)

    # ---- 2. score (billed) ----------------------------------------------
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=secrets.anthropic_api_key)

    scored = _score_all(client, profile, jobs, cv_text, opts.scoring_workers)
    counts["scored"] = len(scored)
    counts["unscoreable"] = len(jobs) - len(scored)
    for verdict in ("YES", "MAYBE", "NO"):
        counts[f"verdict_{verdict}"] = sum(1 for _, s in scored if s.verdict == verdict)

    # The cache is updated as soon as scoring is done, before text generation.
    # If generation crashes, the expensive part is not repeated on the next run.
    if opts.use_seen_cache:
        store.mark_seen(user_id, {job.key for job in jobs})

    # ---- 3. application text (billed) ------------------------------------
    if opts.generate_text and profile.writing and profile.writing.enabled:
        eligible = [(j, s) for j, s in scored if should_generate(profile, s)]
        _log(f"[pipeline] generating {len(eligible)} application texts")
        _generate_all(client, profile, eligible, cv_text, opts.writing_workers)
        counts["texts_generated"] = sum(1 for _, s in eligible if s.generated_text)

    # ---- 4. persist -------------------------------------------------------
    rows = [to_job_result(s, j, user_id, run_id) for j, s in scored]
    counts["saved"] = store.save_results(user_id, rows)

    return _finish(run_id, profile, store, user_id, started, collection,
                   counts, rows, opts)


def _score_all(client, profile, jobs, cv_text, workers) -> list[tuple[JobPosting, ScoredJob]]:
    """Score every job in parallel, preserving input order in the result."""
    results: list[tuple[int, JobPosting, ScoredJob]] = []
    total = len(jobs)
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(score_job, client, profile, job, cv_text): (index, job)
            for index, job in enumerate(jobs)
        }
        for future in concurrent.futures.as_completed(futures):
            index, job = futures[future]
            done += 1
            try:
                scored = future.result()
            except Exception as exc:
                _log(f"  [{done}/{total}] error on {job.title[:40]!r}: {exc}")
                continue
            if scored is None:
                _log(f"  [{done}/{total}] skipped {job.title[:40]!r}")
                continue
            results.append((index, job, scored))
            _log(f"  [{done}/{total}] {scored.verdict:<5} {scored.score:>4} "
                 f"{scored.title[:44]!r} @ {scored.company[:24]!r}")

    results.sort(key=lambda item: item[0])
    return [(job, scored) for _, job, scored in results]


def _generate_all(client, profile, eligible, cv_text, workers) -> None:
    """Fill in generated_text on each eligible ScoredJob, in place."""
    if not eligible:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(generate_text, client, profile, scored, cv_text): scored
            for _, scored in eligible
        }
        for future in concurrent.futures.as_completed(futures):
            scored = futures[future]
            try:
                scored.generated_text = future.result()
            except Exception as exc:
                _log(f"  [write] {scored.title[:40]!r}: {exc}")


def _finish(
    run_id, profile, store, user_id, started, collection, counts, rows, opts
) -> RunReport:
    """Record the run, optionally export, and build the report."""
    export_path = ""
    if opts.export_xlsx_path:
        from jobscout.export.excel import export_xlsx
        exportable = store.get_results(user_id, verdicts=opts.export_verdicts)
        export_xlsx(
            profile, exportable, opts.export_xlsx_path,
            applications=store.get_applications(user_id),
        )
        export_path = str(opts.export_xlsx_path)
        _log(f"[pipeline] exported {len(exportable)} rows to {export_path}")

    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=started, finished_at=datetime.now().isoformat(),
        status=CostDecision.OK.value, cost_decision=CostDecision.OK.value,
        cost_estimate=collection.cost.to_dict(),
        cost_fingerprint=collection.cost.fingerprint,
        counts=counts,
    ))

    return RunReport(
        run_id=run_id, profile_id=profile.profile_id,
        status=CostDecision.OK.value,
        reason=collection.cost.reason,
        cost=collection.cost.to_dict(),
        counts=counts, results=rows, export_path=export_path,
    )
