import os
import uuid
import html
import re
import json
import time
import threading
import traceback
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic as _anthropic
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from backend.services.interviewer import (
    generate_questions, transcribe_recording, score_interview,
    start_twilio_call, build_vocab_hint, list_answer_recordings,
    twilio_client, HALLUCINATION_MARKER,
)
from backend.utils.file_utils import extract_job_title
from backend.app.state import (
    interview_store, batch_store, DEFAULT_QUESTIONS, _scheduler, _SCHEDULER_OK,
    TERMINAL_INTERVIEW_STATUSES,
)
from backend.app.database import (
    _save_interview, _save_transcript_entries,
    _sync_candidate_interview, _link_single_candidate_interview,
    _load_interview, _save_interview_complete, _get_resume_text_for_interview,
)
from backend.app.callbacks import _trigger_callback_call

router = APIRouter()

_TRANSITIONS = [
    "Got it!",
    "Sure!",
    "Great!",
    "Perfect!",
    "Noted!",
]

REPEAT_KEYWORDS = [
    # English
    "repeat", "say that again", "again please", "pardon",
    "didn't hear", "didn't understand", "come again", "what was the question",
    "can you repeat", "please repeat",
    # Hindi transliterated
    "dobara", "phir se", "dobara puchiye", "dobara boliye",
    "samjha nahi", "suna nahi", "sunai nahi", "samajh nahi",
    "ek baar aur", "wapas", "phir bolo",
]

# Only match a keyword within the first few words of an utterance — otherwise a
# legitimate long answer that happens to use a word like "repeat" naturally
# (e.g. "...I had to repeat the QA cycle for every build...") gets
# misclassified as a repeat request. Genuine repeat requests are almost always
# at the very start of what the candidate says.
_REPEAT_KEYWORD_PREFIX_WORDS = 8

# Trailing-silence handling for answers (see the pause-nudge branch in twilio_answer).
# An answer this long that ends in silence is treated as finished — no "press #" nudge.
_LONG_ANSWER_SECS  = 25
# At most one nudge per question, so going quiet can never loop.
_PAUSE_NUDGE_MAX   = 1

# How long the post-call scoring pass waits for a straggling answer before giving up on it.
# The call is already over, so this only delays the score by seconds — and it is the difference
# between scoring 7 of 7 and publishing a wrong "6 of 7" that has to be corrected later.
_FINAL_ANSWER_WAIT_SECS = 25


def _looks_like_repeat_request(text: str) -> bool:
    words = text.lower().split()
    prefix = " ".join(words[:_REPEAT_KEYWORD_PREFIX_WORDS])
    return any(
        re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", prefix)
        for kw in REPEAT_KEYWORDS
    )


def _is_repeat_request(text: str) -> bool:
    claude_key = os.getenv("CLAUDE_API_KEY")
    if not claude_key:
        return False
    try:
        client = _anthropic.Anthropic(api_key=claude_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=5,
            system="You classify phone interview responses. Reply YES or NO only.",
            messages=[{"role": "user", "content":
                f"A job candidate on a phone screening gave this very short response.\n"
                f"Did they ask to repeat the question, or is it a real answer?\n\n"
                f'Response: "{text}"\n\n'
                f"Reply YES if they want to repeat the question, NO if it is a real answer."
            }],
        )
        result = resp.content[0].text.strip().upper()
        print(f"[Repeat] Claude classified='{result}' text='{text}'")
        return result.startswith("Y")
    except Exception as e:
        print(f"[Repeat] Claude classification failed: {e}")
        return False


def _vocab_for(data: dict, resume_text: str | None = None) -> str | None:
    """Whisper vocabulary hint for this specific candidate.

    Name first — getting the candidate's own name right matters most and it's the term
    Whisper is least likely to guess. Then the resume (richest source of college/employer/
    tech proper nouns), then the generated questions and JD, which were themselves derived
    from the resume and so echo its key terms. WHISPER_VOCAB_EXTRA covers recurring
    org-specific nouns (your company name, common local universities) that may not appear
    in any given candidate's own documents.
    """
    # Order is priority order — build_vocab_hint() caps the total term count, so earlier
    # sources win. Operator-curated WHISPER_VOCAB_EXTRA sits near the front deliberately:
    # it's hand-picked, whereas the tail sources (questions, JD prose) yield a lot of
    # incidental capitalised words that would otherwise crowd it out.
    return build_vocab_hint(
        data.get("candidate_name"),
        os.getenv("WHISPER_VOCAB_EXTRA"),
        resume_text or data.get("resume_text"),
        data.get("job_title"),
        " ".join(data.get("questions") or []),
        data.get("jd_text"),
    )


def _get_interview(interview_id: str) -> dict | None:
    data = interview_store.get(interview_id)
    if data:
        return data
    data = _load_interview(interview_id)
    if data:
        interview_store[interview_id] = data
        print(f"[Recovery] Reloaded interview {interview_id} from DB")
    return data


# ─── Twilio TwiML helpers (this file is Twilio-only — see plivo.py for the
# independent Plivo call flow; nothing in this file branches on provider) ────
def _say(text: str) -> str:
    return f"<Say voice='Google.en-IN-Neural2-A'>{text}</Say>"

def _answer_record(base_url: str, interview_id: str, q_idx: int) -> str:
    """The <Record> element used for every answer prompt.

    finishOnKey accepts a set of keys (Twilio docs): '#' submits the answer, '*' asks for the
    question to be repeated — the answer route distinguishes them via the Digits field.
    timeout='5' also ends the recording after 5s of silence at any point (including a pause
    after speech), so a candidate who simply stops talking is carried forward without pressing
    anything; the keys are the fast path, not the only path.

    recordingStatusCallback is the guarantee that an answer is never lost. It fires whenever
    the recording becomes available, independently of the action URL — which matters because a
    candidate who hangs up right after answering can leave the action URL un-requested, and
    then the answer exists only on Twilio's side. That happened live: a 58-second answer to the
    final question was stored as "[no recording]" and excluded from scoring. With this, the URL
    reaches us either way, and the last question is no more fragile than any other.
    """
    return (
        f"<Record"
        f"  action='{base_url}/twilio/answer/{interview_id}/{q_idx}'"
        f"  recordingStatusCallback='{base_url}/twilio/recording/{interview_id}/{q_idx}'"
        f"  recordingStatusCallbackMethod='POST'"
        f"  recordingStatusCallbackEvent='completed'"
        f"  maxLength='120' playBeep='true' finishOnKey='#*' timeout='5'"
        f"/>"
    )


def _gather(action: str, speech_timeout: int = 3, hints: str = "") -> str:
    """Opens a <Gather> — nest the actual question inside it via <Say>, then close with
    _gather_close(), rather than speaking the prompt beforehand and opening a bare Gather
    after.

    Twilio starts listening the instant the <Gather> element opens, i.e. while the nested
    prompt is still being read — not only once it finishes. That matters on a real phone line:
    there is a real, if small, dead-air gap while Twilio switches from playing audio to having
    its speech recognizer actually armed, and a Gather placed AFTER the prompt pays that gap on
    every single response. A candidate who answers immediately on cue can be missed entirely,
    which is what made a live "yes" show up as an empty SpeechResult. Nesting the prompt means
    that gap is paid once, at the very start of the whole exchange, not on the response Twilio
    most needs to catch.

    An earlier version of this app briefly moved to a bare post-prompt Gather specifically to
    stop a stray "Hello?" (said while the agent was still talking) from being misread as the
    candidate's actual answer. That's now handled independently by _is_non_answer() at the
    call site, regardless of Gather structure — so nesting the prompt back in here doesn't
    reopen that bug.

    hints biases Twilio's recognizer toward the words that actually matter for this prompt
    (comma-separated, e.g. "yes,no,haan,ji") — it doesn't restrict recognition to just these
    words, only weights them.
    """
    hints_attr = f" hints='{hints}'" if hints else ""
    return (
        f"<Gather input='speech' speechTimeout='{speech_timeout}' language='en-IN'{hints_attr} "
        f"action='{action}' method='POST'>"
    )

def _gather_close() -> str:
    return "</Gather>"


def _consent_record(base_url: str, interview_id: str) -> str:
    """<Record> used for consent capture — the ONLY consent capture mechanism now, first
    attempt and retry alike.

    Originally added only as a fallback after Twilio's real-time Gather speech recognizer
    returned an empty SpeechResult on a clear, audible "Yes." (confirmed by downloading and
    transcribing the actual call recording — the audio was fine, live ASR just missed it).
    Since the fallback succeeded every single time it was reached while the Gather attempt
    never once succeeded across every real test call, consent capture now goes straight to
    this — see twilio_start()'s comment.

    timeout='2': how many seconds of silence end the recording. Lowered from 4 — a one-word
    "yes" doesn't need that much trailing silence tolerance, and every extra second here is
    dead air the candidate sits through after they've already answered.
    maxLength='8': short ceiling since this only ever needs "yes"/"no" or a decline-and-
    reschedule reply, not a multi-minute answer.

    playBeep is deliberately 'true' — confirmed live: without it, a candidate hears silence
    right after the prompt, has no cue that anything is actually listening, and repeats
    themselves increasingly unsure whether they're being heard. The beep is the same audible
    confirmation already used for every real interview answer via _answer_record().
    """
    return (
        f"<Record"
        f"  action='{base_url}/twilio/consent/{interview_id}'"
        f"  maxLength='8' playBeep='true' finishOnKey='#' timeout='2'"
        f"/>"
    )

def _is_machine(form) -> bool:
    return form.get("AnsweredBy", "").startswith("machine")

def _terminate_call(call_sid: str):
    if twilio_client:
        twilio_client.calls(call_sid).update(status="completed")


# ─── Pydantic Models ──────────────────────────────────────────────────────────
class InterviewRequest(BaseModel):
    phone: str
    resume_text: str
    jd_text: str
    candidate_name: str | None = None
    job_title: str | None = None
    opening_id: str | None = None
    single_id: str | None = None


# ─── TwiML helpers ───────────────────────────────────────────────────────────
def _xml(content: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>\n{content}', media_type="application/xml; charset=utf-8")


def _hangup_xml() -> Response:
    return _xml("<Response><Hangup/></Response>")


# ─── Consent / Callback helpers ──────────────────────────────────────────────
_CONSENT_NO_WORDS = [
    # English
    "no", "nope", "nah", "not", "busy", "later", "bad time", "can't", "cannot", "different",
    # Hindi transliterated
    "nahi", "nhi", "nahin", "abhi nahi", "nahi ji",
]
_CONSENT_YES_WORDS = [
    # English
    "yes", "yeah", "yep", "sure", "okay", "ok", "good", "fine", "ready",
    "go ahead", "of course", "absolutely", "now", "perfect", "great",
    # Hindi transliterated — common affirmatives and fillers meaning yes
    "ha", "haan", "haa", "han", "hnji",
    "ji", "ji haan", "haan ji", "ji han",
    "bilkul", "zaroor", "acha", "accha", "theek", "theek hai", "theek h",
]

# Twilio Gather `hints` — weights recognition toward the words that actually matter for this
# prompt, without restricting it to only these. Only the callback-time question still uses
# <Gather>; consent moved to <Record> + Whisper entirely (see twilio_start()'s comment), so
# there's no equivalent consent-hints constant to keep here.
_CALLBACK_HINTS = ("today,tomorrow,morning,afternoon,evening,night,minutes,minute,hour,hours,"
                    "am,pm,baad,kal,shaam,subah,raat")

# Pure phone-answering noise: what someone says when they pick up, before they've processed
# the question. Deliberately excludes anything with a yes/no reading ("haan", "ji", "ok"),
# which _CONSENT_YES_WORDS already handles — a bare "haan" IS consent and must stay that way.
_NON_ANSWER_TOKENS = {
    "hello", "helo", "hallo", "hullo", "hlo", "hi", "hii", "hey", "yello",
    "namaste", "namaskar", "namaskaar", "salaam",
    "who", "whos", "this", "that", "there", "speaking", "calling", "is", "it", "am",
    "sir", "madam", "maam", "mam", "kaun", "kon", "kaha", "kahan", "se",
    "um", "uh", "umm", "uhh", "er", "erm", "mm", "mmm", "hmm", "hm", "a", "the",
}


def _is_non_answer(text: str) -> bool:
    """True when the utterance carries no consent signal at all — e.g. just "Hello?".

    Observed failure this guards against: a candidate picks up and says "Hello. Hello." while
    the (long) greeting is still playing. That got captured as their answer to "is now a good
    time?", contained no yes-word, and was therefore recorded as a DECLINE — the caller was
    then asked for a better time and the interview never ran, despite them being right there
    and willing. A no-signal utterance means "re-ask", not "declined".
    """
    stripped = re.sub(r"[^\w\s]", " ", (text or "").lower())
    words = [w for w in stripped.split() if w]
    if not words or len(words) > 8:
        return False
    # Any explicit yes/no reading means it IS a real answer — leave it to _detect_consent.
    for w in _CONSENT_YES_WORDS + _CONSENT_NO_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", stripped):
            return False
    return all(w in _NON_ANSWER_TOKENS for w in words)


def _detect_consent(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return False

    no_words  = _CONSENT_NO_WORDS
    yes_words = _CONSENT_YES_WORDS

    has_no  = any(re.search(r'\b' + re.escape(w) + r'\b', t) for w in no_words)
    has_yes = any(re.search(r'\b' + re.escape(w) + r'\b', t) for w in yes_words)

    if has_no and not has_yes:
        print(f"[Consent] keyword=NO  text='{text}'")
        return False
    if has_yes and not has_no:
        print(f"[Consent] keyword=YES text='{text}'")
        return True

    claude_key = os.getenv("CLAUDE_API_KEY")
    if claude_key:
        try:
            client = _anthropic.Anthropic(api_key=claude_key)
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=5,
                system="You classify spoken phone responses from job candidates in India. The candidate may respond in English, Hindi, or Hinglish. Reply with only the word YES or NO.",
                messages=[{"role": "user", "content":
                    f"A candidate was asked: \"Is this a good time for a job interview?\"\n"
                    f"They responded: \"{text}\"\n"
                    f"Are they agreeing to proceed? Hindi affirmatives: ha, haan, ji, bilkul, theek hai, acha, zaroor. Hindi negatives: nahi, nhi, na. Reply YES or NO only."
                }],
            )
            result = resp.content[0].text.strip().upper()
            print(f"[Consent] Claude sentiment='{result}' text='{text}'")
            return result.startswith("Y")
        except Exception as e:
            print(f"[Consent] Claude sentiment failed: {e}")

    print(f"[Consent] fallback has_no={has_no} has_yes={has_yes} text='{text}'")
    return has_yes and not has_no


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fortyfive": 45, "sixty": 60,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
}


def _parse_relative_time(raw: str) -> str | None:
    """Resolve "in 30 minutes" / "after half an hour" / "ek ghante baad" arithmetically.

    Claude handles absolute and vague times well, but on relative offsets it mangled the
    24-hour clock: from 17:36, "after half an hour" came back as 06:06 the NEXT DAY (18:06
    misread as 6:06 AM) on 3 of 4 attempts — a 12.5-hour error, and non-deterministic, so it
    couldn't be trusted or reproduced. Relative offsets are plain arithmetic, so they're
    computed here instead and never reach the model. Returns None for anything that isn't a
    recognisable relative offset, leaving absolute/vague phrasing to Claude.
    """
    if not raw:
        return None
    from datetime import timedelta
    t = re.sub(r"[^\w\s'.-]", " ", raw.lower())

    mins = None
    # "half an hour", "aadhe ghante", "adha ghanta"
    if re.search(r"\b(half\s+an?\s+hour|half\s+hour|aadh[ae]?\s*ghant[ae]|adh[ae]?\s*ghant[ae])\b", t):
        mins = 30
    # "quarter of an hour"
    elif re.search(r"\bquarter\s+(?:of\s+)?an?\s+hour\b", t):
        mins = 15
    if mins is None:
        # "<n> minutes" / "<n> min" / "<n> minute baad"
        m = re.search(r"\b(\d{1,3}|" + "|".join(map(re.escape, _WORD_NUMBERS)) + r")\s*"
                      r"(?:minute|minutes|minit|mins?)\b", t)
        if m:
            v = m.group(1)
            mins = int(v) if v.isdigit() else _WORD_NUMBERS.get(v)
    if mins is None:
        # "<n> hours" / "<n> ghante"
        m = re.search(r"\b(\d{1,2}|" + "|".join(map(re.escape, _WORD_NUMBERS)) + r")\s*"
                      r"(?:hour|hours|hrs?|ghant[aeo]|ghanto)\b", t)
        if m:
            v = m.group(1)
            n = int(v) if v.isdigit() else _WORD_NUMBERS.get(v)
            mins = n * 60 if n else None
    if mins is None:
        # bare "an hour" / "ek ghanta" with no number word matched above
        if re.search(r"\b(?:an|a|one|ek)\s+(?:hour|ghant[ae])\b", t):
            mins = 60
    if not mins or mins <= 0 or mins > 60 * 24 * 7:
        return None

    dt = datetime.now() + timedelta(minutes=mins)
    print(f"[CallbackTime] relative offset resolved locally: +{mins} min -> {dt.isoformat(timespec='seconds')}")
    return dt.isoformat(timespec="seconds")


def _parse_callback_time(raw: str) -> str | None:
    # Relative offsets are computed directly — see _parse_relative_time for why they are not
    # left to the model.
    _rel = _parse_relative_time(raw)
    if _rel:
        return _rel
    return _parse_callback_time_llm(raw)


def _parse_callback_time_llm(raw: str) -> str | None:
    claude_key = os.getenv("CLAUDE_API_KEY")
    if not claude_key:
        return None
    try:
        client = _anthropic.Anthropic(api_key=claude_key)
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(IST)
        prompt = (
            f"The candidate said: \"{raw}\"\n"
            f"Current date and time: {now.strftime('%A, %d %B %Y, %I:%M %p')} IST.\n\n"
            "Convert what they said into an exact ISO 8601 datetime (e.g. 2025-05-14T15:00:00).\n"
            "The candidate may speak in English, Hindi, or Hinglish. Handle all of these correctly:\n"
            "- Relative (English): 'after 30 minutes' → now + 30 min, 'in an hour' → now + 1 hour, 'in 2 hours' → now + 2 hours\n"
            "- Relative (Hindi): '10 minute baad' → now + 10 min, 'ek ghante baad' → now + 1 hour, 'do ghante baad' → now + 2 hours, 'adhe ghante baad' → now + 30 min\n"
            "- Today (English): 'today at 5pm' → today 17:00, 'tonight at 8' → today 20:00\n"
            "- Today (Hindi): 'aaj 5 baje' → today 17:00, 'aaj shaam ko' → today 18:00, 'aaj raat ko' → today 20:00\n"
            "- Named day (English): 'tomorrow at 3pm' → tomorrow 15:00, 'Friday at 2pm' → next Friday 14:00\n"
            "- Named day (Hindi): 'kal' → tomorrow, 'kal subah' → tomorrow 10:00, 'kal shaam' → tomorrow 18:00, 'kal 3 baje' → tomorrow 15:00\n"
            "- Vague (English): 'morning' → next day 10:00, 'afternoon' → next day 14:00, 'evening' → next day 18:00\n"
            "- Vague (Hindi): 'subah' → next day 10:00, 'dopahar' → next day 14:00, 'shaam' → next day 18:00, 'raat' → next day 20:00\n"
            "- Ambiguous hour (e.g. 'at 1', 'at 2', '2 baje', '3 baje' with no AM/PM): assume PM (13:00, 14:00, 15:00) during business hours (9am–8pm range). Only use AM if the candidate explicitly says AM or mentions midnight/early morning.\n"
            "Return ONLY the ISO 8601 string. If you truly cannot interpret it, return the word null."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=30,
            system="Return only an ISO 8601 datetime string or the word null. No explanation.",
            messages=[{"role": "user", "content": prompt}],
        )
        dt_str = resp.content[0].text.strip().strip('"\'')
        if dt_str.lower() == "null":
            return None
        datetime.fromisoformat(dt_str)
        return dt_str
    except Exception:
        return None



# Time-ish markers, English + transliterated Hindi. Used to decide whether a decline is worth
# asking Claude to parse as a callback time at all — without this gate, a plain "no, I'm busy
# right now" could be coerced into a datetime ("right now") and trigger an instant re-dial.
_TIME_HINT_RE = re.compile(
    r"(\d|"
    r"hour|hours|hr|hrs|min|mins|minute|minutes|"
    r"ghante|ghanta|ghante|baad|baje|"
    r"today|tonight|tomorrow|aaj|kal|"
    r"morning|afternoon|evening|night|subah|dopahar|shaam|raat|"
    # "pm" is never an ordinary word so it stands alone, but a bare "am" would match the verb
    # in "no I am busy" — so it only counts after a digit or written as "a.m.".
    r"noon|lunch|o'?clock|pm\b|\d\s*a\.?m\b|a\.m\.|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    re.I,
)


def _contains_time_hint(text: str) -> bool:
    return bool(text and _TIME_HINT_RE.search(text))


def _is_sane_callback_time(dt_str: str) -> bool:
    """Reject a parsed callback time that would misfire.

    Guards against two failure modes: a time in the past or seconds away (which would re-dial
    the candidate immediately, mid-conversation), and an absurdly distant one from a
    mis-parse. Anything from a minute out to 30 days ahead is accepted.
    """
    try:
        dt = datetime.fromisoformat(dt_str)
    except Exception:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    from datetime import timedelta
    return (now + timedelta(minutes=1)) <= dt <= (now + timedelta(days=30))


def _schedule_callback(interview_id: str, data: dict, dt_str: str, raw_text: str) -> str:
    """Persist + schedule a callback, returning a human-readable time for the spoken reply.

    Shared by the callback-time route and the consent branch (which schedules directly when
    the candidate already stated a time while declining). Saves BEFORE registering the
    APScheduler job so a failed save can't leave an orphaned job behind.
    """
    data["callback_time_raw"]     = raw_text
    data["callback_scheduled_at"] = dt_str
    data["status"]                = "callback_scheduled"
    call_log = data.get("call_log", [])
    if call_log:
        call_log[-1]["status"]                = "callback_scheduled"
        call_log[-1]["ended_at"]              = datetime.now().isoformat()
        call_log[-1]["callback_scheduled_at"] = dt_str
    _save_interview(interview_id, data)
    _sync_candidate_interview(interview_id, data)
    try:
        from backend.api.routes.pipeline import _on_pipeline_call_ended
        _on_pipeline_call_ended(interview_id, "callback")
    except Exception:
        pass
    if _SCHEDULER_OK:
        try:
            _scheduler.add_job(
                _trigger_callback_call, 'date',
                run_date=datetime.fromisoformat(dt_str), args=[interview_id],
                id=f"callback_{interview_id}", replace_existing=True,
                misfire_grace_time=3600,
            )
            print(f"[Callback] Scheduled {interview_id} at {dt_str}")
        except Exception as e:
            print(f"[Callback] Schedule failed: {e}")
    try:
        return datetime.fromisoformat(dt_str).strftime("%A at %I:%M %p")
    except Exception:
        return "the time you mentioned"


# Values that mean "no real answer here" — never treat these as a cached transcription, or a
# re-run can never recover an answer that was previously marked missing.
_ANSWER_PLACEHOLDERS = (
    "[no recording]",
    "[no answer provided]",
    "[declined to answer after repeat requests]",
)


def _is_real_answer(text) -> bool:
    t = (str(text) if text is not None else "").strip()
    if not t or t in _ANSWER_PLACEHOLDERS or t == HALLUCINATION_MARKER:
        return False
    return not t.startswith("[transcription error")


def _recording_sid(url) -> str:
    """Provider recording id from a recording URL — last path segment, extension stripped."""
    if not url:
        return ""
    tail = str(url).rstrip("/").split("/")[-1]
    return tail.split("?")[0].rsplit(".", 1)[0]


def _backfill_missing_recordings(interview_id: str, data: dict, total: int) -> int:
    """Recover answers the provider holds but never delivered via the answer webhook.

    A candidate who hangs up straight after answering causes the provider to fire the
    call-status callback instead of the <Record> action URL, so the app never learns that
    recording's URL — the answer is silently lost despite the audio existing (seen live: a
    58s answer to the final question stored as "[no recording]"). This asks the provider what
    recordings the call actually produced and fills the gaps.

    Pairing is deliberately conservative: recordings we don't already hold, ignoring sub-3s
    clips (the silence retries the answer route intentionally discards), matched in
    chronological order — and only when their count exactly equals the number of gaps. If it's
    ambiguous, nothing is changed and the reason is logged, because guessing which answer
    belongs to which question would be worse than leaving the gap.
    """
    recordings = data.setdefault("recordings", {})
    missing = [i for i in range(total)
               if not (recordings.get(i) or recordings.get(str(i)))]
    if not missing:
        return 0
    call_id = data.get("twilio_call_sid")
    if not call_id:
        print(f"[Recover] {interview_id}: {len(missing)} answer(s) missing but no call id to look them up")
        return 0

    fetched = list_answer_recordings(call_id)
    if not fetched:
        return 0
    known   = {_recording_sid(u) for u in recordings.values() if u}
    unknown = [r for r in fetched if r["sid"] not in known and r["duration"] >= 3]
    if not unknown:
        return 0

    # Not every unmatched recording is a lost answer: a repeat request or a too-short clip gets
    # superseded by the real answer and legitimately never stored, so it lingers as "unknown"
    # (a real call had one such 3s clip alongside the genuinely lost 58s answer).
    #
    # Gaps at the END of the interview are the hangup signature — the candidate answered and
    # dropped, so the NEWEST unmatched recordings are the missing tail answers. Pair those.
    # Otherwise only an exact count match is safe enough to act on.
    missing_is_tail = missing[-1] == total - 1 and missing == list(range(missing[0], total))
    if len(unknown) == len(missing):
        chosen = unknown
    elif missing_is_tail and len(unknown) > len(missing):
        chosen = unknown[-len(missing):]
    elif missing_is_tail and unknown:
        # Fewer recordings than gaps, but the gaps are a contiguous run to the end of the
        # interview: the candidate answered some questions and then dropped. Both lists are in
        # order, so recording k belongs to gap k — the remaining gaps are questions that were
        # asked but never answered. Bailing out here (the old behaviour) discarded real answers:
        # a call with four questions unanswered but one undelivered recording left that answer
        # out of the score entirely.
        chosen  = unknown
        missing = missing[:len(unknown)]
    else:
        print(f"[Recover] {interview_id}: found {len(unknown)} undelivered recording(s) but "
              f"{len(missing)} gap(s) (q{missing}) and they are not a trailing run — "
              f"ambiguous, leaving untouched")
        return 0

    # Guard the pairing: a recovered tail answer must be NEWER than every answer we already
    # hold, otherwise we'd be attaching an earlier discarded clip to a later question.
    known_times = [r["created"] for r in fetched if r["sid"] in known and r["created"]]
    if known_times and chosen[0]["created"] and chosen[0]["created"] < max(known_times):
        print(f"[Recover] {interview_id}: candidate recording predates an answer we already "
              f"have — refusing to guess, leaving q{missing} untouched")
        return 0

    for idx, rec in zip(missing, chosen):
        recordings[idx] = rec["url"]
        # Drop any placeholder so the transcription pass actually runs for this question.
        for key in (idx, str(idx)):
            if not _is_real_answer(data.get("transcriptions", {}).get(key)):
                data.get("transcriptions", {}).pop(key, None)
        print(f"[Recover] {interview_id}: recovered q{idx} from provider recording "
              f"{rec['sid'][:12]} ({rec['duration']}s)")
    return len(missing)


def _has_usable_answers(data: dict) -> bool:
    """True if this interview captured anything worth scoring.

    Counts either live transcriptions (streaming mode / inline transcription) or recording
    URLs still awaiting transcription (legacy mode) — a recording that hasn't been
    transcribed yet is still an answer, so it must not be treated as empty.
    """
    for v in (data.get("transcriptions") or {}).values():
        if v and v not in ("[no answer provided]", "[no recording]", HALLUCINATION_MARKER):
            return True
    return bool(data.get("recordings"))


def _missing_answer_indices(data: dict, total: int) -> list[int]:
    """Question indices with no real answer text — placeholders and errors count as missing."""
    tr = data.get("transcriptions") or {}
    return [i for i in range(total)
            if not _is_real_answer(tr.get(i, tr.get(str(i))))]


_repair_lock       = threading.Lock()
_repair_inflight   = set()
_REPAIR_MAX_TRIES  = 4


def repair_interview_answers(interview_id: str) -> bool:
    """Recover answers the provider holds that never made it into the score.

    `_process_interview` waits a few seconds for late webhooks, but three things still land an
    answer outside the score after scoring has finished:

    * Twilio's recordingStatusCallback for the FINAL answer arrives after the call-status
      callback that triggered scoring. The gap gets filled in `recordings`, but nothing
      re-transcribes it — so the dashboard shows "incomplete" while the audio sits in Twilio.
    * A transcription that errored. A Twilio recording 404s for a few seconds after the call
      ends, and the last answer's recording is only created at hangup, which is why question 7
      is the one that goes missing.
    * A gap the provider only reveals when queried, after the in-line backfill had given up.

    Re-transcribes only the gaps and only re-scores when a real answer was actually recovered,
    so a genuinely unanswered question stays unanswered. Never re-fires the pipeline hook —
    that already ran when the interview first completed.

    Returns True if the interview was re-scored.
    """
    with _repair_lock:
        if interview_id in _repair_inflight:
            return False
        _repair_inflight.add(interview_id)
    try:
        data = _get_interview(interview_id)
        if not data:
            return False
        if data.get("_processing_started"):
            return False                      # the main scoring pass owns it right now
        if data.get("status") != "completed":
            return False                      # not scored yet; the normal flow will handle it

        questions = data.get("questions") or []
        total     = len(questions)
        if not total:
            return False

        tries = int(data.get("_repair_tries") or 0)
        if tries >= _REPAIR_MAX_TRIES:
            return False

        missing = _missing_answer_indices(data, total)
        score_result = data.get("score_result") or {}
        if not missing:
            # Nothing is actually missing. If the row is still flagged incomplete from an
            # earlier pass, clear the flag so the dashboard stops contradicting the transcript.
            if score_result.get("incomplete"):
                score_result.pop("incomplete", None)
                score_result.pop("answered_count", None)
                score_result.pop("total_questions", None)
                data["score_result"] = score_result
                interview_store[interview_id] = data
                _save_interview_complete(interview_id, data)
                print(f"[Repair] {interview_id}: all {total} answers present — cleared the "
                      f"stale incomplete flag")
                return True
            return False

        data["_repair_tries"] = tries + 1

        # Ask the provider for anything it holds that we never received.
        try:
            _backfill_missing_recordings(interview_id, data, total)
        except Exception as e:
            print(f"[Repair] {interview_id}: provider lookup failed: {e}")

        recordings = data.get("recordings", {})
        targets = [i for i in missing if recordings.get(i) or recordings.get(str(i))]
        if not targets:
            print(f"[Repair] {interview_id}: q{missing} still have no recording at the provider "
                  f"— genuinely unanswered (attempt {tries + 1}/{_REPAIR_MAX_TRIES})")
            # Deliberately no DB write. Saving here would bump updated_at and keep the row
            # inside the sweep's recent-activity window forever, so a genuinely unanswered
            # interview would be re-examined every 15 minutes for good. Leaving updated_at
            # alone lets it age out of the window on its own.
            interview_store[interview_id] = data
            return False

        _vocab = _vocab_for(data, _get_resume_text_for_interview(interview_id))

        def _one(i):
            url = recordings.get(i) or recordings.get(str(i))
            try:
                return i, transcribe_recording(url, vocab=_vocab)
            except Exception as e:
                return i, f"[transcription error: {e}]"

        recovered = {}
        with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as pool:
            for fut in as_completed([pool.submit(_one, i) for i in targets]):
                i, text = fut.result()
                if _is_real_answer(text):
                    recovered[i] = text

        if not recovered:
            print(f"[Repair] {interview_id}: could not transcribe q{targets} "
                  f"(attempt {tries + 1}/{_REPAIR_MAX_TRIES}) — leaving the score as it stands")
            interview_store[interview_id] = data   # no DB write — see the note above
            return False

        data.setdefault("transcriptions", {}).update(recovered)
        print(f"[Repair] {interview_id}: recovered {len(recovered)} answer(s) "
              f"(q{sorted(recovered)}) — re-scoring")

        # Rebuild the transcript exactly as _process_interview does, so the re-score sees the
        # same shape of input.
        tr    = data["transcriptions"]
        lines = []
        if data.get("consent_raw"):
            lines.append("Interviewer: Is this a good time for the interview?\n"
                         f"Candidate: {data['consent_raw']}")
        for i, question in enumerate(questions):
            lines.append(f"Q{i+1}: {question}\n"
                         f"A{i+1}: {tr.get(i, tr.get(str(i), '[no recording]'))}")
        full_transcript = "\n\n".join(lines)

        try:
            new_score = score_interview(full_transcript, questions, data.get("jd_text", ""))
        except Exception as e:
            print(f"[Repair] {interview_id}: re-scoring failed ({e}) — keeping the old score but "
                  f"saving the recovered transcript")
            data["transcript"] = full_transcript
            interview_store[interview_id] = data
            _save_interview_complete(interview_id, data)
            return False

        still_missing = _missing_answer_indices(data, total)
        if still_missing:
            new_score["incomplete"]      = True
            new_score["answered_count"]  = total - len(still_missing)
            new_score["total_questions"] = total
        data["transcript"]   = full_transcript
        data["score_result"] = new_score
        interview_store[interview_id] = data
        if not _save_interview_complete(interview_id, data):
            print(f"[Repair] {interview_id}: save FAILED after re-scoring")
            return False
        print(f"[Repair] {interview_id}: re-scored on {total - len(still_missing)}/{total} "
              f"answers — {new_score.get('interview_score')}")
        return True
    except Exception as e:
        print(f"[Repair] {interview_id}: unexpected failure: {e}")
        traceback.print_exc()
        return False
    finally:
        with _repair_lock:
            _repair_inflight.discard(interview_id)


def _schedule_answer_repair(interview_id: str, delay: float = 25.0):
    """Run a repair pass shortly after the call, on a daemon thread.

    The delay lets every straggling recordingStatusCallback land first, so two late answers
    cause one re-score rather than two.
    """
    def _run():
        time.sleep(delay)
        repair_interview_answers(interview_id)

    threading.Thread(target=_run, daemon=True,
                     name=f"repair-{interview_id[:8]}").start()


def _unwedge_stuck_processing(interview_id: str, data: dict) -> str | None:
    """Recover an interview stranded in 'processing'.

    "processing" sits in BOTH ACTIVE_CALL_STATUSES and TERMINAL_INTERVIEW_STATUSES — the first
    because scoring is still in flight, the second because scoring must not be started twice.
    That combination deadlocks a crashed scoring pass: the reconciler picks the interview up as
    active, then the resolver returns immediately because it reads as terminal, so it sits at
    'processing' forever with no path out.

    Two distinct cases, distinguished by how far the pass got:
      * scored and transcribed already → only the status update was lost, so just finish it;
      * no score yet → the pass died mid-way, so clear the run guard and let it run again.
    Returns the new status, or None if this interview isn't actually stuck.
    """
    if data.get("status") != "processing":
        return None

    if data.get("score_result") and data.get("transcript"):
        data["status"] = "completed"
        call_log = data.get("call_log", [])
        if call_log and call_log[-1].get("status") == "processing":
            call_log[-1]["status"] = "completed"
            call_log[-1].setdefault("ended_at", datetime.now().isoformat())
        print(f"[Unwedge] {interview_id} was scored but stuck in 'processing' — marking completed")
        try:
            _save_interview(interview_id, data)
            _sync_candidate_interview(interview_id, data)
        except Exception as e:
            print(f"[Unwedge] save failed for {interview_id}: {e}")
        return "completed"

    print(f"[Unwedge] {interview_id} stuck in 'processing' with no score — re-running the scoring pass")
    data.pop("_processing_started", None)
    import threading as _th
    _th.Thread(target=_process_interview, args=(interview_id,), daemon=True).start()
    return "processing"


def _resolve_incomplete_interview(interview_id: str, data: dict, reason: str,
                                  empty_status: str = "abandoned",
                                  pipeline_outcome_if_empty: str = "no_answer") -> str:
    """Resolve a call that ended before reaching the closing message.

    Any call that captured at least one answer is SCORED on what it got, rather than being
    written off. Previously every early-exit path (startup stale sweep, the 2-hour stuck-call
    cleanup) just stamped the interview failed/abandoned and discarded the answers — so a
    candidate who hung up after answering 5 of 7 questions produced no score at all, and the
    recruiter had nothing to look at. _process_interview() already handles gaps (unanswered
    questions become "[no recording]"), and the score is tagged incomplete with the answered
    count so a dropped call is never mistaken for a weak one.

    Returns the status it settled on.
    """
    # A stuck 'processing' interview reads as terminal but isn't — recover it rather than
    # bailing out, which would leave it wedged permanently.
    _unwedged = _unwedge_stuck_processing(interview_id, data)
    if _unwedged:
        return _unwedged

    if data.get("status") in TERMINAL_INTERVIEW_STATUSES:
        return data["status"]

    if _has_usable_answers(data):
        data["status"]      = "processing"
        data["fail_reason"] = reason
        try:
            _save_interview(interview_id, data)
            _sync_candidate_interview(interview_id, data)
        except Exception as e:
            print(f"[Resolve] save before scoring failed for {interview_id}: {e}")
        print(f"[Resolve] {interview_id} ended early ({reason}) but has answers — scoring what we have")
        import threading as _th
        _th.Thread(target=_process_interview, args=(interview_id,), daemon=True).start()
        return "processing"

    # Nothing was captured — genuinely nothing to score.
    data["status"]      = empty_status
    data["fail_reason"] = reason
    call_log = data.get("call_log", [])
    if call_log:
        call_log[-1].setdefault("ended_at", datetime.now().isoformat())
        call_log[-1]["status"] = empty_status
    try:
        _save_interview(interview_id, data)
        _sync_candidate_interview(interview_id, data)
    except Exception as e:
        print(f"[Resolve] save failed for {interview_id}: {e}")
    print(f"[Resolve] {interview_id} ended early ({reason}) with no answers — {empty_status}")
    try:
        from backend.api.routes.pipeline import _on_pipeline_call_ended
        _on_pipeline_call_ended(interview_id, pipeline_outcome_if_empty)
    except Exception:
        pass
    return empty_status


# ─── Background: Transcribe + Score ──────────────────────────────────────────
def _process_interview(interview_id: str):
    data = interview_store.get(interview_id)
    if not data:
        return
    if data.get("_processing_started"):
        print(f"[Process] {interview_id} already processing — skipping duplicate")
        return
    data["_processing_started"] = True
    try:
        questions  = data["questions"]
        recordings = data.get("recordings", {})
        total      = len(questions)

        lines = []
        consent_raw = data.get("consent_raw")
        if consent_raw:
            lines.append(f"Interviewer: Is this a good time for the interview?\nCandidate: {consent_raw}")

        # Give any in-flight webhook a moment to land before deciding an answer is missing.
        # The provider's call-status callback routinely beats the final answer webhook and the
        # recording callback, so scoring immediately can grade a complete interview as partial:
        # a real call was scored 66/100 "6 of 7 answered", then re-scored to 69/100 with all
        # seven once the last answer arrived seconds later. Waiting here means the first score
        # is the right one instead of a wrong number that quietly corrects itself later.
        def _gaps() -> list[int]:
            # Read the live store entry rather than the dict captured when this thread started.
            # A webhook that runs while we wait can reload the interview from the DB and replace
            # the store entry; holding the old reference meant a late answer was written to a
            # dict this loop never looked at, so it stayed "missing" despite having arrived.
            live = interview_store.get(interview_id) or data
            rec  = live.get("recordings", {})
            return [i for i in range(total) if not (rec.get(i) or rec.get(str(i)))]

        # Wait for stragglers, and while waiting keep asking the provider whether the missing
        # audio has appeared. The final answer is the one that goes missing: Twilio only creates
        # its recording at hangup, so neither the Record action URL nor the recording callback
        # has necessarily delivered it by the time the call-status callback starts scoring.
        # Polling here means the first score is computed on the complete interview instead of a
        # wrong "6 of 7" that has to be corrected afterwards.
        if _gaps():
            for _tick in range(_FINAL_ANSWER_WAIT_SECS):
                time.sleep(1)
                if not _gaps():
                    print(f"[Process] {interview_id}: late answer(s) arrived while waiting — "
                          f"scoring the complete interview")
                    break
                # Every few seconds, ask the provider directly — the recording can exist there
                # before either webhook reaches us.
                if _tick and _tick % 5 == 0:
                    try:
                        live = interview_store.get(interview_id) or data
                        if _backfill_missing_recordings(interview_id, live, total):
                            print(f"[Process] {interview_id}: provider had the missing answer(s) "
                                  f"— recovered while waiting")
                            if not _gaps():
                                break
                    except Exception as e:
                        print(f"[Process] {interview_id}: provider poll failed: {e}")
            data = interview_store.get(interview_id) or data
            recordings = data.get("recordings", {})

        # Anything still missing may exist on the provider but never have been delivered —
        # a hangup right after an answer can skip the Record action URL entirely.
        try:
            if _backfill_missing_recordings(interview_id, data, total):
                recordings = data.get("recordings", {})
        except Exception as e:
            print(f"[Recover] backfill failed for {interview_id}: {e}")

        done_count = 0
        answers    = {}
        cached_transcriptions = data.get("transcriptions", {})

        # Post-call pass, so we can afford the extra query for the resume — it's the richest
        # source of the candidate's proper nouns (college, employers, technologies) for the
        # Whisper vocabulary hint.
        _resume_text = _get_resume_text_for_interview(interview_id)
        _vocab       = _vocab_for(data, _resume_text)

        def transcribe_one(i):
            # Reuse inline transcription if available (avoids duplicate Groq call). Placeholders
            # like "[no recording]" must NOT count as cached — otherwise a re-run could never
            # recover an answer that a previous pass failed to capture.
            cached = cached_transcriptions.get(i) or cached_transcriptions.get(str(i))
            if _is_real_answer(cached):
                return i, cached
            rec_url = recordings.get(i)
            if not rec_url:
                return i, "[no recording]"
            try:
                return i, transcribe_recording(rec_url, vocab=_vocab)
            except Exception as e:
                return i, f"[transcription error: {e}]"

        data["processing_step"] = f"Transcribing 0 / {total}"
        _save_interview(interview_id, data)

        with ThreadPoolExecutor(max_workers=min(total, 6)) as pool:
            futures = {pool.submit(transcribe_one, i): i for i in range(total)}
            for fut in as_completed(futures):
                i, answer = fut.result()
                answers[i] = answer
                done_count += 1
                data["processing_step"] = f"Transcribing {done_count} / {total}"
                _save_interview(interview_id, data)

        # Write all transcriptions (including any re-transcribed timed-out ones) back to
        # data so _save_transcript_entries gets the complete set
        data["transcriptions"].update(answers)

        for i, question in enumerate(questions):
            lines.append(f"Q{i+1}: {question}\nA{i+1}: {answers.get(i, '[no recording]')}")

        full_transcript = "\n\n".join(lines)

        data["processing_step"] = "Scoring interview…"
        _save_interview(interview_id, data)

        try:
            score_result = score_interview(full_transcript, questions, data["jd_text"])
        except Exception as e:
            score_result = {
                "interview_score":    "Error",
                "communication":      {"score": 0, "max": 35},
                "confidence":         {"score": 0, "max": 30},
                "motivation_fit":     {"score": 0, "max": 20},
                "behavioral_quality": {"score": 0, "max": 15},
                "verdict":            "Error",
                "strengths":          [],
                "improvements":       [],
                "summary":            f"Scoring failed: {e}",
            }

        # Annotate an incomplete screening so a dropped call is distinguishable from a
        # genuinely weak candidate. Both produce a low score, but for opposite reasons:
        # unanswered questions score 0, so a candidate who hung up after one answer looks
        # identical to one who answered everything badly. Recording the counts lets the UI
        # (and a human reading the row later) tell them apart.
        _answered = [
            i for i in range(total)
            if (answers.get(i) or "") not in
               ("", "[no recording]", "[no answer provided]", HALLUCINATION_MARKER)
            and not str(answers.get(i, "")).startswith("[transcription error")
        ]
        if len(_answered) < total:
            score_result["incomplete"]      = True
            score_result["answered_count"]  = len(_answered)
            score_result["total_questions"] = total
            print(f"[Process] {interview_id} scored on a PARTIAL interview — "
                  f"{len(_answered)}/{total} questions answered")
            # The missing answer may still be on its way: Twilio creates the last answer's
            # recording at hangup and its recordingStatusCallback routinely arrives after the
            # call-status callback that triggered this scoring pass. Come back for it rather
            # than leaving the row marked incomplete for an answer the provider already has.
            _needs_repair = True
        else:
            _needs_repair = False

        call_log = data.get("call_log", [])
        if call_log and call_log[-1].get("status") in ("processing", "calling"):
            call_log[-1]["status"] = "completed"
            call_log[-1].setdefault("ended_at", datetime.now().isoformat())

        interview_store[interview_id] = {
            **data,
            "status":       "completed",
            "transcript":   full_transcript,
            "score_result": score_result,
            "call_log":     call_log,
        }
        # Atomic: the interviews row, its transcript_entries, and the batch_candidates
        # sync all land in one transaction, or none do — previously these were three
        # separate connections/commits, so a failure partway through (e.g. the second
        # or third call) silently left interviews and batch_candidates out of sync
        # with only a log line, no way for anything downstream to notice or retry.
        if not _save_interview_complete(interview_id, interview_store[interview_id]):
            print(f"[Process] Final save FAILED for {interview_id} — interview data not persisted to DB "
                  f"(in-memory interview_store still has the completed result; a server restart before "
                  f"the next successful save would lose it)")

        try:
            from backend.api.routes.pipeline import _on_pipeline_call_ended
            _on_pipeline_call_ended(interview_id, "completed")
        except Exception:
            pass

        if _needs_repair:
            _schedule_answer_repair(interview_id)
    finally:
        # Always clear the in-progress guard so force_resolve can retry on crash
        data.pop("_processing_started", None)
        iv = interview_store.get(interview_id)
        if iv:
            iv.pop("_processing_started", None)


class _QuestionsRequest(BaseModel):
    resume_text: str
    jd_text: str


# ─── Interview Routes ─────────────────────────────────────────────────────────
@router.post("/interview/questions")
async def get_interview_questions(req: _QuestionsRequest):
    try:
        questions = generate_questions(req.resume_text, req.jd_text)
    except Exception:
        questions = DEFAULT_QUESTIONS[:]
    return {"questions": questions}


@router.post("/interview/start")
async def start_interview(req: InterviewRequest):
    if not req.phone.strip(): raise HTTPException(400, "Phone number is required.")
    interview_id = str(uuid.uuid4())
    try:
        questions = generate_questions(req.resume_text, req.jd_text)
    except Exception as e:
        print(f"[generate_questions error] {e}")
        questions = DEFAULT_QUESTIONS[:]

    interview_store[interview_id] = {
        "interview_id":          interview_id,
        "opening_id":            req.opening_id,
        "status":                "calling",
        "consent_status":        "pending",
        "consent_raw":           None,
        "callback_time_raw":     None,
        "callback_scheduled_at": None,
        "candidate_name":        req.candidate_name,
        "phone":                 req.phone,
        "questions":             questions,
        # In-memory only (no DB column) — used to build the Whisper vocabulary hint so the
        # candidate's own proper nouns (college, employers, tech) transcribe correctly.
        "resume_text":           req.resume_text,
        "jd_text":               req.jd_text,
        "job_title":             (req.job_title.strip() if req.job_title and req.job_title.strip() else extract_job_title(req.jd_text)),
        "recordings":            {},
        "transcriptions":        {},
        "repeat_counts":         {},
        "transcript":            None,
        "score_result":          None,
        "call_log":              [{"attempt": 1, "started_at": datetime.now().isoformat(), "status": "calling"}],
    }

    try:
        start_twilio_call(req.phone, interview_id)
    except Exception as e:
        del interview_store[interview_id]
        raise HTTPException(500, f"Failed to initiate call: {e}")

    _save_interview(interview_id, interview_store[interview_id])
    if req.single_id:
        _link_single_candidate_interview(req.single_id, interview_id)
    return {"interview_id": interview_id, "call_id": interview_id, "status": "calling", "questions": questions}


@router.get("/interview/status/{interview_id}")
async def interview_status(interview_id: str):
    if interview_id not in interview_store:
        raise HTTPException(404, "Interview not found.")
    return {k: v for k, v in interview_store[interview_id].items() if k != "jd_text"}


@router.get("/interview/stream/{interview_id}")
async def interview_stream(interview_id: str):
    async def _generator():
        while True:
            data = interview_store.get(interview_id)
            if not data:
                yield 'data: {"status":"not_found"}\n\n'
                break
            payload = {k: v for k, v in data.items() if k != "jd_text"}
            yield f"data: {json.dumps(payload)}\n\n"
            if data["status"] in ("completed", "abandoned", "failed", "callback_scheduled", "declined"):
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/interview/recall/{interview_id}")
async def recall_interview(interview_id: str):
    data = interview_store.get(interview_id)
    if not data:
        raise HTTPException(404, "Interview session not found — server may have restarted.")

    existing_log = data.get("call_log", [])
    data.update({
        "status":                "calling",
        "consent_status":        "pending",
        "consent_raw":           None,
        "consent_re_asked":      False,
        "recordings":            {},
        "transcriptions":        {},
        "transcript":            None,
        "score_result":          None,
        "fail_reason":           None,
        "callback_time_raw":     None,
        "callback_scheduled_at": None,
        "_processing_started":   False,
        "call_log":              existing_log + [{"attempt": len(existing_log) + 1, "started_at": datetime.now().isoformat(), "status": "calling"}],
    })

    try:
        start_twilio_call(data["phone"], interview_id)
    except Exception as e:
        raise HTTPException(500, f"Failed to initiate call: {e}")

    _save_interview(interview_id, data)
    return {"interview_id": interview_id, "status": "calling"}


@router.post("/interview/force-resolve/{interview_id}")
async def force_resolve_interview(interview_id: str, background_tasks: BackgroundTasks):
    data = interview_store.get(interview_id)
    if not data:
        raise HTTPException(404, "Interview not found.")
    if data["status"] in ("completed", "processing"):
        _sync_candidate_interview(interview_id, data)  # fix any stale batch_candidates row
        return {"interview_id": interview_id, "status": data["status"], "message": "Already resolved."}

    recordings = data.get("recordings", {})
    if recordings:
        data["status"] = "processing"
        _save_interview(interview_id, data)
        _sync_candidate_interview(interview_id, data)
        background_tasks.add_task(_process_interview, interview_id)
        return {"interview_id": interview_id, "status": "processing", "message": "Processing started."}
    else:
        data["status"]      = "abandoned"
        data["fail_reason"] = "Force resolved — no recordings found"
        _save_interview(interview_id, data)
        _sync_candidate_interview(interview_id, data)
        return {"interview_id": interview_id, "status": "abandoned", "message": "Marked as abandoned — no recordings."}


@router.get("/callbacks/due")
async def callbacks_due():
    now = datetime.now().isoformat()
    due = []
    for iid, iv in interview_store.items():
        if iv.get("status") != "callback_scheduled":
            continue
        scheduled = iv.get("callback_scheduled_at")
        if scheduled and scheduled <= now:
            due.append({
                "interview_id":          iid,
                "candidate_name":        iv.get("candidate_name"),
                "phone":                 iv.get("phone"),
                "job_title":             iv.get("job_title"),
                "callback_scheduled_at": scheduled,
                "callback_time_raw":     iv.get("callback_time_raw"),
            })
    return {"due": due}


# ─── Twilio TwiML Routes ──────────────────────────────────────────────────────
@router.api_route("/twilio/start/{interview_id}", methods=["GET", "POST"])
async def twilio_start(interview_id: str):
    data = _get_interview(interview_id)
    if not data:
        return _hangup_xml()

    COMPANY_NAME = os.getenv("COMPANY_NAME", "NickelFox Technologies")
    name        = data.get("candidate_name") or "there"
    safe_name   = html.escape(name.split()[0])
    safe_title  = html.escape(data.get("job_title", "the open position"))
    safe_co     = html.escape(COMPANY_NAME)
    base_url    = os.getenv("BASE_URL", "").rstrip("/")
    data["consent_status"] = "pending"

    greeting_text = (
        f"Hello, could I please speak with {safe_name}? "
        f"<break time='500ms'/>"
        f"Hi {safe_name}! This is Sarah calling from the HR team at <phoneme alphabet='ipa' ph='nɪkəlfɒks'>NickelFox</phoneme> Technologies. "
        f"I'm reaching out regarding your application for the {safe_title} role. "
        f"<break time='300ms'/>"
    )
    _screen_msg = "I'd like to conduct a brief screening round — it should only take about 5 to 7 minutes. Would now be a good time?"
    # Consent capture uses <Record> + Whisper from the very FIRST attempt, not <Gather>'s live
    # speech recognition. Confirmed across every real test call so far: Twilio's live Gather
    # ASR missed a clear, audible "yes" 100% of the time on this call path — nesting the
    # prompt inside the Gather and adding recognition hints didn't change that. The retry
    # (<Record> + transcribe_recording(), the same mechanism already proven reliable for every
    # real interview answer) worked every single time it was reached instead. Rather than pay
    # for one attempt that has never once worked before falling back to the one that always
    # does, the reliable path is now the only path.
    return _xml(
        f"<Response>"
        f"{_say(greeting_text)}"
        f"{_say(_screen_msg)}"
        f"{_consent_record(base_url, interview_id)}"
        f"<Redirect method='POST'>{base_url}/twilio/consent/{interview_id}</Redirect>"
        f"</Response>"
    )


@router.api_route("/twilio/consent/{interview_id}", methods=["GET", "POST"])
async def twilio_consent(
    interview_id: str,
    SpeechResult: str = Form(default=None),
    RecordingUrl: str = Form(default=None),
):
    data = _get_interview(interview_id)
    if not data:
        return _hangup_xml()

    base_url  = os.getenv("BASE_URL", "").rstrip("/")
    name      = data.get("candidate_name") or "there"
    safe_name = html.escape(name.split()[0])
    questions = data["questions"]
    total     = len(questions)
    safe_q0   = html.escape(questions[0])

    transcript = SpeechResult or ""
    if not transcript and RecordingUrl:
        # Twilio's own webhook response limit is ~15s (same constraint documented at the
        # inline-transcription call in the answer route). Calling transcribe_recording()
        # unbounded here risked exactly that: if the recording briefly 404s (it can, right
        # after <Record> finishes) and the retry backoff plus Groq latency stacks up, Twilio
        # gives up waiting and just ends the call — silently, with no TwiML ever received —
        # before our "you're accepted, let's begin" response is even built. Bounding this the
        # same way the answer route already does means we always respond in time; a real
        # timeout (rare — this clip is at most 8s of audio) falls through as an empty
        # transcript rather than leaving Twilio to hang up on its own.
        #
        # fast=True (whisper-large-v3-turbo) rather than the standard model used for real
        # interview answers: measured ~2x faster on real consent audio (0.85s vs 1.64s avg)
        # with an identical transcription result. That accuracy/speed trade-off is fine for
        # detecting "yes"/"no" in a few words — it's the interview answers themselves, where
        # verbatim wording actually gets scored, that need the more careful model.
        _result_holder = [None]
        def _transcribe_bg():
            try:
                _result_holder[0] = transcribe_recording(RecordingUrl, fast=True)
            except Exception as e:
                print(f"[Consent] transcription failed: {e}")
        _t = threading.Thread(target=_transcribe_bg, daemon=True)
        _t.start()
        _t.join(timeout=10)
        if _t.is_alive():
            print(f"[Consent] interview={interview_id} transcription still running after 10s "
                  f"— responding now rather than risking Twilio's own webhook timeout")
        transcript = _result_holder[0] or ""

    print(f"[Consent] interview={interview_id} transcript='{transcript}'")
    data["consent_raw"] = transcript

    # A greeting-only reply ("Hello?") is not a decline — the candidate simply hasn't answered
    # the question yet, usually because they picked up mid-greeting. Re-ask instead.
    _non_answer = _is_non_answer(transcript)
    if _non_answer:
        print(f"[Consent] no consent signal in {transcript!r} — treating as 'not answered yet', re-asking")

    if not transcript.strip() or _non_answer:
        if not data.get("consent_re_asked"):
            data["consent_re_asked"] = True
            _save_interview(interview_id, data)
            _reask_lead = (
                f"Sorry {safe_name}, I don't think you caught that — no problem at all! "
                f"I'm calling about your job application, and I'd like to ask you a few quick questions."
            )
            _reask_question = "Is now a good time? Just say yes if you're ready, or no if you'd prefer another time."
            return _xml(
                f"<Response>"
                f"{_say(_reask_lead)}"
                f"{_say(_reask_question)}"
                f"{_consent_record(base_url, interview_id)}"
                f"<Redirect method='POST'>{base_url}/twilio/consent/{interview_id}</Redirect>"
                f"</Response>"
            )
        else:
            data["status"]         = "failed"
            data["fail_reason"]    = "No response during consent check"
            data["consent_status"] = "declined"
            _save_interview(interview_id, data)
            _sync_candidate_interview(interview_id, data)
            try:
                from backend.api.routes.pipeline import _on_pipeline_call_ended
                _on_pipeline_call_ended(interview_id, "no_answer")
            except Exception:
                pass
            return _hangup_xml()

    if _detect_consent(transcript):
        data["consent_status"] = "accepted"
        _save_interview(interview_id, data)
        accepted_text = (
            f"Thank you, {safe_name} — I appreciate you taking the time. "
            f"<break time='200ms'/>"
            f"Just a quick heads up — I'll ask you {total} questions. When you're done answering, press the # key to move ahead, "
            f"or just pause for a few seconds and I'll move on by myself. "
            f"If you'd like me to repeat a question, press the star key — or just say repeat. Take as much time as you need. "
            f"<break time='400ms'/>"
            f"Alright, let's get started! "
            f"<break time='400ms'/>"
            f"Here's my first question — "
            f"<break time='300ms'/>"
            f"{safe_q0}"
        )
        return _xml(
            f"<Response>"
            f"{_say(accepted_text)}"
            f"{_answer_record(base_url, interview_id, 0)}"
            f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/0</Redirect>"
            f"</Response>"
        )
    else:
        data["consent_status"] = "declined"
        _save_interview(interview_id, data)

        # People usually decline WITH a time attached — "can you call me after half an hour".
        # Asking again for a time they just gave wasted their answer and invited a mis-parse:
        # one real call had the candidate say "call me after half an hour", then the follow-up
        # question captured the agent's own example phrasing ("tomorrow morning would be
        # perfect") and booked the callback ~17 hours later than requested. So if the decline
        # already contains a usable time, schedule it now and skip the second question.
        if _contains_time_hint(transcript):
            _dt = _parse_callback_time(transcript)
            if _dt and _is_sane_callback_time(_dt):
                readable = _schedule_callback(interview_id, data, _dt, transcript)
                print(f"[Consent] declined WITH a time — scheduling from the consent reply: {_dt}")
                _confirm_msg = (
                    f"No problem at all — I'll call you back {html.escape(readable)}. "
                    f"Thanks so much for your time, and talk to you then!"
                )
                return _xml(
                    f"<Response>"
                    f"{_say(_confirm_msg)}"
                    f"<Hangup/>"
                    f"</Response>"
                )
            print(f"[Consent] decline mentioned a time but it wasn't parseable — asking explicitly")

        # No usable time in the decline — ask for one. Deliberately WITHOUT concrete example
        # times: any echo of our own prompt back into the recording would otherwise supply a
        # parseable time the candidate never said (the failure described above).
        declined_lead     = "Of course, completely understandable!"
        declined_question = "What day and time would work better for you?"
        return _xml(
            f"<Response>"
            f"{_say(declined_lead)}"
            f"{_gather(f'{base_url}/twilio/callback-time/{interview_id}', speech_timeout=4, hints=_CALLBACK_HINTS)}"
            f"{_say(declined_question)}"
            f"{_gather_close()}"
            f"<Redirect method='POST'>{base_url}/twilio/callback-time/{interview_id}</Redirect>"
            f"</Response>"
        )


@router.api_route("/twilio/callback-time/{interview_id}", methods=["GET", "POST"])
async def twilio_callback_time(
    interview_id: str,
    SpeechResult: str = Form(default=None),
    RecordingUrl: str = Form(default=None),
):
    try:
        data = _get_interview(interview_id)
        if not data:
            return _hangup_xml()

        raw_time = SpeechResult or ""
        if not raw_time and RecordingUrl:
            try:
                raw_time = transcribe_recording(RecordingUrl)
                print(f"[CallbackTime] interview={interview_id} transcribed='{raw_time}'")
            except Exception as e:
                print(f"[CallbackTime] transcription failed: {e}")

        print(f"[CallbackTime] interview={interview_id} raw='{raw_time}'")

        data["callback_time_raw"] = raw_time
        dt_str = _parse_callback_time(raw_time) if raw_time else None
        # Reject a parse that lands in the past/seconds away or absurdly far out — better to
        # fall through to the declined path than to re-dial mid-call or in three months.
        if dt_str and not _is_sane_callback_time(dt_str):
            print(f"[CallbackTime] parsed time {dt_str} failed the sanity check — discarding")
            dt_str = None
        call_log = data.get("call_log", [])

        if dt_str:
            readable = _schedule_callback(interview_id, data, dt_str, raw_time)
            _callback_msg = f"Perfect! We'll give you a call back on {html.escape(readable)}. Thanks so much for your time today — have a wonderful day!"
            return _xml(
                f"<Response>"
                f"{_say(_callback_msg)}"
                f"<Hangup/>"
                f"</Response>"
            )
        else:
            data["status"]      = "declined"
            data["fail_reason"] = "Candidate declined to schedule a callback"
            if call_log:
                call_log[-1]["status"]   = "declined"
                call_log[-1]["ended_at"] = datetime.now().isoformat()
            _save_interview(interview_id, data)
            _sync_candidate_interview(interview_id, data)
            try:
                from backend.api.routes.pipeline import _on_pipeline_call_ended
                _on_pipeline_call_ended(interview_id, "declined")
            except Exception:
                pass
            return _xml(
                f"<Response>"
                f"{_say('No problem at all — we appreciate your time. If you change your mind, feel free to reach out to us. Have a wonderful day!')}"
                f"<Hangup/>"
                f"</Response>"
            )
    except Exception as _e:
        print(f"[TwiML] twilio_callback_time unhandled error for {interview_id}: {_e}")
        _tech_err_msg = "We're having a technical issue. We'll call you back shortly. Goodbye!"
        return _xml(
            f"<Response>"
            f"{_say(_tech_err_msg)}"
            f"<Hangup/>"
            f"</Response>"
        )


@router.api_route("/twilio/answer/{interview_id}/{q_idx}", methods=["GET", "POST"])
async def twilio_answer(
    interview_id:      str,
    q_idx:             int,
    background_tasks:  BackgroundTasks,
    request:           Request,
    RecordingUrl:      str = Form(default=None),
    RecordingDuration: str = Form(default=None),
    CallSid:           str = Form(default=None),
    Digits:            str = Form(default=None),
):
    try:
        data = _get_interview(interview_id)
        if not data:
            return _hangup_xml()

        questions    = data["questions"]
        total        = len(questions)
        base_url     = os.getenv("BASE_URL", "").rstrip("/")
        rec_url_val  = RecordingUrl
        call_sid_val = CallSid
        duration     = int(RecordingDuration or "0")

        print(f"[Twilio answer] interview={interview_id} q={q_idx}/{total-1} duration={duration}s digits={Digits!r}")

        # Proof of life for the reconciliation sweep: it ages an interview from this, so a long
        # interview that is still progressing is never mistaken for an abandoned call.
        data["_last_activity_at"] = datetime.now().isoformat()

        if q_idx >= total:
            print(f"[Twilio answer] q_idx={q_idx} out of bounds (total={total}) — hanging up")
            return _hangup_xml()

        if call_sid_val:
            data["twilio_call_sid"] = call_sid_val

        # '*' = "repeat the question", handled before any transcription so it's instant and
        # deterministic. Saying "repeat" aloud still works, but that path depends on Whisper
        # returning matchable text within the 12s inline window — and if the transcript comes
        # back in the wrong script or the transcription times out, the request is missed and
        # the candidate instead gets nudged to press #. A DTMF key can't be misheard.
        if Digits and "*" in Digits:
            repeat_count = data["repeat_counts"].get(q_idx, 0)
            if repeat_count < 2:
                data["repeat_counts"][q_idx] = repeat_count + 1
                data["status"] = "calling"
                print(f"[Twilio answer] '*' pressed q={q_idx} count={repeat_count+1}/2 — re-asking immediately")
                safe_q      = html.escape(questions[q_idx])
                _star_msg   = f"Sure, here it is again. <break time='300ms'/>{safe_q}"
                return _xml(
                    f"<Response>"
                    f"{_say(_star_msg)}"
                    f"{_answer_record(base_url, interview_id, q_idx)}"
                    f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/{q_idx}</Redirect>"
                    f"</Response>"
                )
            print(f"[Twilio answer] '*' pressed q={q_idx} but repeat limit reached — prompting for an answer")
            _star_cap_msg = "That was the last repeat for this one — please share whatever you can after the beep."
            return _xml(
                f"<Response>"
                f"{_say(_star_cap_msg)}"
                f"{_answer_record(base_url, interview_id, q_idx)}"
                f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/{q_idx}</Redirect>"
                f"</Response>"
            )

        if rec_url_val and not data["transcriptions"].get(q_idx):
            # NOTE: deliberately does NOT set status="processing" here. This block can run for
            # up to 12s (inline transcription), and "processing" is in TERMINAL_INTERVIEW_STATUSES
            # — meaning "the call is over and scoring has begun". If the candidate hangs up
            # mid-answer, Twilio's call-ended callback can land inside this window, see
            # "processing", conclude scoring is already underway and return without doing
            # anything. This block then resets status to "calling" and the interview is orphaned:
            # never scored, stuck until a restart sweep marks it failed — silently discarding a
            # partial interview that should have been scored on the answers it did get.
            # The genuine "processing" transition happens only on the all-questions-done path
            # below, which actually schedules _process_interview().
            data["processing_step"] = "Transcribing your answer…"

            # Truly silent or near-silent (Whisper hallucinates on < 3s clips)
            # Also catches rapid # presses — finishOnKey does NOT bypass the minimum duration
            if duration < 3:
                # Silence AFTER we already captured speech for this question means the
                # candidate has finished — not that they said nothing. Commit what we have and
                # fall through to advance. Without this, a nudged answer went through three
                # "that was too short to capture" rounds before the pending parts were
                # recovered, which is what made going quiet feel broken.
                _pending_now = data.get("_pending_answer_parts", {}).get(q_idx)
                if _pending_now:
                    print(f"[Twilio answer] silence after speech q={q_idx} — treating answer as complete")
                    data["transcriptions"][q_idx] = " ".join(_pending_now)
                    data["_pending_answer_parts"].pop(q_idx, None)
                    data["recordings"][q_idx] = rec_url_val
                else:
                    retries = data["repeat_counts"].get(q_idx, 0) + 1
                    data["repeat_counts"][q_idx] = retries
                    if retries < 3:
                        print(f"[Twilio answer] silence q={q_idx} retry={retries}/3")
                        _silence_msg = "Hmm, that was too short to capture. Please share your answer after the beep and press the # key when you're finished."
                        return _xml(
                            f"<Response>"
                            f"{_say(_silence_msg)}"
                            f"{_answer_record(base_url, interview_id, q_idx)}"
                            f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/{q_idx}</Redirect>"
                            f"</Response>"
                        )
                    print(f"[Twilio answer] silence q={q_idx} max retries — marking no answer")
                    data["transcriptions"][q_idx] = "[no answer provided]"
                    # Record the (short) URL too so an all-silent interview still has a
                    # non-empty recordings dict — otherwise the status callback misclassifies
                    # a fully-reached interview as "abandoned".
                    data["recordings"][q_idx] = rec_url_val

            else:
                # Transcribe first — repeat check must happen before pause hint
                quick_text = None
                try:
                    import threading as _th
                    _result_holder = [None]
                    def _transcribe_bg():
                        try:
                            _result_holder[0] = transcribe_recording(rec_url_val, fast=False, vocab=_vocab_for(data))
                        except Exception as _te:
                            print(f"[Twilio answer] transcription error: {_te}")
                    _t = _th.Thread(target=_transcribe_bg, daemon=True)
                    _t.start()
                    _t.join(timeout=12)  # truly returns after 12s — daemon thread finishes in background
                    quick_text = _result_holder[0]
                    if quick_text:
                        print(f"[Twilio answer] q={q_idx} transcript: {quick_text[:100]!r}")
                    else:
                        print(f"[Twilio answer] q={q_idx} transcription timed out or failed")
                except Exception as te:
                    print(f"[Twilio answer] inline transcription failed: {te}")

                is_repeat = bool(quick_text) and _looks_like_repeat_request(quick_text)

                # Claude fallback: only for very short responses to avoid false positives
                if not is_repeat and quick_text and len(quick_text.split()) < 8:
                    is_repeat = _is_repeat_request(quick_text)

                _repeat_cap_reached = False
                if is_repeat:
                    repeat_count = data["repeat_counts"].get(q_idx, 0)
                    if repeat_count < 2:
                        data["repeat_counts"][q_idx] = repeat_count + 1
                        data["status"] = "calling"
                        print(f"[Twilio answer] repeat detected q={q_idx} count={repeat_count+1}/2 — re-asking")
                        safe_q = html.escape(questions[q_idx])
                        _repeat_msg = f"Of course, happy to repeat that! <break time='400ms'/>{safe_q}"
                        return _xml(
                            f"<Response>"
                            f"{_say(_repeat_msg)}"
                            f"{_answer_record(base_url, interview_id, q_idx)}"
                            f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/{q_idx}</Redirect>"
                            f"</Response>"
                        )
                    else:
                        # Move on WITHOUT recording the repeat-request text itself as the
                        # candidate's answer — that would unfairly tank their score.
                        print(f"[Twilio answer] repeat limit reached q={q_idx} — moving on without scoring the repeat request")
                        data["recordings"][q_idx] = rec_url_val
                        data["transcriptions"][q_idx] = "[declined to answer after repeat requests]"
                        is_repeat = False
                        _repeat_cap_reached = True

                # Spoke but didn't press # and transcription isn't a repeat.
                # Twilio's <Record timeout='5'> already ended this recording on trailing
                # silence, so reaching here means the candidate spoke and then stopped. That
                # is usually "finished", occasionally "pausing to think". Two guards keep the
                # nudge from turning a finished answer into a 20s interrogation about the #
                # key (previously it fired unconditionally and uncapped, so going quiet cost
                # one nudge plus three "too short to capture" rounds before advancing):
                #   - never nudge an answer already long enough to be self-evidently complete
                #   - never nudge the same question twice
                # When either guard blocks it, control falls through to the normal
                # commit-and-advance path below, which stitches _pending_answer_parts in.
                _nudges = data.setdefault("_pause_nudge_counts", {}).get(q_idx, 0)
                if (not _repeat_cap_reached and duration > 6 and not Digits and duration < 118
                        and duration < _LONG_ANSWER_SECS
                        and _nudges < _PAUSE_NUDGE_MAX):
                    data["_pause_nudge_counts"][q_idx] = _nudges + 1
                    print(f"[Twilio answer] spoke then paused q={q_idx} ({duration}s) — nudging once for #")
                    # Save this segment instead of silently discarding it — a candidate who
                    # pauses mid-answer (e.g. to think through a technical question) would
                    # otherwise lose everything said before the pause, since a fresh Record
                    # starts for the same question and only the last segment used to survive.
                    if quick_text and quick_text != HALLUCINATION_MARKER:
                        pending = data.setdefault("_pending_answer_parts", {})
                        pending.setdefault(q_idx, []).append(quick_text)
                    _pause_msg = "Take your time — press the # key when you're finished, or star to hear the question again."
                    return _xml(
                        f"<Response>"
                        f"{_say(_pause_msg)}"
                        f"{_answer_record(base_url, interview_id, q_idx)}"
                        f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/{q_idx}</Redirect>"
                        f"</Response>"
                    )

                if not _repeat_cap_reached:
                    data["recordings"][q_idx] = rec_url_val
                    # Don't cache a hallucination-flagged result as final — treat it the same
                    # as an inline-transcription timeout (quick_text=None) and leave it unset so
                    # _process_interview()'s background pass gets a genuine, unhurried second
                    # attempt at this recording (which may also be more fully processed/available
                    # by then than during the live call's 12s inline window).
                    if quick_text and quick_text != HALLUCINATION_MARKER:
                        # Merge in any earlier segments saved before a mid-answer pause.
                        pending = data.get("_pending_answer_parts", {}).pop(q_idx, None)
                        data["transcriptions"][q_idx] = " ".join(pending + [quick_text]) if pending else quick_text

            if q_idx + 1 < total:
                data["status"] = "calling"
            _save_interview(interview_id, data)
            _save_transcript_entries(interview_id, data)

        next_q = q_idx + 1
        if next_q < total:
            print(f"[Twilio answer] advancing to q={next_q}")
            safe_next_q = html.escape(questions[next_q])
            transition  = _TRANSITIONS[next_q % len(_TRANSITIONS)]
            midpoint    = " We're halfway through — you're doing brilliantly! <break time='300ms'/>" if next_q == total // 2 else ""
            _next_msg = f"{transition}{midpoint} <break time='400ms'/>{safe_next_q}"
            return _xml(
                f"<Response>"
                f"{_say(_next_msg)}"
                f"{_answer_record(base_url, interview_id, next_q)}"
                f"<Redirect method='POST'>{base_url}/twilio/answer/{interview_id}/{next_q}</Redirect>"
                f"</Response>"
            )
        else:
            print(f"[Twilio answer] all questions done — closing call")
            data["status"] = "processing"
            background_tasks.add_task(_process_interview, interview_id)
            _closing_msg = "That's all my questions for today — you did a wonderful job! It was genuinely lovely speaking with you. Our team will be in touch very soon. Wishing you a brilliant rest of your day — take care!"
            return _xml(
                f"<Response>"
                f"{_say(_closing_msg)}"
                f"<Hangup/>"
                f"</Response>"
            )

    except Exception as e:
        print(f"[Twilio answer] UNHANDLED ERROR at q={q_idx}: {e}")
        traceback.print_exc()
        _ans_err_msg = "Oh, I'm so sorry — we seem to have hit a small technical hiccup. Thank you so much for your time today, and we'll be in touch soon. Take care!"
        return _xml(
            f"<Response>"
            f"{_say(_ans_err_msg)}"
            f"<Hangup/>"
            f"</Response>"
        )


@router.api_route("/twilio/recording/{interview_id}/{q_idx}", methods=["GET", "POST"])
async def twilio_recording_callback(
    interview_id:      str,
    q_idx:             int,
    RecordingUrl:      str = Form(default=None),
    RecordingSid:      str = Form(default=None),
    RecordingStatus:   str = Form(default=None),
    RecordingDuration: str = Form(default=None),
):
    """Recording-available callback — the safety net that keeps an answer from being lost.

    Fires independently of the answer route whenever Twilio finishes storing a recording. This
    exists because a candidate who hangs up straight after answering can leave the <Record>
    action URL un-requested, so the app never learns the URL and the answer is dropped even
    though the audio exists (observed: a 58s answer to the final question scored as
    "[no recording]").

    Deliberately does nothing but fill a gap: it never overwrites an answer already captured,
    never advances the interview, and never returns TwiML — the normal answer webhook stays in
    charge of the call flow.
    """
    data = _get_interview(interview_id)
    if not data:
        return {"status": "ok"}
    if RecordingStatus and RecordingStatus != "completed":
        return {"status": "ok"}
    if not RecordingUrl:
        return {"status": "ok"}

    try:
        duration = int(RecordingDuration or "0")
    except ValueError:
        duration = 0
    # Sub-3s clips are the silence retries the answer route intentionally discards; storing one
    # would overwrite nothing but could mask a real answer arriving later.
    if duration and duration < 3:
        return {"status": "ok"}

    recordings = data.setdefault("recordings", {})
    if recordings.get(q_idx) or recordings.get(str(q_idx)):
        return {"status": "ok"}   # answer webhook already captured it — nothing to do

    recordings[q_idx] = RecordingUrl
    # Clear any placeholder so the scoring pass actually transcribes this answer.
    for key in (q_idx, str(q_idx)):
        if not _is_real_answer(data.get("transcriptions", {}).get(key)):
            data.get("transcriptions", {}).pop(key, None)
    print(f"[Recording] q{q_idx} of {interview_id} captured via recording callback "
          f"({duration}s) — the answer webhook had not stored it")
    try:
        _save_interview(interview_id, data)
    except Exception as e:
        print(f"[Recording] save failed for {interview_id}: {e}")

    # If scoring already finished, storing the URL alone changes nothing the dashboard can see:
    # the answer is still untranscribed and the row still reads "incomplete". This callback is
    # the common case for the final question — Twilio only creates that recording at hangup, so
    # it regularly lands after the call-status callback has triggered scoring.
    if data.get("status") == "completed":
        print(f"[Recording] {interview_id} was already scored — scheduling a repair pass "
              f"to transcribe q{q_idx} and re-score")
        _schedule_answer_repair(interview_id)
    return {"status": "ok"}


@router.post("/twilio/status/{interview_id}")
async def twilio_status_callback(interview_id: str, request: Request, background_tasks: BackgroundTasks):
    form        = await request.form()
    call_status = form.get("CallStatus", "")
    print(f"[Twilio status] interview={interview_id} CallStatus={call_status}")

    data = _get_interview(interview_id)
    if not data:
        return {"status": "ok"}

    terminal_call = {"completed", "no-answer", "busy", "failed", "canceled", "cancel"}
    if call_status not in terminal_call:
        return {"status": "ok"}
    if data["status"] in TERMINAL_INTERVIEW_STATUSES:
        return {"status": "ok"}

    recordings = data.get("recordings", {})
    # Streaming-mode interviews (voice_agent.py) capture answers as live text with no
    # per-question recordings — count either as evidence the candidate answered.
    answers    = {k: v for k, v in data.get("transcriptions", {}).items()
                  if v and v != "[no answer provided]"}
    call_log   = data.get("call_log", [])
    now_iso    = datetime.now().isoformat()
    if call_status in ("no-answer", "busy"):
        data["status"]      = "failed"
        data["fail_reason"] = "Call not answered" if call_status == "no-answer" else "Candidate's line was busy"
        if call_log:
            call_log[-1].update({"status": "failed", "ended_at": now_iso, "fail_reason": data["fail_reason"]})
    elif len(recordings) == 0 and len(answers) == 0:
        data["status"]      = "abandoned"
        data["fail_reason"] = "Candidate disconnected before answering any question"
        if call_log:
            call_log[-1].update({"status": "abandoned", "ended_at": now_iso})
    else:
        data["status"] = "processing"
        if call_log:
            call_log[-1]["status"] = "processing"
        background_tasks.add_task(_process_interview, interview_id)

    _save_interview(interview_id, data)
    _sync_candidate_interview(interview_id, data)

    if data["status"] in ("failed", "abandoned"):
        try:
            from backend.api.routes.pipeline import _on_pipeline_call_ended
            _on_pipeline_call_ended(interview_id, "no_answer")
        except Exception:
            pass

    return {"status": "ok"}


def _human_is_engaged(data: dict) -> bool:
    """True when we have positive evidence a real person is on this call.

    Twilio's AMD runs asynchronously for up to machine_detection_timeout (30s) and
    fires *concurrently* with the live interview. Because this app's greeting is long
    (~20-30s of TTS) and the candidate is mostly silent while listening, AMD's
    DetectMessageEnd heuristic reliably misreads a real pickup as a voicemail
    greeting — observed as AnsweredBy=machine_end_other on a call where the candidate
    had already answered the consent question with "Yes.". Acting on that hung up on a
    live human mid-interview.

    So AMD is only trusted while we have no evidence of a human. Once the candidate
    has said anything at all, AMD is ignored for the rest of the call. This keeps the
    genuine benefit (skipping actual voicemails, which never respond) without the
    false-positive killing real interviews.
    """
    if data.get("consent_status") == "accepted":
        return True
    if (data.get("consent_raw") or "").strip():
        return True
    if data.get("recordings"):
        return True
    if any(v for v in (data.get("transcriptions") or {}).values()):
        return True
    return False


@router.post("/twilio/amd/{interview_id}")
async def twilio_amd_callback(interview_id: str, request: Request):
    """Async AMD callback — fires when Twilio detects an answering machine."""
    form     = await request.form()
    call_sid = form.get("CallSid", "")
    answered_by = form.get("AnsweredBy", "")
    print(f"[AMD] interview={interview_id} AnsweredBy={answered_by}")

    # Only act on machine detections — human means the call continues normally
    if not _is_machine(form):
        return {"status": "ok"}

    data = _get_interview(interview_id)
    if not data:
        return {"status": "ok"}

    # Already in a terminal state (status callback beat us here)
    if data["status"] in TERMINAL_INTERVIEW_STATUSES:
        return {"status": "ok"}

    # A human has demonstrably responded — AMD is a false positive, ignore it.
    if _human_is_engaged(data):
        print(f"[AMD] interview={interview_id} AnsweredBy={answered_by} but candidate has "
              f"already responded (consent={data.get('consent_status')}) — ignoring as a "
              f"false positive, call continues")
        return {"status": "ok"}

    # Observe-only by default. Twilio's AMD window (machine_detection_timeout, 30s) runs
    # concurrently with our greeting, and the greeting alone is ~20-30s of TTS — so AMD can
    # reach its verdict before the candidate has had a chance to say anything, which means
    # _human_is_engaged() above can't always save a real pickup. Rather than risk hanging up
    # on live candidates (observed in practice), AMD is logged but not acted on, matching the
    # same decision already made for Plivo (see plivo.py's /plivo/amd). Set
    # TWILIO_AMD_HANGUP=true to re-enable termination once AMD is proven reliable on your
    # numbers — the _human_is_engaged() guard above still applies when it is enabled.
    if os.getenv("TWILIO_AMD_HANGUP", "false").lower() not in ("true", "1", "yes"):
        print(f"[AMD] interview={interview_id} AnsweredBy={answered_by} — observe-only "
              f"(TWILIO_AMD_HANGUP not enabled), call continues")
        return {"status": "ok"}

    # Hang up the live call
    if call_sid:
        try:
            _terminate_call(call_sid)
        except Exception as e:
            print(f"[AMD] Failed to hang up call {call_sid}: {e}")

    data["status"]      = "failed"
    data["fail_reason"] = "Voicemail detected — call not answered by a person"
    call_log = data.get("call_log", [])
    if call_log:
        call_log[-1].update({"status": "failed", "fail_reason": data["fail_reason"]})
    _save_interview(interview_id, data)
    _sync_candidate_interview(interview_id, data)

    try:
        from backend.api.routes.pipeline import _on_pipeline_call_ended
        _on_pipeline_call_ended(interview_id, "no_answer")
    except Exception:
        pass

    return {"status": "ok"}
