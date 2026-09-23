import os
import html
import re
import threading as _th
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, Request, Form

from backend.services.interviewer import transcribe_recording, plivo_client, HALLUCINATION_MARKER
from backend.app.database import _save_interview, _save_transcript_entries, _sync_candidate_interview
from backend.app.state import _scheduler, _SCHEDULER_OK, TERMINAL_INTERVIEW_STATUSES
from backend.app.callbacks import _trigger_callback_call
from backend.api.routes.interview import (
    _get_interview, _detect_consent, _parse_callback_time, _is_repeat_request,
    _process_interview, _xml, _hangup_xml, _looks_like_repeat_request, _vocab_for, _TRANSITIONS,
)

router = APIRouter()

# Bounded pool for firing Plivo full-call-recording starts off the request path.
# Caps concurrency so a flood of /plivo/start hits (or a slow/unreachable Plivo API)
# can never spawn unbounded threads and exhaust the worker.
_recording_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="plivo-rec")


# ─── Plivo XML helpers (independent of Twilio's TwiML helpers in interview.py) ─
def _say(text: str) -> str:
    # Polly.Kajal (neural) is silent both with and without <prosody> — isolating the failure
    # to the voice itself, not the SSML wrapper. Confirmed with Plivo support: Kajal is not
    # actually a supported voice for the Voice API XML <Speak> element at all (their SSML docs
    # only list Polly.Aditi for Hindi) — Kajal-quality neural bilingual TTS is only reachable
    # through Plivo's separate "AI Studio" product (Cartesia/OpenAI/ElevenLabs providers), not
    # this raw Voice API XML integration. So this isn't a temporary bug to revisit later —
    # Polly.Aditi is the correct, durable choice for this integration path. <prosody
    # rate='110%'> compensates for Aditi's naturally slower default pace (the original
    # complaint that led to trying Kajal in the first place); confirmed working on Aditi.
    # pitch='+2%' per Plivo support's own tuning suggestion — a slight lift to sound less
    # flat/monotone, since rate alone doesn't address the "voice quality" complaint.
    return f"<Speak voice='Polly.Aditi'><prosody rate='110%' pitch='+2%'>{text}</prosody></Speak>"

def _short_record(action: str, max_length: int = 20, timeout: int = 2) -> str:
    # Used for consent/callback-time capture — Plivo's <GetInput> (live ASR) has NO
    # recording fallback (confirmed against Plivo's docs: it only ever posts a `Speech`
    # field, never a RecordUrl), so a live-ASR miss had no safety net and just failed the
    # whole consent step. <Record> + Groq Whisper is the same reliable pattern already used
    # for the Q&A answer loop — bilingual, and has an actual audio file to fall back on.
    return f"<Record action='{action}' maxLength='{max_length}' playBeep='true' finishOnKey='#' timeout='{timeout}'/>"

def _is_machine(form) -> bool:
    return form.get("Machine", "false").lower() == "true"


@router.api_route("/plivo/start/{interview_id}", methods=["GET", "POST"])
async def plivo_start(interview_id: str, request: Request):
    print(f"[Plivo start] HIT interview={interview_id} method={request.method}")
    data = _get_interview(interview_id)
    if not data:
        print(f"[Plivo start] interview={interview_id} NOT FOUND — hanging up")
        return _hangup_xml()

    # Plivo has no create-time record=True equivalent — start full-call recording
    # now that CallUUID is known. Fired on a background thread so it can never
    # delay or interfere with returning the greeting XML below.
    try:
        form = await request.form()
        call_uuid = form.get("CallUUID")
    except Exception:
        call_uuid = None
    if call_uuid and plivo_client:
        def _start_recording_bg():
            try:
                plivo_client.calls.record(call_uuid=call_uuid)
            except Exception as e:
                print(f"[Plivo] Failed to start full-call recording: {e}")
        try:
            _recording_pool.submit(_start_recording_bg)
        except Exception as e:
            print(f"[Plivo] Could not queue recording-start task: {e}")

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
        f"Hi {safe_name}! This is Sarah calling from the HR team at NickelFox Technologies. "
        f"I'm reaching out regarding your application for the {safe_title} role. "
        f"<break time='300ms'/>"
    )
    _screen_msg = "I'd like to conduct a brief screening round — it should only take about 5 to 7 minutes. Would now be a good time?"
    xml_response = (
        f"<Response>"
        f"{_say(greeting_text)}"
        f"{_say(_screen_msg)}"
        f"{_short_record(f'{base_url}/plivo/consent/{interview_id}')}"
        f"<Redirect method='POST'>{base_url}/plivo/consent/{interview_id}</Redirect>"
        f"</Response>"
    )
    print(f"[Plivo start] interview={interview_id} base_url={base_url!r} returning {len(xml_response)} chars of XML")
    print(f"[Plivo start] XML: {xml_response}")
    return _xml(xml_response)


@router.api_route("/plivo/consent/{interview_id}", methods=["GET", "POST"])
async def plivo_consent(
    interview_id: str,
    RecordUrl: str = Form(default=None),
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

    transcript = ""
    if RecordUrl:
        try:
            # fast=True (turbo model): consent replies are short, decisive yes/no —
            # speed matters more than the extra accuracy whisper-large-v3 buys here.
            transcript = transcribe_recording(RecordUrl, fast=True)
        except Exception as e:
            print(f"[Plivo consent] transcription failed: {e}")

    print(f"[Plivo consent] interview={interview_id} transcript='{transcript}'")
    data["consent_raw"] = transcript

    # Treat a hallucination-flagged result the same as genuine silence — neither is a real
    # answer we should feed into consent detection.
    if not transcript.strip() or transcript == HALLUCINATION_MARKER:
        if not data.get("consent_re_asked"):
            data["consent_re_asked"] = True
            _save_interview(interview_id, data)
            _reask_msg = "Oh, I'm sorry about that — I didn't quite catch your response! Could you let me know — just say yes if you're ready, or no if now isn't the best time?"
            return _xml(
                f"<Response>"
                f"{_say(_reask_msg)}"
                f"{_short_record(f'{base_url}/plivo/consent/{interview_id}')}"
                f"<Redirect method='POST'>{base_url}/plivo/consent/{interview_id}</Redirect>"
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
            f"Just a quick heads up — I'll ask you {total} questions. When you're done answering, press the # key to move ahead. "
            f"If you need me to repeat anything, just say repeat and I'll ask it again. Take as much time as you need. "
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
            f"<Record"
            f"  action='{base_url}/plivo/answer/{interview_id}/0'"
            f"  maxLength='120' playBeep='true' finishOnKey='#' timeout='5'"
            f"/>"
            f"<Redirect method='POST'>{base_url}/plivo/answer/{interview_id}/0</Redirect>"
            f"</Response>"
        )
    else:
        data["consent_status"] = "declined"
        _save_interview(interview_id, data)
        declined_text = (
            "Of course, completely understandable! "
            "Could you let me know a time that works better for you? "
            "Something like — in 30 minutes, today at 5 PM, or tomorrow morning would be perfect."
        )
        return _xml(
            f"<Response>"
            f"{_say(declined_text)}"
            f"{_short_record(f'{base_url}/plivo/callback-time/{interview_id}')}"
            f"<Redirect method='POST'>{base_url}/plivo/callback-time/{interview_id}</Redirect>"
            f"</Response>"
        )


@router.api_route("/plivo/callback-time/{interview_id}", methods=["GET", "POST"])
async def plivo_callback_time(
    interview_id: str,
    RecordUrl: str = Form(default=None),
):
    try:
        data = _get_interview(interview_id)
        if not data:
            return _hangup_xml()

        raw_time = ""
        if RecordUrl:
            try:
                # fast=True (turbo model): same latency trade-off as consent — short time
                # expressions don't need whisper-large-v3's extra accuracy.
                raw_time = transcribe_recording(RecordUrl, fast=True)
                print(f"[Plivo callback-time] interview={interview_id} transcribed='{raw_time}'")
            except Exception as e:
                print(f"[Plivo callback-time] transcription failed: {e}")

        if raw_time == HALLUCINATION_MARKER:
            raw_time = ""  # don't waste a Claude call parsing obviously-fabricated text

        print(f"[Plivo callback-time] interview={interview_id} raw='{raw_time}'")

        data["callback_time_raw"] = raw_time
        dt_str = _parse_callback_time(raw_time) if raw_time else None
        call_log = data.get("call_log", [])

        if dt_str:
            data["callback_scheduled_at"] = dt_str
            data["status"] = "callback_scheduled"
            if call_log:
                call_log[-1]["status"]               = "callback_scheduled"
                call_log[-1]["ended_at"]             = datetime.now().isoformat()
                call_log[-1]["callback_scheduled_at"] = dt_str
            # Save BEFORE scheduling — prevents orphaned APScheduler jobs if save throws
            _save_interview(interview_id, data)
            _sync_candidate_interview(interview_id, data)
            try:
                from backend.api.routes.pipeline import _on_pipeline_call_ended
                _on_pipeline_call_ended(interview_id, "callback")
            except Exception:
                pass
            if _SCHEDULER_OK:
                try:
                    dt = datetime.fromisoformat(dt_str)
                    _scheduler.add_job(
                        _trigger_callback_call, 'date',
                        run_date=dt, args=[interview_id],
                        id=f"callback_{interview_id}", replace_existing=True,
                        misfire_grace_time=3600,
                    )
                    print(f"[Plivo callback] Scheduled {interview_id} at {dt_str}")
                except Exception as e:
                    print(f"[Plivo callback] Schedule failed: {e}")
            try:
                readable = datetime.fromisoformat(dt_str).strftime("%A at %I:%M %p")
            except Exception:
                readable = "the time you mentioned"
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
        print(f"[Plivo XML] plivo_callback_time unhandled error for {interview_id}: {_e}")
        _tech_err_msg = "We're having a technical issue. We'll call you back shortly. Goodbye!"
        return _xml(
            f"<Response>"
            f"{_say(_tech_err_msg)}"
            f"<Hangup/>"
            f"</Response>"
        )


@router.api_route("/plivo/answer/{interview_id}/{q_idx}", methods=["GET", "POST"])
async def plivo_answer(
    interview_id:      str,
    q_idx:             int,
    background_tasks:  BackgroundTasks,
    request:           Request,
    RecordUrl:         str = Form(default=None),
    RecordingDuration: str = Form(default=None),
    CallUUID:          str = Form(default=None),
    Digits:            str = Form(default=None),
):
    try:
        data = _get_interview(interview_id)
        if not data:
            return _hangup_xml()

        questions   = data["questions"]
        total       = len(questions)
        base_url    = os.getenv("BASE_URL", "").rstrip("/")
        rec_url_val = RecordUrl
        duration    = int(RecordingDuration or "0")

        print(f"[Plivo answer] interview={interview_id} q={q_idx}/{total-1} duration={duration}s digits={Digits!r}")

        if q_idx >= total:
            print(f"[Plivo answer] q_idx={q_idx} out of bounds (total={total}) — hanging up")
            return _hangup_xml()

        if CallUUID:
            data["twilio_call_sid"] = CallUUID

        if rec_url_val and not data["transcriptions"].get(q_idx):
            data["status"] = "processing"

            # Truly silent or near-silent (Whisper hallucinates on < 3s clips)
            # Also catches rapid # presses — finishOnKey does NOT bypass the minimum duration
            if duration < 3:
                retries = data["repeat_counts"].get(q_idx, 0) + 1
                data["repeat_counts"][q_idx] = retries
                if retries < 3:
                    print(f"[Plivo answer] silence q={q_idx} retry={retries}/3")
                    _silence_msg = "Hmm, that was too short to capture. Please share your answer after the beep and press the # key when you're finished."
                    return _xml(
                        f"<Response>"
                        f"{_say(_silence_msg)}"
                        f"<Record"
                        f"  action='{base_url}/plivo/answer/{interview_id}/{q_idx}'"
                        f"  maxLength='120' playBeep='true' finishOnKey='#' timeout='5'"
                        f"/>"
                        f"<Redirect method='POST'>{base_url}/plivo/answer/{interview_id}/{q_idx}</Redirect>"
                        f"</Response>"
                    )
                else:
                    print(f"[Plivo answer] silence q={q_idx} max retries — marking no answer")
                    # If earlier segments of this answer were captured before a mid-answer
                    # pause (see the pause-hint branch below), use that instead of discarding
                    # it — going silent on the FINAL segment doesn't mean nothing was said.
                    pending = data.get("_pending_answer_parts", {}).pop(q_idx, None)
                    data["transcriptions"][q_idx] = " ".join(pending) if pending else "[no answer provided]"
                    # Record the (short) URL too so an all-silent interview still has a
                    # non-empty recordings dict — otherwise the status callback misclassifies
                    # a fully-reached interview as "abandoned".
                    data["recordings"][q_idx] = rec_url_val

            else:
                # Transcribe first — repeat check must happen before pause hint
                quick_text = None
                try:
                    _result_holder = [None]
                    def _transcribe_bg():
                        try:
                            _result_holder[0] = transcribe_recording(rec_url_val, fast=False, vocab=_vocab_for(data))
                        except Exception as _te:
                            print(f"[Plivo answer] transcription error: {_te}")
                    _t = _th.Thread(target=_transcribe_bg, daemon=True)
                    _t.start()
                    _t.join(timeout=12)  # truly returns after 12s — daemon thread finishes in background
                    quick_text = _result_holder[0]
                    if quick_text:
                        print(f"[Plivo answer] q={q_idx} transcript: {quick_text[:100]!r}")
                    else:
                        print(f"[Plivo answer] q={q_idx} transcription timed out or failed")
                except Exception as te:
                    print(f"[Plivo answer] inline transcription failed: {te}")

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
                        print(f"[Plivo answer] repeat detected q={q_idx} count={repeat_count+1}/2 — re-asking")
                        safe_q = html.escape(questions[q_idx])
                        _repeat_msg = f"Of course, happy to repeat that! <break time='400ms'/>{safe_q}"
                        return _xml(
                            f"<Response>"
                            f"{_say(_repeat_msg)}"
                            f"<Record"
                            f"  action='{base_url}/plivo/answer/{interview_id}/{q_idx}'"
                            f"  maxLength='120' playBeep='true' finishOnKey='#' timeout='5'"
                            f"/>"
                            f"<Redirect method='POST'>{base_url}/plivo/answer/{interview_id}/{q_idx}</Redirect>"
                            f"</Response>"
                        )
                    else:
                        # Move on WITHOUT recording the repeat-request text itself as the
                        # candidate's answer — that would unfairly tank their score.
                        print(f"[Plivo answer] repeat limit reached q={q_idx} — moving on without scoring the repeat request")
                        data["recordings"][q_idx] = rec_url_val
                        data["transcriptions"][q_idx] = "[declined to answer after repeat requests]"
                        is_repeat = False
                        _repeat_cap_reached = True

                # Spoke but didn't press # and transcription isn't a repeat
                # Skip if maxLength (120s) was hit — treat as completed answer
                if not _repeat_cap_reached and duration > 6 and not Digits and duration < 118:
                    print(f"[Plivo answer] spoke then paused q={q_idx} — prompting press #")
                    # Save this segment instead of silently discarding it — a candidate who
                    # pauses mid-answer (e.g. to think through a technical question) would
                    # otherwise lose everything said before the pause, since a fresh Record
                    # starts for the same question and only the last segment used to survive.
                    if quick_text and quick_text != HALLUCINATION_MARKER:
                        pending = data.setdefault("_pending_answer_parts", {})
                        pending.setdefault(q_idx, []).append(quick_text)
                    _pause_msg = "Whenever you're ready, just press the # key to wrap up your answer."
                    return _xml(
                        f"<Response>"
                        f"{_say(_pause_msg)}"
                        f"<Record"
                        f"  action='{base_url}/plivo/answer/{interview_id}/{q_idx}'"
                        f"  maxLength='120' playBeep='true' finishOnKey='#' timeout='5'"
                        f"/>"
                        f"<Redirect method='POST'>{base_url}/plivo/answer/{interview_id}/{q_idx}</Redirect>"
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
            print(f"[Plivo answer] advancing to q={next_q}")
            safe_next_q = html.escape(questions[next_q])
            transition  = _TRANSITIONS[next_q % len(_TRANSITIONS)]
            midpoint    = " We're halfway through — you're doing brilliantly! <break time='300ms'/>" if next_q == total // 2 else ""
            _next_msg = f"{transition}{midpoint} <break time='400ms'/>{safe_next_q}"
            return _xml(
                f"<Response>"
                f"{_say(_next_msg)}"
                f"<Record"
                f"  action='{base_url}/plivo/answer/{interview_id}/{next_q}'"
                f"  maxLength='120' playBeep='true' finishOnKey='#' timeout='5'"
                f"/>"
                f"<Redirect method='POST'>{base_url}/plivo/answer/{interview_id}/{next_q}</Redirect>"
                f"</Response>"
            )
        else:
            print(f"[Plivo answer] all questions done — closing call")
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
        print(f"[Plivo answer] UNHANDLED ERROR at q={q_idx}: {e}")
        traceback.print_exc()
        _ans_err_msg = "Oh, I'm so sorry — we seem to have hit a small technical hiccup. Thank you so much for your time today, and we'll be in touch soon. Take care!"
        return _xml(
            f"<Response>"
            f"{_say(_ans_err_msg)}"
            f"<Hangup/>"
            f"</Response>"
        )


@router.post("/plivo/status/{interview_id}")
async def plivo_status_callback(interview_id: str, request: Request, background_tasks: BackgroundTasks):
    form        = await request.form()
    call_status = form.get("CallStatus", "")
    print(f"[Plivo status] interview={interview_id} CallStatus={call_status}")

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


@router.post("/plivo/amd/{interview_id}")
async def plivo_amd_callback(interview_id: str, request: Request):
    """Async AMD callback — observe-only. Plivo's AMD previously false-positived on
    100% of real pickups, so this logs the signal but never hangs up the call."""
    form = await request.form()
    print(f"[Plivo AMD] interview={interview_id} Machine={form.get('Machine','')}")

    if _is_machine(form):
        print(f"[Plivo AMD] flagged machine for interview={interview_id} — observe-only, not hanging up")

    return {"status": "ok"}
