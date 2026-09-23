import uuid
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException

from backend.app.state import (
    interview_store, opening_store,
    pipeline_store, opening_pipeline,
    DEFAULT_QUESTIONS,
)
from backend.app.database import (
    _save_pipeline, _get_qualified_candidates_for_opening,
    _save_interview, _link_batch_candidate_interview, _link_single_candidate_interview,
    _link_candidate_interview_by_bcid,
)

router = APIRouter()

# Per-pipeline locks to prevent race conditions when concurrent calls end simultaneously
_pipeline_locks: dict = {}
_locks_mutex = threading.Lock()


def _get_lock(pipeline_id: str) -> threading.Lock:
    with _locks_mutex:
        if pipeline_id not in _pipeline_locks:
            _pipeline_locks[pipeline_id] = threading.Lock()
        return _pipeline_locks[pipeline_id]


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _start_candidate_call(pipeline_id: str, candidate: dict, pipeline: dict) -> str | None:
    from backend.services.interviewer import generate_questions, start_twilio_call
    interview_id = None
    try:
        resume_text = candidate.get("resume_text") or ""
        jd_text     = pipeline.get("jd_text") or ""
        job_title   = pipeline.get("job_title") or ""
        phone       = candidate.get("phone", "")
        name        = candidate.get("name") or "Candidate"
        opening_id  = pipeline.get("opening_id")

        try:
            questions = generate_questions(resume_text, jd_text)
        except Exception:
            questions = DEFAULT_QUESTIONS[:]

        interview_id = str(uuid.uuid4())
        now_iso = datetime.now().isoformat()
        interview_data = {
            "interview_id":          interview_id,
            "opening_id":            opening_id,
            "pipeline_id":           pipeline_id,
            "status":                "calling",
            "consent_status":        "pending",
            "consent_raw":           None,
            "consent_re_asked":      False,
            "callback_time_raw":     None,
            "callback_scheduled_at": None,
            "candidate_name":        name,
            "phone":                 phone,
            "job_title":             job_title,
            "jd_text":               jd_text,
            "twilio_call_sid":       None,
            "transcript":            None,
            "fail_reason":           None,
            "processing_step":       None,
            "questions":             questions,
            # In-memory only — seeds the Whisper vocabulary hint (see _vocab_for in interview.py)
            "resume_text":           resume_text,
            "recordings":            {},
            "transcriptions":        {},
            "repeat_counts":         {},
            "score_result":          None,
            "call_log":              [{"attempt": 1, "started_at": now_iso, "status": "calling"}],
        }

        interview_store[interview_id] = interview_data
        _save_interview(interview_id, interview_data)

        # Link interview back to the batch_candidates row so existing sync logic works.
        # Prefer primary key (bc_id) — guaranteed to match exactly one row regardless of
        # whether batch_id / single_id are set.
        bc_id     = candidate.get("bc_id")
        batch_id  = candidate.get("batch_id")
        file_name = candidate.get("file_name")
        single_id = candidate.get("single_id")
        if bc_id:
            _link_candidate_interview_by_bcid(bc_id, interview_id)
        elif single_id:
            _link_single_candidate_interview(single_id, interview_id)
        elif batch_id and file_name:
            _link_batch_candidate_interview(batch_id, file_name, interview_id)
        else:
            print(f"[Pipeline] WARNING: no link key for candidate {name} — won't appear in active calls")

        start_twilio_call(phone, interview_id)
        print(f"[Pipeline] Call started: {name} ({phone}) → {interview_id}")
        return interview_id
    except Exception as e:
        print(f"[Pipeline] Failed to start call for {candidate.get('name')}: {e}")
        if interview_id and interview_id in interview_store:
            del interview_store[interview_id]
            try:
                from backend.app.database import _delete_interview
                _delete_interview(interview_id)
            except Exception:
                pass
        # Reset batch_candidates so this candidate can be picked up by the next pipeline run
        bc_id = candidate.get("bc_id")
        if bc_id and interview_id:
            try:
                from backend.app.database import _reset_candidate_on_call_failure
                _reset_candidate_on_call_failure(bc_id, interview_id)
            except Exception:
                pass
        return None


def _fill_slots(p: dict):
    """Start calls to fill up to max_concurrent active slots. Called inside the pipeline lock."""
    max_c = p.get("max_concurrent", 3)
    while len(p["active"]) < max_c and p["queue"]:
        candidate = p["queue"].pop(0)
        _save_pipeline(p["pipeline_id"], p)  # persist queue removal before call — prevents re-call if server crashes mid-slot
        iid = _start_candidate_call(p["pipeline_id"], candidate, p)
        if iid:
            p["active"][iid] = candidate
        elif candidate.get("call_failed_count", 0) < 1:
            # Separate counter from no_answer_count so a call-creation failure doesn't
            # consume the candidate's later no-answer retry budget.
            candidate["call_failed_count"] = candidate.get("call_failed_count", 0) + 1
            p["queue"].append(candidate)   # retry once on Twilio/Plivo call creation failure
            print(f"[Pipeline] {candidate.get('name')} re-queued after call_failed (attempt #{candidate['call_failed_count']})")
        else:
            p["skipped"].append({**candidate, "skip_reason": "call_failed"})
        _save_pipeline(p["pipeline_id"], p)  # persist final state (active/re-queued/skipped)


def _on_pipeline_call_ended(interview_id: str, outcome: str):
    """
    Called from interview.py hooks whenever a pipeline call reaches a terminal state.
    outcome: "completed" | "no_answer" | "declined" | "callback"
    """
    # Find which pipeline owns this interview
    target_pid = None
    for pid, p in list(pipeline_store.items()):
        if interview_id in p.get("active", {}):
            target_pid = pid
            break

    # Fallback: if the active-scan missed it (e.g. the terminal event raced slot-filling
    # before active[iid] was populated), recover the owning pipeline from the interview's
    # own stored pipeline_id — it's written before the call is ever placed.
    if not target_pid:
        iv = interview_store.get(interview_id)
        target_pid = iv.get("pipeline_id") if iv else None

    if not target_pid:
        return

    lock = _get_lock(target_pid)
    with lock:
        p = pipeline_store.get(target_pid)
        if not p:
            return

        candidate = p["active"].pop(interview_id, None)
        if candidate is None:
            return

        if outcome == "no_answer":
            if candidate.get("no_answer_count", 0) < 1:
                candidate["no_answer_count"] = candidate.get("no_answer_count", 0) + 1
                p["queue"].append(candidate)   # 1 retry at end of queue (2 total attempts)
                print(f"[Pipeline] {candidate.get('name')} re-queued (no answer #{candidate['no_answer_count']})")
            else:
                p["skipped"].append({**candidate, "skip_reason": "no_answer_twice"})
        elif outcome in ("declined", "callback"):
            p["skipped"].append({**candidate, "skip_reason": outcome})
        else:  # completed
            p["completed"].append(candidate)

        if p["status"] == "running":
            _fill_slots(p)
            if not p["queue"] and not p["active"]:
                p["status"] = "completed"
                opening_pipeline.pop(p["opening_id"], None)
                print(f"[Pipeline] {target_pid} completed — {len(p['completed'])} interviewed")

        _save_pipeline(target_pid, p)


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/openings/{opening_id}/pipeline/start")
async def pipeline_start(opening_id: str):
    if opening_id not in opening_store:
        raise HTTPException(404, "Opening not found")

    # One active pipeline per opening
    existing_pid = opening_pipeline.get(opening_id)
    if existing_pid and pipeline_store.get(existing_pid, {}).get("status") == "running":
        raise HTTPException(409, "A pipeline is already running for this opening")

    candidates = _get_qualified_candidates_for_opening(opening_id)
    if not candidates:
        raise HTTPException(400, "No qualified candidates with phone numbers available to call")

    # Deduplicate by phone (keep highest resume_score per phone)
    seen_phones: set = set()
    queue = []
    for c in candidates:
        phone = (c.get("phone") or "").strip()
        if phone and phone not in seen_phones:
            seen_phones.add(phone)
            queue.append({
                "bc_id":          c.get("id"),       # batch_candidates primary key
                "batch_id":       c.get("batch_id"),
                "single_id":      c.get("single_id"),
                "file_name":      c.get("file_name"),
                "name":           c.get("name"),
                "phone":          phone,
                "resume_text":    c.get("resume_text") or "",
                "resume_score":   c.get("resume_score"),
                "no_answer_count": 0,
            })

    # Pull jd_text / job_title from first candidate row (populated by JOIN in the query)
    jd_text   = candidates[0].get("jd_text") or ""
    job_title = candidates[0].get("job_title") or ""

    pipeline_id = str(uuid.uuid4())
    p = {
        "pipeline_id":   pipeline_id,
        "opening_id":    opening_id,
        "status":        "running",
        "queue":         queue,
        "active":        {},
        "completed":     [],
        "skipped":       [],
        "total":         len(queue),
        "max_concurrent": 3,
        "jd_text":       jd_text,
        "job_title":     job_title,
    }

    pipeline_store[pipeline_id]    = p
    opening_pipeline[opening_id]   = pipeline_id
    _get_lock(pipeline_id)          # pre-create lock

    _save_pipeline(pipeline_id, p)

    # Start first batch of calls (up to 5) in a background thread so the HTTP response returns fast
    import threading as _t
    lock = _get_lock(pipeline_id)
    def _kick():
        with lock:
            _fill_slots(p)
            _save_pipeline(pipeline_id, p)
    _t.Thread(target=_kick, daemon=True).start()

    print(f"[Pipeline] Started {pipeline_id} for opening {opening_id} — {len(queue)} candidates")
    return {
        "pipeline_id":    pipeline_id,
        "status":         "running",
        "total":          p["total"],
        "queue_remaining": len(p["queue"]),
        "active_count":   len(p["active"]),
        "completed_count": 0,
        "skipped_count":  0,
    }


@router.get("/openings/{opening_id}/pipeline/status")
async def pipeline_status(opening_id: str):
    pid = opening_pipeline.get(opening_id)
    if not pid or pid not in pipeline_store:
        return {"status": "none"}

    p = pipeline_store[pid]
    return {
        "pipeline_id":     pid,
        "status":          p["status"],
        "total":           p["total"],
        "active_count":    len(p["active"]),
        "queue_remaining": len(p["queue"]),
        "completed_count": len(p["completed"]),
        "skipped_count":   len(p["skipped"]),
        "active_candidates": [
            {"name": v.get("name"), "phone": v.get("phone")}
            for v in p["active"].values()
        ],
        "queue": [
            {"name": c.get("name"), "file_name": c.get("file_name"), "resume_score": c.get("resume_score")}
            for c in p["queue"]
        ],
        "skipped": [
            {"name": c.get("name"), "file_name": c.get("file_name"), "skip_reason": c.get("skip_reason")}
            for c in p["skipped"]
        ],
    }


@router.post("/openings/{opening_id}/pipeline/stop")
async def pipeline_stop(opening_id: str):
    pid = opening_pipeline.get(opening_id)
    if not pid or pid not in pipeline_store:
        raise HTTPException(404, "No active pipeline for this opening")

    lock = _get_lock(pid)
    with lock:
        p = pipeline_store[pid]
        p["status"] = "stopped"
        p["queue"]  = []   # drain queue so no new calls start
        opening_pipeline.pop(opening_id, None)
        _save_pipeline(pid, p)

    print(f"[Pipeline] {pid} stopped by user")
    return {"status": "stopped"}
