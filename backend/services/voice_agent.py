"""Streaming interview voice agent (interview_mode=streaming).

One Pipecat pipeline shared by both call providers:
  Twilio  → <Connect><Stream>  (Media Streams)  → /ws/twilio/{interview_id}
  Plivo   → <Stream bidirectional="true">       → /ws/plivo/{interview_id}

The candidate's speech is transcribed live and end-of-answer is detected
automatically — no "press #". Turn taking is semantic, not a silence timer:

  - STT: Deepgram Flux multilingual (model-integrated end-of-turn), or
         Deepgram nova-3 (language=multi), or Groq Whisper segmented
         (VOICE_AGENT_STT=groq — free tier, key already in .env).
  - Turn detection: STT-dependent, chosen in _build_stt(). Flux defines its
    own turn boundaries and calls broadcast_interruption() itself, so
    UserTurnProcessor is configured with ExternalUserTurnStrategies() to defer
    entirely to Flux rather than running a redundant, uncoordinated VAD +
    smart-turn-v3 detector on top of it. Groq (segmented/batch, no native turn
    concept) uses the library's default VAD-start + local smart-turn-v3 stop.
    nova-3 (real interim transcripts) uses a min-words start strategy for a
    better backchannel filter than raw VAD onset.
  - Barge-in: real interruption (candidate genuinely starts talking) is
    handled natively — either by Flux itself or by UserTurnProcessor's default
    strategies. A short grace window in InterviewFlowProcessor additionally
    suppresses very short utterances that interrupt within ~1.5s of a prompt
    starting (backchannel filler like "mm"/"haan"/"okay") by re-stating the
    prompt instead of treating it as a reply.
  - TTS: Azure Neural (en-IN-Neerja / hi-IN-Swara — free F0 tier).

The interview itself stays the same scripted 7-question flow as the legacy
webhook routes: the provider-neutral text helpers (_detect_consent,
_parse_callback_time, _is_repeat_request, _process_interview) are imported
from interview.py and reused unchanged; only the audio transport differs.

Pipecat is an optional dependency: if it isn't installed, VOICE_AGENT_OK is
False and the /ws routes reject connections — the legacy flow is unaffected.
"""

import os
import re as _re
import time
import asyncio
import threading
import traceback
from datetime import datetime

from backend.app.state import interview_store, _scheduler, _SCHEDULER_OK, TERMINAL_INTERVIEW_STATUSES
from backend.app.database import (
    _save_interview, _save_transcript_entries, _sync_candidate_interview,
)
from backend.app.callbacks import _trigger_callback_call
from backend.api.routes.interview import (
    _get_interview, _detect_consent, _parse_callback_time, _is_repeat_request,
    _process_interview, _looks_like_repeat_request, _TRANSITIONS,
)

# ── Optional Pipecat imports (server must still boot without them) ───────────
VOICE_AGENT_OK = True
VOICE_AGENT_IMPORT_ERROR = None
try:
    from pipecat.frames.frames import (
        TranscriptionFrame,
        InterimTranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
        BotStoppedSpeakingFrame,
        TTSSpeakFrame,
        EndWorkerFrame,
        StartFrame,
        EndFrame,
        CancelFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.serializers.plivo import PlivoFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketTransport, FastAPIWebsocketParams,
    )
except ImportError as _e:
    VOICE_AGENT_OK = False
    VOICE_AGENT_IMPORT_ERROR = str(_e)
    print(f"[VoiceAgent] pipecat not available — streaming mode disabled: {_e}")
    print('[VoiceAgent] Run: pip install "pipecat-ai[deepgram,azure,silero,groq]"')

    class FrameProcessor:  # placeholder so the class definition below still parses
        def __init__(self, *a, **kw): ...


# Grace window after a turn signal, letting trailing STT finals arrive
_TURN_SETTLE_SECS    = float(os.getenv("VOICE_AGENT_TURN_SETTLE_SECS", "0.8"))
# Seconds of no candidate speech after the agent finishes talking → re-prompt
_IDLE_TIMEOUT_SECS   = float(os.getenv("VOICE_AGENT_IDLE_SECS", "14"))
_MAX_SILENCE_RETRIES = 2   # re-prompts per question before giving up on it
_MAX_REPEATS         = 2   # same "say repeat" limit as the legacy flow

# States where the agent just said something the candidate is expected to
# respond to — used to gate the idle watchdog and the backchannel/early-
# interrupt grace window (see InterviewFlowProcessor).
_PROMPT_STATES = ("consent", "question", "callback_time")
# If the candidate's turn starts this soon after a prompt begins, and the
# resulting utterance is very short, treat it as backchannel noise ("mm",
# "haan", "okay") rather than a genuine reply — see Finding 2/3 in the review.
_INTERRUPT_GRACE_SECS   = float(os.getenv("VOICE_AGENT_INTERRUPT_GRACE_SECS", "1.5"))
_BACKCHANNEL_MAX_WORDS  = 3
_MAX_BACKCHANNEL_REPROMPTS = 2  # stop suppressing after this many, to avoid looping forever in noisy audio


def _plain(text: str) -> str:
    """Strip SSML-style tags — streaming TTS gets plain text."""
    return _re.sub(r"\s{2,}", " ", _re.sub(r"<[^>]+>", " ", text)).strip()


# ── STT / TTS builders ───────────────────────────────────────────────────────
def _build_stt():
    """Returns (stt_service, turn_strategies).

    turn_strategies is None to let UserTurnProcessor use its library defaults
    (Silero VAD start + local smart-turn-v3 stop) — correct for Groq, which has
    no native turn concept. For Deepgram Flux, turn_strategies is
    ExternalUserTurnStrategies(): Flux already emits its own authoritative
    UserStartedSpeakingFrame/UserStoppedSpeakingFrame and calls
    broadcast_interruption() itself (confirmed in pipecat's
    deepgram/flux/base.py) — running the generic VAD+smart-turn detector on
    top would be redundant, CPU-wasting, and race Flux's own semantic EOT
    decision with a generic audio classifier that doesn't understand the
    interview context.
    """
    choice = os.getenv("VOICE_AGENT_STT", "deepgram").lower()

    if choice == "groq":
        # Free-forever stack: VAD + smart-turn segment the turn, each utterance is
        # POSTed to Groq Whisper (same key/quota the legacy flow already uses).
        from pipecat.services.groq.stt import GroqSTTService
        print("[VoiceAgent] STT = Groq Whisper (segmented) — turn detection = VAD + smart-turn-v3 (default)")
        return GroqSTTService(
            api_key=os.getenv("GROQ_API_KEY"),
            model=os.getenv("GROQ_STT_MODEL", "whisper-large-v3"),
        ), None

    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY missing — set it in .env (or set VOICE_AGENT_STT=groq)")

    model = os.getenv("DEEPGRAM_STT_MODEL", "flux-general-multi")

    if model.startswith("flux"):
        # Deepgram Flux: semantic end-of-turn detection built into the STT model —
        # TranscriptionFrames arrive exactly at end-of-turn.
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        print(f"[VoiceAgent] STT = Deepgram Flux ({model}) — turn detection = Flux's own semantic EOT")
        return DeepgramFluxSTTService(
            api_key=api_key,
            model=model,
            params=DeepgramFluxSTTService.InputParams(
                # Interviews need pause tolerance — favor NOT cutting candidates off.
                eot_threshold=float(os.getenv("DEEPGRAM_EOT_THRESHOLD", "0.8")),
                eot_timeout_ms=int(os.getenv("DEEPGRAM_EOT_TIMEOUT_MS", "7000")),
            ),
        ), ExternalUserTurnStrategies()

    from pipecat.services.deepgram.stt import DeepgramSTTService
    from deepgram import LiveOptions
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
    print(f"[VoiceAgent] STT = Deepgram {model} (language=multi) — turn detection = min-words start + smart-turn-v3 stop")
    stt = DeepgramSTTService(
        api_key=api_key,
        live_options=LiveOptions(
            model=model,
            language=os.getenv("DEEPGRAM_STT_LANGUAGE", "multi"),
            smart_format=True,
        ),
    )
    # nova-3 (unlike Flux/Groq) streams real interim transcripts, so a min-words
    # start strategy can use them for a much better backchannel filter than raw
    # VAD onset — a genuine streaming STT, not a segmented/batch one like Groq.
    turn_strategies = UserTurnStrategies(
        start=[MinWordsUserTurnStartStrategy(min_words=3, use_interim=True)]
    )
    return stt, turn_strategies


def _build_tts():
    api_key = os.getenv("AZURE_SPEECH_KEY")
    if not api_key:
        raise RuntimeError("AZURE_SPEECH_KEY missing — set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in .env")
    from pipecat.services.azure.tts import AzureTTSService
    voice = os.getenv("AZURE_TTS_VOICE", "en-IN-NeerjaNeural")
    print(f"[VoiceAgent] TTS = Azure {voice}")
    return AzureTTSService(
        api_key=api_key,
        region=os.getenv("AZURE_SPEECH_REGION", "centralindia"),
        voice=voice,
    )


# ── Interview flow state machine ─────────────────────────────────────────────
class InterviewFlowProcessor(FrameProcessor):
    """Drives the scripted interview over a streaming call.

    States: consent → (question 0..N-1 → closing) | callback_time → done.
    Consumes final transcriptions + turn start/stop events emitted by the
    upstream UserTurnProcessor; speaks by pushing TTSSpeakFrame downstream
    to the TTS service.
    """

    def __init__(self, interview_id: str, data: dict):
        super().__init__()
        self.interview_id = interview_id
        self.data = data
        self.state = "consent"
        self.q_idx = 0
        self.questions = data["questions"]
        self.total = len(self.questions)
        self._turn_buffer: list[str] = []
        self._settle_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._silence_retries: dict[str, int] = {}
        self._backchannel_counts: dict[str, int] = {}
        self._handling_turn = False
        self._started = False
        # Early-interrupt / backchannel-suppression bookkeeping (Finding 2/3)
        self._prompt_started_at: float | None = None
        self._last_prompt_text: str | None = None
        self._early_interrupt = False

    # ---- frame plumbing ------------------------------------------------------
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            if not self._started:
                self._started = True
                self._watchdog_task = asyncio.create_task(self._idle_watchdog())
                await self._speak_greeting()
            return

        if isinstance(frame, (EndFrame, CancelFrame)):
            self._cancel_timers()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterimTranscriptionFrame):
            # Someone is talking — don't let the idle timer or a pending turn fire mid-speech
            self._cancel_idle()
            self._cancel_settle()
            return  # interims aren't needed downstream

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                self._turn_buffer.append(text)
                self._cancel_idle()
                self._schedule_settle()
            return  # consumed — the TTS must never receive the user's words as text

        if isinstance(frame, UserStartedSpeakingFrame):
            self._cancel_idle()
            self._cancel_settle()
            if (
                self._prompt_started_at is not None
                and self.state in _PROMPT_STATES
                and (time.monotonic() - self._prompt_started_at) < _INTERRUPT_GRACE_SECS
            ):
                self._early_interrupt = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            # Turn is semantically final (smart-turn / Flux) — process after a short
            # settle window so trailing transcription finals can land in the buffer.
            self._schedule_settle()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            # Agent finished talking — candidate's turn; arm the idle timer
            if self.state in _PROMPT_STATES and not self._turn_buffer:
                self._schedule_idle()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    # ---- timers --------------------------------------------------------------
    def _cancel_settle(self):
        if self._settle_task and not self._settle_task.done():
            self._settle_task.cancel()
        self._settle_task = None

    def _cancel_idle(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _cancel_timers(self):
        self._cancel_settle()
        self._cancel_idle()
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def _schedule_settle(self):
        self._cancel_settle()
        self._settle_task = asyncio.create_task(self._settle_then_handle())

    def _schedule_idle(self):
        self._cancel_idle()
        self._idle_task = asyncio.create_task(self._idle_then_reprompt())

    async def _idle_watchdog(self):
        """Independent backstop, decoupled from BotStoppedSpeakingFrame.

        The normal idle-reprompt path (_schedule_idle) only arms when
        BotStoppedSpeakingFrame arrives. This watchdog uses only our own
        _prompt_started_at bookkeeping (set the moment we push a prompt,
        regardless of any pipecat frame) so a stalled call still gets noticed
        even if that frame is ever missed for some transport/TTS combination.
        """
        try:
            while self.state != "done":
                await asyncio.sleep(5)
                if (
                    self.state in _PROMPT_STATES
                    and not self._turn_buffer
                    and not self._handling_turn
                    and self._idle_task is None
                    and self._prompt_started_at is not None
                    and (time.monotonic() - self._prompt_started_at) > _IDLE_TIMEOUT_SECS + 5
                ):
                    print(f"[VoiceAgent] {self.interview_id} watchdog: idle timer never armed — forcing silence handling")
                    await self._handle_silence()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[VoiceAgent] idle watchdog error ({self.interview_id}): {e}")

    async def _settle_then_handle(self):
        try:
            await asyncio.sleep(_TURN_SETTLE_SECS)
        except asyncio.CancelledError:
            return
        if not self._turn_buffer or self._handling_turn or self.state == "done":
            return
        text = " ".join(self._turn_buffer).strip()
        self._turn_buffer = []

        if self._early_interrupt:
            self._early_interrupt = False
            if self.state in _PROMPT_STATES and len(text.split()) <= _BACKCHANNEL_MAX_WORDS:
                key = f"{self.state}:{self.q_idx}"
                count = self._backchannel_counts.get(key, 0) + 1
                self._backchannel_counts[key] = count
                if count <= _MAX_BACKCHANNEL_REPROMPTS:
                    print(f"[VoiceAgent] {self.interview_id} likely backchannel/early interrupt "
                          f"({text!r}) — re-stating the last prompt instead of treating as a reply")
                    if self._last_prompt_text:
                        await self._speak(self._last_prompt_text)
                    return
                print(f"[VoiceAgent] {self.interview_id} backchannel re-prompt limit reached — "
                      f"processing {text!r} as a real reply to avoid looping")

        self._handling_turn = True
        try:
            await self._handle_turn(text)
        except Exception as e:
            print(f"[VoiceAgent] turn handling error ({self.interview_id}): {e}")
            traceback.print_exc()
        finally:
            self._handling_turn = False

    async def _idle_then_reprompt(self):
        try:
            await asyncio.sleep(_IDLE_TIMEOUT_SECS)
        except asyncio.CancelledError:
            return
        if self._turn_buffer or self._handling_turn or self.state == "done":
            return
        try:
            await self._handle_silence()
        except Exception as e:
            print(f"[VoiceAgent] silence handling error ({self.interview_id}): {e}")
            traceback.print_exc()

    # ---- speaking ------------------------------------------------------------
    async def _speak(self, text: str):
        plain = _plain(text)
        if self.state in _PROMPT_STATES:
            # Arm the early-interrupt/backchannel grace window (Finding 2/3) and
            # remember what was said so a suppressed backchannel can be re-stated.
            self._prompt_started_at = time.monotonic()
            self._last_prompt_text = plain
            self._early_interrupt = False
        await self.push_frame(TTSSpeakFrame(plain))

    async def _end_call(self):
        self.state = "done"
        self._cancel_timers()
        # EndWorkerFrame travels upstream to the task, which shuts the pipeline down
        # gracefully — queued TTS audio finishes playing first, then the serializer
        # hangs up the provider call (auto_hang_up).
        await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    async def _speak_greeting(self):
        name       = self.data.get("candidate_name") or "there"
        first_name = name.split()[0]
        title      = self.data.get("job_title") or "the open position"
        await self._speak(
            f"Hello, could I please speak with {first_name}? "
            f"Hi {first_name}! This is Sarah calling from the HR team at NickelFox Technologies. "
            f"I'm reaching out regarding your application for the {title} role. "
            f"I'd like to conduct a brief screening round — it should only take about 5 to 7 minutes. "
            f"Would now be a good time?"
        )

    def _question_text(self, idx: int) -> str:
        return self.questions[idx]

    # ---- silence handling (mirrors legacy re-prompt limits) -------------------
    async def _handle_silence(self):
        key = f"{self.state}:{self.q_idx}"
        retries = self._silence_retries.get(key, 0) + 1
        self._silence_retries[key] = retries

        if self.state == "consent":
            if retries <= 1:
                await self._speak(
                    "Oh, I'm sorry about that — I didn't quite catch your response! "
                    "Could you let me know — just say yes if you're ready, or no if now isn't the best time?"
                )
            else:
                await self._fail_no_consent()
            return

        if self.state == "question":
            if retries <= _MAX_SILENCE_RETRIES:
                await self._speak(
                    "Hmm, I didn't catch anything there. Please go ahead with your answer whenever you're ready."
                )
            else:
                print(f"[VoiceAgent] silence q={self.q_idx} max retries — marking no answer")
                self.data["transcriptions"][self.q_idx] = "[no answer provided]"
                await asyncio.to_thread(self._save_progress)
                await self._advance()
            return

        if self.state == "callback_time":
            if retries <= 1:
                await self._speak(
                    "Sorry, I didn't catch that — could you tell me a time that works better for you?"
                )
            else:
                await self._decline()
            return

    # ---- turn dispatch ---------------------------------------------------------
    async def _handle_turn(self, text: str):
        print(f"[VoiceAgent] {self.interview_id} state={self.state} q={self.q_idx} heard: {text[:120]!r}")

        if self.state == "consent":
            await self._handle_consent(text)
        elif self.state == "question":
            await self._handle_answer(text)
        elif self.state == "callback_time":
            await self._handle_callback_time(text)
        # state == closing/done: candidate talking over the goodbye — ignore

    async def _handle_consent(self, text: str):
        self.data["consent_raw"] = text
        accepted = await asyncio.to_thread(_detect_consent, text)
        if accepted:
            self.data["consent_status"] = "accepted"
            self.data["status"] = "in_progress"
            await asyncio.to_thread(_save_interview, self.interview_id, self.data)
            self.state = "question"
            self.q_idx = 0
            name = (self.data.get("candidate_name") or "there").split()[0]
            await self._speak(
                f"Thank you, {name} — I appreciate you taking the time. "
                f"Just a quick heads up — I'll ask you {self.total} questions. "
                f"When you finish answering, just pause for a moment and I'll move on to the next one. "
                f"If you need me to repeat anything, just say repeat and I'll ask it again. "
                f"Take as much time as you need. "
                f"Alright, let's get started! Here's my first question — {self._question_text(0)}"
            )
        else:
            self.data["consent_status"] = "declined"
            await asyncio.to_thread(_save_interview, self.interview_id, self.data)
            self.state = "callback_time"
            await self._speak(
                "Of course, completely understandable! "
                "Could you let me know a time that works better for you? "
                "Something like — in 30 minutes, today at 5 PM, or tomorrow morning would be perfect."
            )

    async def _handle_answer(self, text: str):
        q_idx = self.q_idx

        # Repeat request? Keyword check first (free, gated to the start of the
        # utterance so a long legit answer using the word "repeat" naturally
        # doesn't misfire), Claude only for very short replies.
        is_repeat = _looks_like_repeat_request(text)
        if not is_repeat and len(text.split()) < 8:
            is_repeat = await asyncio.to_thread(_is_repeat_request, text)

        if is_repeat:
            repeat_count = self.data["repeat_counts"].get(q_idx, 0)
            if repeat_count < _MAX_REPEATS:
                self.data["repeat_counts"][q_idx] = repeat_count + 1
                print(f"[VoiceAgent] repeat q={q_idx} count={repeat_count + 1}/{_MAX_REPEATS}")
                await self._speak(f"Of course, happy to repeat that! {self._question_text(q_idx)}")
                return
            # Cap reached — move on WITHOUT recording the repeat-request text
            # itself as the candidate's answer (that would unfairly tank their score).
            print(f"[VoiceAgent] repeat limit reached q={q_idx} — moving on without scoring the repeat request")
            self.data["transcriptions"][q_idx] = "[declined to answer after repeat requests]"
            await asyncio.to_thread(self._save_progress)
            await self._advance()
            return

        # Store the answer (append if the candidate adds more after a re-prompt)
        existing = self.data["transcriptions"].get(q_idx)
        if existing and existing != "[no answer provided]":
            self.data["transcriptions"][q_idx] = f"{existing} {text}"
        else:
            self.data["transcriptions"][q_idx] = text
        await asyncio.to_thread(self._save_progress)
        await self._advance()

    async def _advance(self):
        next_q = self.q_idx + 1
        if next_q < self.total:
            self.q_idx = next_q
            transition = _TRANSITIONS[next_q % len(_TRANSITIONS)]
            midpoint = (
                " We're halfway through — you're doing brilliantly!"
                if next_q == self.total // 2 else ""
            )
            await self._speak(f"{transition}{midpoint} {self._question_text(next_q)}")
        else:
            print(f"[VoiceAgent] {self.interview_id} all questions done — closing")
            self.state = "closing"
            self.data["status"] = "processing"
            await asyncio.to_thread(_save_interview, self.interview_id, self.data)
            await self._speak(
                "That's all my questions for today — you did a wonderful job! "
                "It was genuinely lovely speaking with you. Our team will be in touch very soon. "
                "Wishing you a brilliant rest of your day — take care!"
            )
            # Scoring runs in its own thread; the pipeline shuts down independently.
            threading.Thread(target=_process_interview, args=(self.interview_id,), daemon=True).start()
            await self._end_call()

    async def _handle_callback_time(self, text: str):
        self.data["callback_time_raw"] = text
        dt_str = await asyncio.to_thread(_parse_callback_time, text)
        call_log = self.data.get("call_log", [])

        if dt_str:
            self.data["callback_scheduled_at"] = dt_str
            self.data["status"] = "callback_scheduled"
            if call_log:
                call_log[-1]["status"] = "callback_scheduled"
                call_log[-1]["ended_at"] = datetime.now().isoformat()
                call_log[-1]["callback_scheduled_at"] = dt_str
            # Save BEFORE scheduling — prevents orphaned APScheduler jobs if save throws
            await asyncio.to_thread(_save_interview, self.interview_id, self.data)
            await asyncio.to_thread(_sync_candidate_interview, self.interview_id, self.data)
            self._notify_pipeline("callback")
            if _SCHEDULER_OK:
                try:
                    dt = datetime.fromisoformat(dt_str)
                    _scheduler.add_job(
                        _trigger_callback_call, "date",
                        run_date=dt, args=[self.interview_id],
                        id=f"callback_{self.interview_id}", replace_existing=True,
                        misfire_grace_time=3600,
                    )
                    print(f"[VoiceAgent] Callback scheduled {self.interview_id} at {dt_str}")
                except Exception as e:
                    print(f"[VoiceAgent] Callback schedule failed: {e}")
            try:
                readable = datetime.fromisoformat(dt_str).strftime("%A at %I:%M %p")
            except Exception:
                readable = "the time you mentioned"
            await self._speak(
                f"Perfect! We'll give you a call back on {readable}. "
                f"Thanks so much for your time today — have a wonderful day!"
            )
            await self._end_call()
        else:
            await self._decline()

    # ---- terminal helpers ------------------------------------------------------
    async def _decline(self):
        self.data["status"] = "declined"
        self.data["fail_reason"] = "Candidate declined to schedule a callback"
        call_log = self.data.get("call_log", [])
        if call_log:
            call_log[-1]["status"] = "declined"
            call_log[-1]["ended_at"] = datetime.now().isoformat()
        await asyncio.to_thread(_save_interview, self.interview_id, self.data)
        await asyncio.to_thread(_sync_candidate_interview, self.interview_id, self.data)
        self._notify_pipeline("declined")
        await self._speak(
            "No problem at all — we appreciate your time. "
            "If you change your mind, feel free to reach out to us. Have a wonderful day!"
        )
        await self._end_call()

    async def _fail_no_consent(self):
        self.data["status"] = "failed"
        self.data["fail_reason"] = "No response during consent check"
        self.data["consent_status"] = "declined"
        await asyncio.to_thread(_save_interview, self.interview_id, self.data)
        await asyncio.to_thread(_sync_candidate_interview, self.interview_id, self.data)
        self._notify_pipeline("no_answer")
        await self._end_call()

    def _notify_pipeline(self, outcome: str):
        try:
            from backend.api.routes.pipeline import _on_pipeline_call_ended
            _on_pipeline_call_ended(self.interview_id, outcome)
        except Exception:
            pass

    def _save_progress(self):
        _save_interview(self.interview_id, self.data)
        _save_transcript_entries(self.interview_id, self.data)


# ── Provider websocket handshakes ─────────────────────────────────────────────
async def _read_start_message(websocket, provider: str) -> dict:
    """Read the provider's initial websocket messages up to the 'start' event.

    Twilio sends {"event":"connected"} then {"event":"start","start":{...}}.
    Plivo sends {"event":"start","start":{...}}.
    Returns {"stream_id": ..., "call_id": ...}.
    """
    import json
    for _ in range(6):
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("event") == "start":
            start = msg.get("start", {})
            if provider == "twilio":
                return {
                    "stream_id": start.get("streamSid") or msg.get("streamSid"),
                    "call_id":   start.get("callSid"),
                }
            return {
                "stream_id": start.get("streamId") or msg.get("streamId"),
                "call_id":   start.get("callId") or start.get("callUUID") or start.get("callUuid"),
            }
    raise RuntimeError(f"No 'start' event received from {provider} websocket")


def _build_serializer(provider: str, stream_id: str, call_id: str):
    if provider == "twilio":
        # account_sid + auth_token enable auto_hang_up when the pipeline ends
        return TwilioFrameSerializer(
            stream_sid=stream_id,
            call_sid=call_id,
            account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        )
    return PlivoFrameSerializer(
        stream_id=stream_id,
        call_id=call_id,
        auth_id=os.getenv("PLIVO_AUTH_ID"),
        auth_token=os.getenv("PLIVO_AUTH_TOKEN"),
    )


# ── Session entrypoint (called from the /ws routes) ───────────────────────────
async def run_interview_session(websocket, provider: str, interview_id: str):
    """Run one streaming interview call end-to-end on an accepted websocket.

    The whole body runs under try/finally so _finalize_after_stream() always
    fires — including if the handshake itself fails (_read_start_message
    raising, e.g. a malformed/missing 'start' event) — otherwise the interview
    would be left stuck in whatever status it had, with cleanup depending
    entirely on the provider's own call-level status callback eventually firing.
    """
    data = _get_interview(interview_id)
    if not data:
        print(f"[VoiceAgent] interview {interview_id} not found — closing websocket")
        await websocket.close()
        return

    try:
        ids = await _read_start_message(websocket, provider)
        print(f"[VoiceAgent] {provider} stream started interview={interview_id} "
              f"stream={ids['stream_id']} call={ids['call_id']}")
        if ids.get("call_id"):
            data["twilio_call_sid"] = ids["call_id"]  # column shared by both providers
        data["status"] = "in_progress"
        try:
            _save_interview(interview_id, data)
        except Exception as e:
            print(f"[VoiceAgent] initial save failed: {e}")

        try:
            stt, turn_strategies = _build_stt()
            tts = _build_tts()
        except Exception as e:
            # Missing/invalid vendor keys — close the socket; the provider's status
            # callback will classify the interview (no answers → abandoned/failed).
            print(f"[VoiceAgent] STT/TTS setup failed interview={interview_id}: {e}")
            try:
                await websocket.close()
            except Exception:
                pass
            return

        flow = InterviewFlowProcessor(interview_id, data)

        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=_build_serializer(provider, ids["stream_id"], ids["call_id"]),
            ),
        )

        vad = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(stop_secs=float(os.getenv("VOICE_AGENT_VAD_STOP_SECS", "0.8")))
            )
        )
        # turn_strategies is None for Groq (library default: VAD start + local
        # smart-turn-v3 stop — the right choice for a segmented/batch STT with
        # no native turn concept) or ExternalUserTurnStrategies()/min-words for
        # Deepgram (Flux defines its own turn boundaries and calls
        # broadcast_interruption() itself; nova-3 gets a min-words start
        # strategy since it streams real interim transcripts) — see _build_stt().
        turns = UserTurnProcessor(user_turn_strategies=turn_strategies)

        @turns.event_handler("on_user_turn_stop_timeout")
        async def _on_turn_stop_timeout(_turns):
            # Diagnostic safety net — the flow's own idle timer/watchdog drive
            # actual re-prompting; this just surfaces a stuck-turn condition.
            print(f"[VoiceAgent] {interview_id} turn-stop watchdog fired (no stop strategy triggered in time)")

        pipeline = Pipeline([
            transport.input(),
            vad,
            stt,
            turns,
            flow,
            tts,
            transport.output(),
        ])

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=8000,
                audio_out_sample_rate=8000,
            ),
        )

        @transport.event_handler("on_client_disconnected")
        async def _on_disconnect(_transport, _client):
            print(f"[VoiceAgent] client disconnected interview={interview_id}")
            await task.cancel()

        runner = PipelineRunner(handle_sigint=False)
        try:
            await runner.run(task)
        except Exception as e:
            print(f"[VoiceAgent] pipeline error interview={interview_id}: {e}")
            traceback.print_exc()
    except Exception as e:
        print(f"[VoiceAgent] session setup failed interview={interview_id}: {e}")
        traceback.print_exc()
    finally:
        _finalize_after_stream(interview_id)


def _finalize_after_stream(interview_id: str):
    """Safety net when the stream ends (hangup, drop, or normal completion).

    Terminal states (callback_scheduled/declined/failed/processing/completed) were
    already set by the flow processor. If the call dropped mid-interview with some
    answers captured, process what we have; with none, the provider's status
    callback will classify it (abandoned / no-answer) exactly like the legacy flow.
    """
    data = interview_store.get(interview_id)
    if not data:
        return
    if data.get("status") in TERMINAL_INTERVIEW_STATUSES:
        return
    answered = {k: v for k, v in data.get("transcriptions", {}).items()
                if v and v != "[no answer provided]"}
    if answered:
        print(f"[VoiceAgent] stream ended mid-interview with {len(answered)} answer(s) — processing")
        data["status"] = "processing"
        try:
            _save_interview(interview_id, data)
        except Exception:
            pass
        threading.Thread(target=_process_interview, args=(interview_id,), daemon=True).start()
    else:
        print(f"[VoiceAgent] stream ended with no answers — leaving to status callback")
