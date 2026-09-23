import backend.app.config  # noqa: F401 — loads .env before anything else

import os
import threading
from contextlib import asynccontextmanager

import httpx as _httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as _Response

from backend.app.state import _scheduler, _SCHEDULER_OK
from backend.app.database import _init_db, load_stores, _load_pipelines, _save_pipeline, _get_interview_status_from_db, _save_interview, _sync_candidate_interview
from backend.app.state import (
    interview_store, batch_store, opening_store, pipeline_store, opening_pipeline, settings_store,
    ACTIVE_CALL_STATUSES, RESOLVED_OR_ACTIVE_INTERVIEW_STATUSES,
)
from backend.app.callbacks import _reschedule_pending_callbacks

from backend.api.routes.health    import router as health_router
from backend.api.routes.resume    import router as resume_router
from backend.api.routes.interview import router as interview_router
from backend.api.routes.plivo     import router as plivo_router
from backend.api.routes.batch     import router as batch_router
from backend.api.routes.openings  import router as openings_router
from backend.api.routes.auth      import router as auth_router
from backend.api.routes.pipeline  import router as pipeline_router
from backend.api.routes.settings  import router as settings_router
from backend.api.routes.stream    import router as stream_router


def _reconcile_orphaned_calls(max_age_minutes: int = 6, label: str = "Startup") -> int:
    """Resolve calls left mid-flight — scoring any that captured answers.

    A call can be orphaned whenever the provider's call-ended callback doesn't land: an ngrok
    blip, a restart, or the callback arriving during a webhook that had briefly marked the
    interview 'processing'. Before, these sat at 'calling' until a restart stamped them
    failed/abandoned and threw the answers away. Now they're routed through
    _resolve_incomplete_interview(), which scores whatever was captured.

    Runs both at startup and periodically, so an orphaned call no longer needs a restart to
    resolve. max_age_minutes must exceed a normal inter-question gap (a candidate may take
    ~2min on one answer) so a healthy in-progress call is never swept.
    """
    from datetime import datetime, timedelta
    from backend.api.routes.interview import _resolve_incomplete_interview
    from backend.services.interviewer import is_call_active
    cutoff  = datetime.now() - timedelta(minutes=max_age_minutes)
    scored  = 0
    closed  = 0
    skipped_live = 0
    for iid, iv in list(interview_store.items()):
        if iv.get("status") not in ACTIVE_CALL_STATUSES:
            continue

        # Age from the LAST ANSWER, never the call's start. Reading started_at here was a real
        # bug: a normal 7-question interview runs 8-12 minutes, so every healthy call crossed
        # the threshold and got swept mid-interview — one candidate was scored 4/7 while still
        # answering. _last_activity_at is stamped by the answer webhook on every answer.
        call_log = iv.get("call_log", [])
        stamp = iv.get("_last_activity_at") or (call_log[-1].get("started_at") if call_log else None)
        try:
            last_seen = datetime.fromisoformat(stamp) if stamp else None
        except Exception:
            last_seen = None
        if last_seen is None or last_seen >= cutoff:
            continue

        # Idle by our own reckoning, so ask the provider whether the call is actually over.
        # This is the authoritative check — a long pause is not the same as a finished call,
        # and only "definitely ended" justifies scoring. Unknown is not ended.
        if is_call_active(iv.get("twilio_call_sid")) is True:
            skipped_live += 1
            print(f"[{label}] {iid} looks idle but the provider says the call is still live — leaving it alone")
            continue

        try:
            outcome = _resolve_incomplete_interview(
                iid, iv,
                reason=f"Call ended without a completion callback (auto-resolved by {label.lower()} reconciliation)",
            )
            if outcome == "processing":
                scored += 1
            else:
                closed += 1
        except Exception as e:
            print(f"[{label}] reconcile failed for {iid}: {e}")
    if scored or closed or skipped_live:
        print(f"[{label}] Reconciled orphaned calls — {scored} sent for scoring, "
              f"{closed} closed with no answers, {skipped_live} still live and left alone")
    return scored + closed


def _cleanup_stuck_calls():
    # Startup pass. Anything idle for >6 minutes gets resolved; those with answers are scored.
    _reconcile_orphaned_calls(max_age_minutes=6, label="Startup")


def _sweep_repairable_interviews(minutes: int = 30, label: str = "Repair sweep") -> int:
    """Retry answer recovery for interviews that JUST finished and came out incomplete.

    Deliberately scoped to a short window. This is a safety net for the call that has only just
    ended — the case where a recording callback never arrived, so the answer would otherwise be
    lost. It is NOT a backfill for history: re-scoring a call from days ago would rewrite a
    result someone may already have acted on, and re-running the same transcript through the
    scorer does not reproduce the identical number anyway.

    Each interview is checked against the provider: only a question that has no answer text but
    does have audio gets transcribed, then the interview is re-scored. Genuinely unanswered
    questions are left alone, and each interview is retried a bounded number of times, so this
    converges rather than churning.
    """
    from backend.app.database import _list_repairable_interviews
    from backend.api.routes.interview import repair_interview_answers

    ids = _list_repairable_interviews(minutes=minutes)
    if not ids:
        return 0
    repaired = 0
    for iid in ids:
        try:
            if repair_interview_answers(iid):
                repaired += 1
        except Exception as e:
            print(f"[{label}] failed for {iid}: {e}")
    if repaired:
        print(f"[{label}] recovered answers for {repaired} of {len(ids)} incomplete interview(s)")
    return repaired


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if _SCHEDULER_OK:
        _scheduler.start()
        print("[Startup] APScheduler started — callback scheduling enabled")
        from backend.services.recording_cleanup import cleanup_old_recordings
        _scheduler.add_job(
            cleanup_old_recordings,
            'interval',
            hours=24,
            id='recording_cleanup_daily',
            replace_existing=True,
            misfire_grace_time=3600,
        )
        print("[Startup] Recording cleanup scheduled — runs daily")
        # Periodic safety net for calls whose completion callback never arrived (ngrok blip,
        # restart, or a callback landing mid-webhook). Without this, an orphaned call sat at
        # 'calling' until the next restart — and its answers were discarded rather than
        # scored. Runs every 5 minutes; only touches interviews idle for >6 minutes, so a
        # healthy in-progress interview is never swept.
        _scheduler.add_job(
            lambda: _reconcile_orphaned_calls(max_age_minutes=6, label="Reconcile"),
            'interval',
            minutes=5,
            id='orphaned_call_reconcile',
            replace_existing=True,
            misfire_grace_time=300,
        )
        print("[Startup] Orphaned-call reconciliation scheduled — runs every 5 minutes")
        # Second safety net, for answers rather than calls. A recording that only becomes
        # available (or only becomes transcribable) after scoring finished leaves the row
        # marked "incomplete" while the audio sits at the provider. The recording callback
        # normally triggers a repair immediately; this catches the cases where that callback
        # never arrived — an ngrok blip, or a restart between the call and the callback.
        _scheduler.add_job(
            _sweep_repairable_interviews,
            'interval',
            minutes=15,
            id='answer_repair_sweep',
            replace_existing=True,
            misfire_grace_time=600,
        )
        print("[Startup] Answer-repair sweep scheduled — runs every 15 minutes")
    _init_db()
    from backend.app.database import _get_setting
    env_default = settings_store.get("call_provider")
    persisted_provider = _get_setting("call_provider")
    print(f"[Startup] env/current call_provider='{env_default}', DB-persisted call_provider={persisted_provider!r}")
    if persisted_provider in ("twilio", "plivo"):
        settings_store["call_provider"] = persisted_provider
        print(f"[Startup] Restored call_provider='{persisted_provider}' from DB")
    else:
        print(f"[Startup] No valid persisted call_provider found — keeping '{env_default}'")
    persisted_mode = _get_setting("interview_mode")
    if persisted_mode in ("legacy", "streaming"):
        settings_store["interview_mode"] = persisted_mode
        print(f"[Startup] Restored interview_mode='{persisted_mode}' from DB")
    else:
        print(f"[Startup] interview_mode='{settings_store.get('interview_mode', 'legacy')}' (env/default)")
    from backend.app.database import _seed_super_admin
    sa_user = os.getenv("SUPER_ADMIN_USERNAME", "director")
    sa_pass = os.getenv("SUPER_ADMIN_PASSWORD", "changeme")
    _seed_super_admin(sa_user, sa_pass)
    print(f"[Startup] Super admin '{sa_user}' ready (seeded only if first run)")
    loaded_ivs, loaded_batches, loaded_openings = load_stores()
    interview_store.update(loaded_ivs)
    batch_store.update(loaded_batches)
    opening_store.update(loaded_openings)
    loaded_pipelines = _load_pipelines()
    pipeline_store.update(loaded_pipelines)
    for pid, p in loaded_pipelines.items():
        opening_pipeline[p["opening_id"]] = pid
        # Purge active entries whose Twilio calls ended during the downtime
        stale = []
        for iid, candidate in list(p["active"].items()):
            iv = interview_store.get(iid)
            status = iv.get("status") if iv else _get_interview_status_from_db(iid)
            # Previously missing "declined" here meant a candidate who declined during
            # a server restart was never detected as stale — left orphaned in this
            # pipeline's "active" dict forever, since it fell outside this whole check.
            if status in RESOLVED_OR_ACTIVE_INTERVIEW_STATUSES:
                stale.append((iid, candidate, status))
        for iid, candidate, status in stale:
            p["active"].pop(iid, None)
            if status == "completed":
                p["completed"].append(candidate)
            elif status in ("callback_scheduled", "declined"):
                p["skipped"].append({**candidate, "skip_reason": "callback" if status == "callback_scheduled" else "declined"})
            elif status in ACTIVE_CALL_STATUSES:
                # Re-fetch the interview for THIS iid — do not reuse the loop-variable
                # left over from the scan loop above (that held the last active entry)
                iv = interview_store.get(iid)
                # Mark old interview failed so batch_candidates.interview_status is cleared
                # immediately — prevents permanent 'calling' if the next call also fails
                if iv:
                    iv["status"]      = "failed"
                    iv["fail_reason"] = "Auto-resolved on restart: server restarted mid-call"
                    _save_interview(iid, iv)
                    _sync_candidate_interview(iid, iv)
                # Server crashed mid-call — re-queue once (1 retry max, consistent with pipeline logic)
                if candidate.get("no_answer_count", 0) < 1:
                    candidate["no_answer_count"] = candidate.get("no_answer_count", 0) + 1
                    p["queue"].append(candidate)
                    print(f"[Startup] Re-queued {candidate.get('name')} after crash (no_answer #{candidate['no_answer_count']})")
                else:
                    p["skipped"].append({**candidate, "skip_reason": "no_answer_twice"})
            else:
                p["skipped"].append({**candidate, "skip_reason": status})
        if stale:
            _save_pipeline(pid, p)
            print(f"[Startup] Cleared {len(stale)} stale active entries from pipeline {pid}")
    if loaded_pipelines:
        print(f"[Startup] Restored {len(loaded_pipelines)} active pipeline(s) from DB")

    # Clear stale calling/in_progress records directly in DB.
    # pipeline_id is not persisted to the interviews table, so we can't filter by it.
    # Instead: any interview stuck as 'calling'/'in_progress' for > 5 minutes is definitively
    # stale (Twilio ring timeout is ~20s; full interview never exceeds 30 min).
    # Interviews legitimately active in a pipeline's active dict are excluded.
    _active_in_pipeline = {
        iid
        for p in pipeline_store.values()
        for iid in p.get("active", {})
    }
    try:
        from backend.app.database import _db_engine, _sql
        if _db_engine:
            with _db_engine.connect() as _conn:
                stale_rows = _conn.execute(_sql("""
                    SELECT id FROM interviews
                    WHERE status IN ('calling', 'in_progress')
                      AND updated_at < NOW() - INTERVAL '5 minutes'
                """)).mappings().all()
            stale_iids = [str(r["id"]) for r in stale_rows
                          if str(r["id"]) not in _active_in_pipeline]
            if stale_iids:
                # Resolve each one individually rather than a blanket UPDATE to 'failed'.
                # The old blanket update discarded answers: a candidate who hung up after
                # answering some questions was stamped "stale call" with no score, and the
                # recruiter had nothing to review. _resolve_incomplete_interview() scores
                # anything that captured answers and only closes out the genuinely empty ones.
                from backend.api.routes.interview import _get_interview, _resolve_incomplete_interview
                _scored = _closed = 0
                for iid in stale_iids:
                    try:
                        iv = _get_interview(iid)   # falls back to loading from the DB
                        if not iv:
                            continue
                        outcome = _resolve_incomplete_interview(
                            iid, iv,
                            reason="Call ended without a completion callback (auto-resolved on startup)",
                        )
                        if outcome == "processing":
                            _scored += 1
                        else:
                            _closed += 1
                    except Exception as _ie:
                        print(f"[Startup] resolve failed for {iid}: {_ie}")
                print(f"[Startup] Resolved {len(stale_iids)} stale interview(s) — "
                      f"{_scored} sent for scoring, {_closed} closed with no answers")
    except Exception as _e:
        print(f"[Startup] Stale call DB cleanup failed: {_e}")

    _cleanup_stuck_calls()
    _reschedule_pending_callbacks()
    try:
        r = _httpx.get("http://localhost:4040/api/tunnels", timeout=2)
        tunnels = r.json().get("tunnels", [])
        https_url = next(
            (t["public_url"] for t in tunnels if t["public_url"].startswith("https")), None
        )
        if https_url:
            os.environ["BASE_URL"] = https_url
            print(f"[Startup] ngrok auto-detected → BASE_URL={https_url}")
        else:
            print(f"[Startup] ngrok running but no HTTPS tunnel. BASE_URL={os.getenv('BASE_URL', 'NOT SET')}")
    except Exception:
        print(f"[Startup] ngrok not detected. BASE_URL={os.getenv('BASE_URL', 'NOT SET')}")
    yield
    if _SCHEDULER_OK:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="AI Recruitment Assistant", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def _get_error_xml(is_plivo: bool) -> str:
    if is_plivo:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            '<Speak voice="Polly.Kajal">'
            "We're having a technical issue. We'll call you back shortly. Goodbye!"
            '</Speak>'
            '<Hangup/>'
            '</Response>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Say voice="Google.en-IN-Neural2-A">'
        "We're having a technical issue. We'll call you back shortly. Goodbye!"
        '</Say>'
        '<Hangup/>'
        '</Response>'
    )

@app.exception_handler(Exception)
async def twilio_fallback_handler(request: Request, exc: Exception):
    """Return graceful TwiML/Plivo-XML on any unhandled exception in the Twilio or Plivo webhook routes."""
    path = str(request.url.path)
    if "/twilio/" in path or "/plivo/" in path:
        print(f"[TwiML] Unhandled error on {path}: {exc}")
        return _Response(content=_get_error_xml(is_plivo="/plivo/" in path), media_type="text/xml", status_code=200)
    raise exc

from fastapi import Depends
from backend.api.routes.auth import _require_auth

# Routers that mix public webhooks with app routes, or self-guard, are wired without a
# blanket dependency: auth_router (self-guards), health, interview_router (/twilio/* is public),
# plivo_router (/plivo/* is public), settings_router (self-guards super_admin).
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(interview_router)
app.include_router(plivo_router)
app.include_router(settings_router)
app.include_router(stream_router)   # streaming-mode XML + websockets (/twilio|/plivo/stream-xml, /ws/*) — public like the other webhooks

# Data/action routers — require a valid JWT on every route.
_auth_dep = [Depends(_require_auth)]
app.include_router(resume_router,   dependencies=_auth_dep)
app.include_router(batch_router,    dependencies=_auth_dep)
app.include_router(openings_router, dependencies=_auth_dep)
app.include_router(pipeline_router, dependencies=_auth_dep)
