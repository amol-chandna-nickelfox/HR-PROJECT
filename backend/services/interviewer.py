from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

import os
import re
import json
import httpx
import anthropic as _anthropic
from twilio.rest import Client as _TwilioClient

_claude_key   = os.getenv("CLAUDE_API_KEY")
claude_client = _anthropic.Anthropic(api_key=_claude_key, timeout=60.0) if _claude_key else None
CLAUDE_MODEL  = "claude-haiku-4-5-20251001"

# ── Twilio client (module-level) ──────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE       = os.getenv("TWILIO_PHONE_NUMBER")
twilio_client = _TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# ── Plivo client (module-level) ───────────────────────────────────────────────
try:
    import plivo as _plivo
    PLIVO_AUTH_ID    = os.getenv("PLIVO_AUTH_ID")
    PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
    PLIVO_PHONE      = os.getenv("PLIVO_PHONE_NUMBER")
    plivo_client = _plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN) if PLIVO_AUTH_ID else None
except ImportError:
    _plivo = None
    PLIVO_AUTH_ID = PLIVO_AUTH_TOKEN = PLIVO_PHONE = None
    plivo_client = None
    print("[Startup] plivo not installed — Plivo calls disabled. Run: pip install plivo")

from backend.app.state import settings_store


def _claude(system: str, prompt: str) -> str:
    resp = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,  
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def _strip_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw.rstrip())
    return raw.strip()


def generate_questions(resume_text: str, jd_text: str) -> list[str]:
    if not claude_client:
        return [
            "Could you start by introducing yourself and walking me through your background?",
            "What specifically draws you to this role and company?",
            "Tell me about a time you worked with others on a project — how did you contribute and what was the outcome?",
            "Can you explain how you've used one of your core technical skills in a real or academic project?",
            "The role has some specific technical requirements — can you talk about your familiarity with the key technologies mentioned?",
            "Tell me about a specific project you've worked on — what was a challenge you faced and how did you handle it?",
        ]

    prompt = f"""You are a recruiter conducting a structured phone screening. First, read the resume and determine if the candidate is a FRESHER/INTERN (student, recent graduate, little or no work experience) or EXPERIENCED (has significant work experience).

Then generate exactly 7 questions in this fixed order, adapted to their profile:

Question 1 — Pure Introduction:
Ask the candidate to simply introduce themselves — just who they are and what they do. Keep it open and warm. Do NOT ask about their journey or history here.
- Fresher example: "Could you start by telling me a little about yourself?"
- Experienced example: "Great to connect — could you start by telling me a bit about yourself?"

Question 2 — Background / Journey:
Now ask them to walk through their background in more depth.
- Fresher: Ask about their academic background, what they studied, and what drew them to this field.
- Experienced: Ask them to walk through their career journey and what has brought them to where they are today.

Question 3 — Motivation / Why this role:
Ask why they are interested in this specific role or company. Reference something specific from the JD.

Question 4 — Behavioural:
- Fresher: Ask about a time they collaborated in a team on an academic project, group assignment, or personal project — how they contributed and what the outcome was.
- Experienced: Ask about a notable achievement at work, a difficult situation they navigated, or how they adapted to a significant change. Pick whichever fits the resume best.

Question 5 — Technical (Resume-based):
- Fresher: Ask a concise technical question based on a skill, technology, or concept from their coursework or academic projects. Ask a how/why/what question, not just "tell me about X".
- Experienced: Ask a concise technical question grounded in a specific technology or experience from their work history. Intermediate depth.

Question 6 — Technical (JD-based):
Ask a concise technical question based on a specific requirement or technology from the JOB DESCRIPTION. Test whether they understand the concept, not just the name. Same depth for both profiles.

Question 7 — Project deep-dive:
- Fresher: Pick one specific academic or personal project from their resume (by name). Ask about a challenge they faced, what they built, or what they learned.
- Experienced: Pick one specific work project from their resume (by name). Ask about a technical challenge, how they solved it, or what they would do differently.

Rules:
- Each question must be a single, complete sentence — aim for under 20 words but NEVER cut a question mid-thought; it must always be grammatically complete and make full sense
- No multi-part questions — do not combine two questions into one with "and" or follow-up clauses
- Write in casual, spoken language — as if asking a friend, not writing a formal document
- Do NOT number the questions
- Do NOT mention "fresher" or "experienced" in the questions themselves
- Return a JSON array of exactly 7 question strings. Valid JSON only, no markdown.

RESUME:
{resume_text}

JOB DESCRIPTION:
{jd_text}"""

    raw = _claude("You are a recruiter conducting phone screenings. Return only valid JSON.", prompt)
    return json.loads(_strip_json(raw))


def _answer_path(provider: str, interview_id: str) -> str:
    """Answer-URL path for the active interview mode.

    streaming → the Stream-XML routes (backend/api/routes/stream.py) that hand
    the call to the Pipecat voice agent; legacy → the classic webhook flow.
    """
    if settings_store.get("interview_mode", "legacy") == "streaming":
        return f"/{provider}/stream-xml/{interview_id}"
    return f"/{provider}/start/{interview_id}"


def _start_twilio_call(phone_number: str, interview_id: str) -> dict:
    if not twilio_client or not TWILIO_PHONE:
        raise Exception("Twilio credentials missing — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER in .env")

    digits = re.sub(r"\D", "", phone_number)
    if len(digits) == 10:
        to = f"+91{digits}"
    elif digits.startswith("91") and len(digits) == 12:
        to = f"+{digits}"
    else:
        to = f"+{digits}"

    base_url = os.getenv("BASE_URL", "").rstrip("/")
    print(f"[Twilio] Calling {to}, interview_id={interview_id} (mode={settings_store.get('interview_mode', 'legacy')})")
    call = twilio_client.calls.create(
        to=to,
        from_=TWILIO_PHONE,
        url=f"{base_url}{_answer_path('twilio', interview_id)}",
        status_callback=f"{base_url}/twilio/status/{interview_id}",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        timeout=20,
        record=True,
        machine_detection="DetectMessageEnd",
        machine_detection_timeout=30,
        async_amd=True,
        async_amd_status_callback=f"{base_url}/twilio/amd/{interview_id}",
    )
    print(f"[Twilio] Call initiated — SID={call.sid}")
    return {"call_sid": call.sid}


def _start_plivo_call(phone_number: str, interview_id: str) -> dict:
    if not plivo_client or not PLIVO_PHONE:
        raise Exception("Plivo credentials missing — set PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_PHONE_NUMBER in .env")

    digits = re.sub(r"\D", "", phone_number)
    if len(digits) == 10:
        to = f"+91{digits}"
    elif digits.startswith("91") and len(digits) == 12:
        to = f"+{digits}"
    else:
        to = f"+{digits}"

    base_url = os.getenv("BASE_URL", "").rstrip("/")
    print(f"[Plivo] Calling {to}, interview_id={interview_id} (mode={settings_store.get('interview_mode', 'legacy')})")
    response = plivo_client.calls.create(
        from_=PLIVO_PHONE,
        to_=to,
        answer_url=f"{base_url}{_answer_path('plivo', interview_id)}",
        hangup_url=f"{base_url}/plivo/status/{interview_id}",
        ring_timeout=20,
        # Observe-only for now — Plivo's AMD false-positived on real pickups previously,
        # so /plivo/amd logs the signal but never acts on it.
        machine_detection="true",
        machine_detection_time=10000,
        machine_detection_url=f"{base_url}/plivo/amd/{interview_id}",
    )
    print(f"[Plivo] Call initiated — UUID={response.request_uuid}")
    return {"call_sid": response.request_uuid}


def start_twilio_call(phone_number: str, interview_id: str) -> dict:
    provider = settings_store.get("call_provider", "twilio")
    if provider == "plivo":
        if not plivo_client:
            raise Exception("Plivo credentials not configured — set PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_PHONE_NUMBER in .env")
        return _start_plivo_call(phone_number, interview_id)
    return _start_twilio_call(phone_number, interview_id)


# Exported so callers (the inline answer-loop transcription in interview.py/plivo.py) can
# recognize this exact marker and avoid caching it as final — see comment at those call sites.
HALLUCINATION_MARKER = "[unclear response — possible transcription error]"

# Whisper output language. Candidates speak English, Hindi, or Hinglish. This was previously
# hardcoded to "hi", which forced Whisper to render English speech phonetically in Devanagari
# — e.g. "and I requested my boss to give it to me" came back as
# "और आई रिक्वेस्टिड माइबॉस टो गिव इट टो मी" — unreadable and useless for scoring.
# "en" keeps output in readable English for English and Hinglish speech alike.
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en")

# Common words to keep out of the vocabulary hint — they carry no proper-noun signal and
# would just crowd out the useful terms inside Whisper's limited prompt budget.
_VOCAB_STOPWORDS = {
    "I", "A", "An", "The", "And", "But", "Or", "If", "In", "On", "At", "To", "For", "Of",
    "With", "My", "We", "You", "He", "She", "It", "They", "This", "That", "These", "Those",
    "What", "When", "Where", "Why", "How", "Who", "Which", "Can", "Could", "Would", "Should",
    "Tell", "Describe", "Explain", "Walk", "Yes", "No", "Please", "Thanks", "Hello", "Hi",
    "Job", "Role", "Work", "Team", "Time", "Year", "Years", "Company", "Experience",
}

# Capitalised words (Rahul, Python, NickelFox) and all-caps acronyms (NSUIT, AWS, API).
_VOCAB_TOKEN_RE = re.compile(r"\b(?:[A-Z]{2,}|[A-Z][a-zA-Z0-9+#.]{1,})\b")


def build_vocab_hint(*sources: str | None, max_terms: int = 40) -> str | None:
    """Build a Whisper prompt biasing recognition toward this candidate's proper nouns.

    Whisper reliably mangles domain/proper nouns it has no context for — an observed case was
    a candidate saying "NSUIT" being transcribed as "MSU IT". Seeding a short glossary drawn
    from the candidate's own resume/JD/name fixes those, verified against real call audio.

    Returns a closed, period-terminated list (never an unfinished sentence) so Whisper treats
    it as vocabulary rather than a prefix to continue — see the note at the prompt call site.
    """
    seen: list[str] = []
    for src in sources:
        if not src:
            continue
        for tok in _VOCAB_TOKEN_RE.findall(str(src)):
            if tok in _VOCAB_STOPWORDS or len(tok) < 2:
                continue
            if tok not in seen:
                seen.append(tok)
            if len(seen) >= max_terms:
                break
        if len(seen) >= max_terms:
            break
    if not seen:
        return None
    return "Glossary of names and terms that may appear: " + ", ".join(seen) + "."


# The scoring rubric's real caps. The prompt asks Claude for these ranges, but asking is not
# enforcing — an observed result came back with behavioral_quality 20/15 (133%), inflating the
# headline total by 5 points. These are the authority; whatever the model reports is clamped.
_SCORE_DIMENSIONS = (
    ("communication",      35),
    ("confidence",         30),
    ("motivation_fit",     20),
    ("behavioral_quality", 15),
)


def _normalize_score_result(result: dict) -> dict:
    """Clamp each dimension to its real maximum and recompute the headline total from the parts.

    Two problems this closes:
      * a dimension score above its cap (seen live: 20 out of a possible 15) silently inflated
        the total, so a candidate's score could exceed what the rubric allows;
      * "interview_score" was a string the model wrote independently of the dimensions, so the
        headline number and the breakdown could disagree with each other.

    The total is only rewritten when at least one dimension parsed as a number, so an explicit
    non-numeric result (e.g. "Unable to Score" on an empty transcript) is left intact.
    """
    if not isinstance(result, dict):
        return result
    total = 0
    parsed_any = False
    for key, cap in _SCORE_DIMENSIONS:
        d = result.get(key)
        d = d if isinstance(d, dict) else {}
        raw = d.get("score")
        try:
            score = int(round(float(raw)))
            parsed_any = True
        except (TypeError, ValueError):
            score = 0
        if score > cap:
            print(f"[Score] {key}={score} exceeds its maximum of {cap} — clamping")
            score = cap
        elif score < 0:
            score = 0
        result[key] = {"score": score, "max": cap}
        total += score
    if parsed_any:
        prev = str(result.get("interview_score", ""))
        result["interview_score"] = f"{total} / 100"
        if prev and prev.split("/")[0].strip() not in (str(total),):
            print(f"[Score] headline was {prev!r}; recomputed from dimensions as {total} / 100")
    return result


_LIVE_CALL_STATES = {"queued", "ringing", "in-progress", "in_progress"}


def is_call_active(call_id: str) -> bool | None:
    """Does the provider still consider this call live?

    The authoritative answer to "has this call ended", and the guard that stops a reconciliation
    sweep from declaring a call over while the candidate is still on the phone. Returns None
    when it cannot be determined, so callers must treat None as "unknown", never as "ended".
    """
    if not call_id:
        return None
    if twilio_client:
        try:
            return twilio_client.calls(call_id).fetch().status in _LIVE_CALL_STATES
        except Exception as e:
            print(f"[CallStatus] Twilio lookup failed for {call_id}: {e}")
    if plivo_client:
        try:
            plivo_client.live_calls.get(call_id)
            return True          # present among live calls ⇒ still active
        except Exception:
            return None          # absent or lookup failed — genuinely unknown
    return None


def list_answer_recordings(call_id: str) -> list[dict]:
    """Answer recordings the provider holds for a call, oldest first.

    Exists to recover answers the provider captured but never delivered to us. If a candidate
    hangs up right after finishing an answer, Twilio ends the <Record> and fires the
    call-status callback *instead of* the Record action URL — so the app never learns that
    recording's URL and the answer is lost even though the audio exists. Observed live: a
    58-second answer to the final question was stored as "[no recording]".

    Returns [{'sid','url','duration','created'}]. Never raises — a lookup failure just yields
    an empty list, leaving the interview exactly as it was.
    """
    out: list[dict] = []
    if not call_id:
        return out

    if twilio_client:
        try:
            for r in twilio_client.recordings.list(call_sid=call_id, limit=50):
                # RecordVerb = a <Record> answer. Excludes the full-call recording (OutboundAPI).
                if getattr(r, "source", "") != "RecordVerb":
                    continue
                out.append({
                    "sid":      r.sid,
                    "url":      f"https://api.twilio.com/2010-04-01/Accounts/{r.account_sid}/Recordings/{r.sid}",
                    "duration": int(getattr(r, "duration", 0) or 0),
                    "created":  getattr(r, "date_created", None),
                })
        except Exception as e:
            print(f"[Recover] Twilio recording lookup failed for {call_id}: {e}")

    # twilio_call_sid doubles as Plivo's CallUUID, so try Plivo when Twilio returned nothing.
    if not out and plivo_client:
        try:
            for r in plivo_client.recordings.list(call_uuid=call_id, limit=50):
                url = getattr(r, "recording_url", None)
                if not url:
                    continue
                out.append({
                    "sid":      getattr(r, "recording_id", None) or str(url).rstrip("/").split("/")[-1],
                    "url":      url,
                    "duration": int(float(getattr(r, "recording_duration_ms", 0) or 0) / 1000)
                                 or int(getattr(r, "duration", 0) or 0),
                    "created":  getattr(r, "add_time", None),
                })
        except Exception as e:
            print(f"[Recover] Plivo recording lookup failed for {call_id}: {e}")

    out.sort(key=lambda x: (x["created"] is None, x["created"]))
    return out


def transcribe_recording(recording_url: str, *, fast: bool = False, vocab: str | None = None) -> str:
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return "[transcription skipped — no GROQ_API_KEY set]"

    # Detect provider from the recording URL itself, NOT the global call_provider setting —
    # otherwise toggling the provider (or a callback re-dial on the other provider) between
    # when a recording is made and when it's transcribed would fetch with the wrong auth.
    if "plivo" in recording_url:
        auth = (PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
        url  = recording_url
    elif "twilio" in recording_url:
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        # Twilio requires .mp3 extension; ensure it's present without double-appending
        url  = recording_url if recording_url.endswith(".mp3") else recording_url + ".mp3"
    else:
        # Unknown host — fall back to the active provider's credentials
        provider = settings_store.get("call_provider", "twilio")
        if provider == "plivo":
            auth = (PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
            url  = recording_url
        else:
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            url  = recording_url if recording_url.endswith(".mp3") else recording_url + ".mp3"

    model = "whisper-large-v3-turbo" if fast else "whisper-large-v3"

    import time as _time
    last_err = None
    audio_resp = None
    for attempt in range(3):
        try:
            audio_resp = httpx.get(
                url, auth=auth, follow_redirects=True, timeout=30
            )
            if audio_resp.status_code == 404 and attempt < 2:
                print(f"[Transcribe] recording not ready (attempt {attempt+1}/3) — retrying")
                _time.sleep(2 ** attempt)
                continue
            audio_resp.raise_for_status()
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                print(f"[Transcribe] fetch error (attempt {attempt+1}/3): {e} — retrying")
                _time.sleep(2 ** attempt)
    if last_err:
        raise last_err

    from groq import Groq
    client = Groq(api_key=groq_key)

    # Transcribe (not translate) so the output stays verbatim — the translate task
    # paraphrases, which loses the candidate's actual wording. language=WHISPER_LANGUAGE
    # ("en" by default) keeps output in English even for Hinglish speech.
    kwargs = {}
    if vocab:
        # A short glossary of proper nouns relevant to THIS candidate (their name, college,
        # employers, technologies) — biases Whisper so e.g. "NSUIT" isn't heard as "MSU IT".
        # Deliberately a comma-separated list terminated with a period: the earlier
        # hallucination problem came from a prompt that ended mid-sentence (e.g. a dangling
        # "Candidate:" cue), which Whisper "continued" instead of transcribing. A closed list
        # gives it vocabulary without an unfinished thought to complete. The
        # _is_hallucinated_transcript() guard below still applies as a backstop.
        kwargs["prompt"] = vocab
    result = client.audio.transcriptions.create(
        file=("answer.mp3", audio_resp.content),
        model=model,
        language=WHISPER_LANGUAGE,
        temperature=0,
        **kwargs,
    )
    text = result.text.strip()
    if _is_hallucinated_transcript(text):
        print(f"[Transcribe] discarded likely-hallucinated output: {text[:120]!r}")
        return HALLUCINATION_MARKER
    return text


# Known Whisper hallucination signatures — Whisper was trained on YouTube subtitles, so
# silent/noisy audio segments often get filled with fluent-sounding but fabricated text
# echoing that training data (or, previously, our own prompt). Conservative on purpose:
# only trips on specific, well-documented patterns, not just "looks unusual."
_HALLUCINATION_PHRASES = [
    "thank you for watching", "thanks for watching", "subscribe to",
    "like and subscribe", "see you in the next video", "captions by", "subtitles by",
]
_CANDIDATE_ECHO_RE = re.compile(r"\bcandidate\s*(\d+(\.\d+)?|[a-z]\.)", re.IGNORECASE)


def _is_hallucinated_transcript(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    if any(phrase in lower for phrase in _HALLUCINATION_PHRASES):
        return True
    word_count = len(text.split())
    candidate_hits = len(_CANDIDATE_ECHO_RE.findall(text))
    if candidate_hits >= 2:
        return True
    # A single hit is only suspicious in a short response — a candidate legitimately
    # saying "candidate" once in a long, otherwise-coherent answer shouldn't be discarded.
    if candidate_hits >= 1 and word_count <= 12:
        return True
    # Degenerate repetition: the same short (2-4 word) phrase repeated 3+ times in a row
    # is a common Whisper "looping" signature on silence/noise, regardless of wording.
    words = lower.split()
    for n in (2, 3, 4):
        if len(words) < n * 3:
            continue
        for i in range(len(words) - n * 3 + 1):
            chunk = words[i:i + n]
            if chunk == words[i + n:i + 2 * n] == words[i + 2 * n:i + 3 * n]:
                return True
    return False


def score_interview(transcript: str, questions: list[str], jd_text: str) -> dict:
    if not claude_client or not transcript.strip():
        return {
            "interview_score":    "N/A",
            "communication":      {"score": 0, "max": 35},
            "confidence":         {"score": 0, "max": 30},
            "motivation_fit":     {"score": 0, "max": 20},
            "behavioral_quality": {"score": 0, "max": 15},
            "verdict":            "No Data",
            "strengths":          [],
            "improvements":       [],
            "summary":            "No transcript available to score.",
        }

    questions_text = "\n".join([f"{i+1}. {q}" for i, q in enumerate(questions)])

    prompt = f"""You are an experienced HR recruiter scoring a first-round phone screening interview.

This is an HR screening round — not a technical deep-dive. Score fairly and generously for natural, conversational answers.

Scoring dimensions and anchors:
- communication (0-35): Clarity, fluency, structure, professionalism.
  35 = exceptionally clear and polished | 25-30 = speaks clearly, professional tone | 15-24 = mostly understandable, some gaps | below 15 = hard to follow or very unprofessional
- confidence (0-30): Assertiveness, directness, absence of excessive hedging ("I think maybe", "I'm not sure", "kind of").
  28-30 = very direct and assured | 20-27 = generally confident with minor hedging | 10-19 = noticeable self-doubt | below 10 = very passive or hesitant throughout
- motivation_fit (0-20): Genuine interest in the role, understanding of the position, enthusiasm.
  18-20 = specific reasons, clear understanding of role | 12-17 = shows interest, general awareness | 6-11 = generic answers | below 6 = no apparent motivation or understanding
- behavioral_quality (0-15): Quality of situational/example answers — do they describe real situations with outcomes?
  13-15 = specific examples with clear outcomes | 8-12 = decent examples, outcome implied | 3-7 = vague or generic examples | 0-2 = no examples given

Important scoring rule: A competent, reasonably articulate candidate should score 72-82 overall. Only score below 50 if answers are clearly poor. Give benefit of the doubt for natural speech patterns. Do not penalize for being conversational rather than formal.

Interview Questions:
{questions_text}

Job Description (context):
{jd_text[:800]}

Full Interview Transcript:
{transcript}

Return this exact JSON (no markdown):
{{
  "interview_score": "XX / 100",
  "communication":      {{"score": 0-35, "max": 35}},
  "confidence":         {{"score": 0-30, "max": 30}},
  "motivation_fit":     {{"score": 0-20, "max": 20}},
  "behavioral_quality": {{"score": 0-15, "max": 15}},
  "verdict": "Strongly Recommended | Recommended | Consider | Not Recommended",
  "strengths":    ["strength 1", "strength 2", "strength 3"],
  "improvements": ["area 1", "area 2"],
  "summary": "2-3 sentence overall HR assessment of the candidate's screening performance"
}}"""

    raw = _claude("You are an experienced HR recruiter. Return only valid JSON.", prompt)
    return _normalize_score_result(json.loads(_strip_json(raw)))
