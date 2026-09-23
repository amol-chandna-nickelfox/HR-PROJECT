import uuid
import threading
from typing import List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Form
from pydantic import BaseModel

from backend.services.analyzer import analyze
from backend.services.interviewer import generate_questions, start_twilio_call
from backend.utils.file_utils import extract_text, extract_job_title
from backend.app.state import (
    interview_store, batch_store, opening_store, DEFAULT_QUESTIONS, get_qualification_threshold,
    DASHBOARD_ACTIVE_STATUSES,
)
from backend.app.database import _save_batch, _save_interview, _parse_interview_score, _combined_score, _override_qualify_candidate

router = APIRouter()


class BatchCallRequest(BaseModel):
    file_name: str


class QualifyOverrideRequest(BaseModel):
    bc_id: int


@router.post("/batch/start")
async def batch_start(
    background_tasks: BackgroundTasks,
    files:      List[UploadFile] = File(...),
    jd_text:    str = Form(...),
    job_title:  str = Form(default=''),
    opening_id: str = Form(default=''),
):
    if not jd_text.strip():
        raise HTTPException(400, "Job description is required.")

    candidates = []
    for file in files:
        entry = {
            "file_name":        file.filename or "unknown",
            "resume_text":      None,
            "name":             None,
            "email":            None,
            "phone":            None,
            "resume_score":     None,
            "analyze_result":   None,
            "filter_status":    "pending",
            "interview_id":     None,
            "interview_status": "pending",
            "interview_score":  None,
            "combined_score":   None,
            "callback_scheduled_at": None,
            "score_result":     None,
        }
        try:
            content = await file.read()
            entry["resume_text"] = extract_text(content, file.filename or "")
        except Exception as e:
            print(f"[Batch] Failed to parse {file.filename}: {e}")
            # Distinct from "filtered_out" — we never actually scored this file, so it
            # shouldn't look identical to a candidate who was reviewed and rejected.
            entry["filter_status"] = "parse_failed"
        candidates.append(entry)

    if all(c["resume_text"] is None for c in candidates):
        raise HTTPException(422, "No files could be parsed. Check that files are valid PDF or DOCX.")

    batch_id = str(uuid.uuid4())
    batch_store[batch_id] = {
        "batch_id":   batch_id,
        "opening_id": opening_id.strip() or None,
        "status":     "processing",
        "jd_text":    jd_text,
        "job_title":  job_title.strip() if job_title.strip() else extract_job_title(jd_text),
        "total":      len(candidates),
        "completed":  0,
        "candidates": candidates,
    }
    _save_batch(batch_id, batch_store[batch_id])
    background_tasks.add_task(_process_batch, batch_id)
    return {"batch_id": batch_id, "total": len(candidates), "status": "processing"}


@router.get("/batch/status/{batch_id}")
async def batch_status(batch_id: str):
    data = batch_store.get(batch_id)
    if not data:
        raise HTTPException(404, "Batch not found.")
    candidates_out = []
    for c in data["candidates"]:
        cd = {k: v for k, v in c.items() if k not in ("resume_text", "_batch_done")}
        iid = c.get("interview_id")
        if iid and iid in interview_store:
            iv = interview_store[iid]
            cd["call_log"] = iv.get("call_log", [])
            iv_status = iv.get("status")
            if iv_status and iv_status != "calling":
                cd["interview_status"]      = iv_status
                cd["fail_reason"]           = iv.get("fail_reason")
                cd["score_result"]          = iv.get("score_result")
                cd["transcript"]            = iv.get("transcript")
                cd["questions"]             = iv.get("questions")
                cd["callback_scheduled_at"] = iv.get("callback_scheduled_at")
                cd["processing_step"]       = iv.get("processing_step")
                if iv_status == "completed" and iv.get("score_result"):
                    iscore = _parse_interview_score(iv["score_result"])
                    if iscore is not None:
                        cd["interview_score"] = iscore
                        cd["combined_score"]  = _combined_score(c.get("resume_score"), iscore)
        # Derive interview_score from score_result if DB column is NULL (e.g. _sync_candidate_interview missed)
        if cd.get("score_result") and cd.get("interview_score") is None:
            iscore = _parse_interview_score(cd["score_result"])
            if iscore:
                cd["interview_score"] = iscore
                cd["combined_score"]  = _combined_score(c.get("resume_score"), iscore)
        candidates_out.append(cd)
    return {
        "batch_id":  data["batch_id"],
        "status":    data["status"],
        "total":     data["total"],
        "completed": data["completed"],
        "candidates": candidates_out,
    }


def _process_batch(batch_id: str):
    data = batch_store.get(batch_id)
    if not data:
        return

    jd_text    = data["jd_text"]
    candidates = data["candidates"]
    opening_id = data.get("opening_id")
    threshold  = get_qualification_threshold(opening_id)

    jd_fields = None
    if opening_id:
        opening = opening_store.get(opening_id)
        if opening:
            jd_fields = opening.get("jd_fields") or None

    progress_lock = threading.Lock()

    def analyze_one(idx):
        cand = candidates[idx]
        if not cand["resume_text"]:
            # Already marked "parse_failed" at upload time (batch_start) if that's why
            # resume_text is missing; set it here too so this path is self-consistent
            # regardless of how it was reached.
            cand["filter_status"] = "parse_failed"
        else:
            try:
                result    = analyze(cand["resume_text"], jd_text, jd_fields=jd_fields)
                score_str = result.get("match_score", "0 / 100")
                score_num = int(str(score_str).split("/")[0].strip())
                cand.update({
                    "analyze_result": result,
                    "name":           result.get("name"),
                    "email":          result.get("email"),
                    "phone":          result.get("phone"),
                    "resume_score":   score_num,
                    "filter_status":  "qualified" if score_num >= threshold else "filtered_out",
                })
            except Exception as e:
                print(f"[Batch] analyze failed for {cand['file_name']}: {e}")
                cand["filter_status"] = "filtered_out"

        if cand["filter_status"] == "qualified" and not cand.get("phone"):
            cand["filter_status"]    = "no_phone"
            cand["interview_status"] = "no_phone"

        # Persist + advance the live progress counter as each candidate finishes, rather
        # than only once at the very end — otherwise the progress bar sits at 0% the
        # whole time while the candidate list underneath it fills in row by row.
        with progress_lock:
            data["completed"] += 1
            _save_batch(batch_id, data)

    # Bounded concurrency — an unbounded thread-per-resume spawn fired every candidate's
    # Claude call at once regardless of batch size (200 resumes = 200 concurrent calls).
    with ThreadPoolExecutor(max_workers=min(len(candidates), 6)) as pool:
        list(pool.map(analyze_one, range(len(candidates))))

    data["status"] = "completed"
    _save_batch(batch_id, data)
    print(f"[Batch] Analysis complete — {len(candidates)} candidates ranked. Use Call button to interview.")


@router.put("/batch/candidates/qualify")
async def qualify_candidate_override(req: QualifyOverrideRequest):
    """Manually promote a filtered-out candidate to 'qualified' — e.g. a borderline
    score the recruiter still wants to call, or the resume-score threshold was tightened
    after the fact. Only affects candidates with a phone number (matches the existing
    'qualified implies callable' invariant used elsewhere)."""
    ok = _override_qualify_candidate(req.bc_id)
    if not ok:
        raise HTTPException(404, "Candidate not found, or has no phone number on file.")
    # Reflect immediately in any in-memory batch still being polled via /batch/status
    for b in batch_store.values():
        for c in b.get("candidates", []):
            if c.get("_bc_id") == req.bc_id:
                c["filter_status"] = "qualified"
    return {"status": "ok"}


@router.post("/batch/{batch_id}/interview/start")
async def batch_interview_start(batch_id: str, req: BatchCallRequest):
    data = batch_store.get(batch_id)
    if not data:
        raise HTTPException(404, "Batch not found.")

    cand = next((c for c in data["candidates"] if c["file_name"] == req.file_name), None)
    if not cand:
        raise HTTPException(404, "Candidate not found in batch.")
    if not cand.get("phone"):
        raise HTTPException(400, "Candidate has no phone number.")
    if not cand.get("resume_text"):
        raise HTTPException(400, "Resume text unavailable — re-upload this candidate's CV.")

    try:
        questions = generate_questions(cand["resume_text"], data["jd_text"])
    except Exception:
        questions = DEFAULT_QUESTIONS[:]

    interview_id = str(uuid.uuid4())
    interview_store[interview_id] = {
        "interview_id":          interview_id,
        "status":                "calling",
        "consent_status":        "pending",
        "consent_raw":           None,
        "consent_re_asked":      False,
        "callback_time_raw":     None,
        "callback_scheduled_at": None,
        "candidate_name":        cand.get("name"),
        "phone":                 cand["phone"],
        "questions":             questions,
        # In-memory only — seeds the Whisper vocabulary hint (see _vocab_for in interview.py)
        "resume_text":           cand.get("resume_text"),
        "jd_text":               data["jd_text"],
        "job_title":             data.get("job_title") or extract_job_title(data["jd_text"]),
        "recordings":            {},
        "transcriptions":        {},
        "repeat_counts":         {},
        "transcript":            None,
        "score_result":          None,
        "fail_reason":           None,
        "call_log":              [{"attempt": 1, "started_at": datetime.now().isoformat(), "status": "calling"}],
    }

    try:
        start_twilio_call(cand["phone"], interview_id)
    except Exception as e:
        del interview_store[interview_id]
        raise HTTPException(500, f"Failed to initiate call: {e}")

    cand["interview_id"]     = interview_id
    cand["interview_status"] = "calling"
    _save_interview(interview_id, interview_store[interview_id])
    _save_batch(batch_id, batch_store[batch_id])
    return {"interview_id": interview_id, "status": "calling"}


@router.get("/calls/active")
async def get_active_calls():
    """All candidates currently calling, in-progress, or processing — across all batches and pipelines."""
    from backend.app.database import _db_engine, _sql
    if not _db_engine:
        return {"calls": []}
    try:
        with _db_engine.connect() as conn:
            rows = conn.execute(_sql("""
                SELECT bc.id, bc.batch_id, bc.single_id, bc.file_name, bc.name,
                       bc.phone, bc.email, bc.resume_score, bc.filter_status,
                       bc.interview_id, bc.interview_status, bc.callback_scheduled_at,
                       COALESCE(b.job_title, jo.title)       AS job_title,
                       COALESCE(b.opening_id, bc.opening_id) AS opening_id
                FROM batch_candidates bc
                LEFT JOIN batches b       ON bc.batch_id = b.id
                LEFT JOIN job_openings jo ON jo.id = COALESCE(b.opening_id, bc.opening_id)
                -- Keep this literal in sync with DASHBOARD_ACTIVE_STATUSES (state.py) —
                -- the Python-side re-check below (after merging live interview_store data)
                -- uses that shared constant, this initial DB filter can't easily interpolate it.
                WHERE bc.interview_status IN ('calling', 'in_progress', 'processing', 'callback_scheduled')
                ORDER BY bc.updated_at DESC NULLS LAST
            """)).mappings().all()
    except Exception as e:
        print(f"[ActiveCalls] DB query failed: {e}")
        return {"calls": []}

    calls = []
    for row in rows:
        c = dict(row)
        iid = c.get("interview_id")
        iv  = interview_store.get(iid) if iid else None
        if iv:
            c["interview_status"] = iv.get("status", c["interview_status"])
            c["processing_step"]  = iv.get("processing_step")
            c["fail_reason"]      = iv.get("fail_reason")
            c["score_result"]     = iv.get("score_result")
            c["interview_score"]  = (iv.get("score_result") or {}).get("interview_score")
            c["name"]             = iv.get("candidate_name") or c.get("name")
        if c["interview_status"] in DASHBOARD_ACTIVE_STATUSES:
            calls.append(c)
    return {"calls": calls}
