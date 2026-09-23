import os

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _SCHEDULER_OK = True
except ImportError:
    _scheduler = None
    _SCHEDULER_OK = False
    print("[Startup] apscheduler not installed — callback scheduling disabled. Run: pip install apscheduler")

interview_store: dict = {}
batch_store:     dict = {}
opening_store:   dict = {}
pipeline_store:  dict = {}   # {pipeline_id: pipeline_data}
opening_pipeline: dict = {}  # {opening_id: pipeline_id}  — one active pipeline per opening
settings_store:  dict = {
    "call_provider":  os.getenv("CALL_PROVIDER", "twilio"),
    # "legacy" = classic webhook flow (press # to finish an answer);
    # "streaming" = Pipecat voice agent with automatic end-of-answer detection.
    # Both seeded from env, then overridden by the DB-persisted value on startup.
    "interview_mode": os.getenv("INTERVIEW_MODE", "legacy"),
}

# ── Shared interview-status literal sets ──────────────────────────────────────
# Previously each of legacy Twilio, legacy Plivo, the streaming voice agent, and
# main.py's startup recovery independently wrote out its own tuple of "which statuses
# mean X" — and they'd quietly drifted apart (the streaming path included "declined" in
# its terminal-state guard, the legacy Twilio/Plivo status callbacks didn't; main.py's
# pipeline-recovery sweep didn't either, which orphaned a pipeline's "active" entry
# forever if the candidate declined during a server restart). Single source of truth now.

# "This interview's outcome is already decided" — guards against a status/AMD callback
# or the streaming pipeline's finalize step reprocessing an interview that's already
# been resolved one way or another.
TERMINAL_INTERVIEW_STATUSES = {
    "processing", "completed", "abandoned", "failed", "callback_scheduled", "declined",
}

# "A call is actively mid-flight, not yet resolved" — used by startup recovery to find
# crashed/orphaned interviews. Deliberately excludes callback_scheduled: a scheduled
# callback is a resolved, intentional wait state, not a stuck call.
ACTIVE_CALL_STATUSES = {"calling", "in_progress", "processing"}

# What counts as "show this on the live Active Calls dashboard" — active calls plus a
# pending callback (still something a recruiter should see as in-flight).
DASHBOARD_ACTIVE_STATUSES = ACTIVE_CALL_STATUSES | {"callback_scheduled"}

# "This interview reached some conclusion, or was actively in-flight when the server
# went down" — used by the pipeline-recovery sweep on restart to detect an "active"
# pipeline entry that needs reconciling (as opposed to one that was never dispatched).
RESOLVED_OR_ACTIVE_INTERVIEW_STATUSES = TERMINAL_INTERVIEW_STATUSES | ACTIVE_CALL_STATUSES

DEFAULT_QUALIFICATION_THRESHOLD = 70


def get_qualification_threshold(opening_id: str | None) -> int:
    """Resume-score cutoff for 'qualified' — per-opening if set, else the default."""
    if not opening_id:
        return DEFAULT_QUALIFICATION_THRESHOLD
    opening = opening_store.get(opening_id)
    if not opening:
        return DEFAULT_QUALIFICATION_THRESHOLD
    return opening.get("qualification_threshold", DEFAULT_QUALIFICATION_THRESHOLD)


DEFAULT_QUESTIONS = [
    "Tell me a bit about yourself and what brought you to apply for this role.",
    "What do you know about this position and why does it interest you?",
    "Can you walk me through a situation where you had to handle pressure or a tight deadline?",
    "Tell me about an achievement from your recent work that you're proud of.",
    "How do you prefer to communicate and collaborate with your team?",
    "Where do you see yourself growing in the next couple of years?",
]
