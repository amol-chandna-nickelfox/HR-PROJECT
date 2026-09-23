# RecruitAI — AI-Powered HR Screening Assistant

## What It Does
Phone-based AI recruitment pipeline: upload resumes → Claude scores them → Twilio calls candidates → Groq Whisper transcribes answers → Claude scores the interview. Supports single calls, batch processing, and parallel pipelines.

---

## Stack

### Backend
| Package | Version | Role |
|---------|---------|------|
| `fastapi` | 0.115.0 | HTTP framework |
| `uvicorn[standard]` | 0.30.6 | ASGI server |
| `pydantic` | 2.9.2 | Request/response validation |
| `python-dotenv` | ≥1.0.0 | `.env` loading via `backend/app/config.py` |
| `httpx` | ≥0.27.0 | HTTP client (ngrok detection, Twilio audio fetch) |
| `anthropic` | ≥0.25.0 | Claude API — question generation, consent detection, scoring, job title extraction |
| `groq` | ≥0.9.0 | Groq Whisper API — audio transcription |
| `twilio` | ≥9.0.0 | Outbound calls, TwiML, recording, AMD |
| `apscheduler` | ≥3.10.0 | Scheduled callback re-calls (`BackgroundScheduler`) |
| `sqlalchemy` | ≥2.0 | ORM / query layer |
| `psycopg` | ≥3.0 | PostgreSQL driver (psycopg3) |
| `PyMuPDF` | ≥1.23.0 | PDF text extraction (`fitz`) |
| `python-docx` | ≥1.1.0 | DOCX text extraction |
| `python-multipart` | ≥0.0.9 | Multipart form uploads (file + form fields) |
| `PyJWT` | (implicit) | JWT encode/decode in `auth.py` |
| `plivo` | (optional) | Alternate call provider — see Call Provider Abstraction below |

### Frontend (React + Vite)
| Package | Role |
|---------|------|
| React 18 | UI framework |
| Vite | Dev server + bundler, proxies API to `localhost:8000` |
| `@phosphor-icons/react` | Icon library used throughout |
| `useAnalyze`, `useInterview`, `useBatch` hooks | Encapsulate API call + state logic |
| `AppContext` | Global openings state, duplicate detection, callback alerts |

---

## Running the Project

> **CRITICAL: Start ngrok BEFORE the backend.** The server auto-detects ngrok on startup (`http://localhost:4040/api/tunnels`) and sets `BASE_URL` for Twilio webhooks. If ngrok isn't running first, all Twilio callbacks fail silently.

```
# Terminal 1 — ngrok (always first)
ngrok http 8000

# Terminal 2 — backend
cd backend
.venv\Scripts\activate
uvicorn backend.app.main:app --reload --port 8000

# Terminal 3 — frontend
cd frontend
npm run dev
```

---

## Environment Variables (`.env` at project root — NEVER commit)

```
DATABASE_URL=postgresql://user:pass@host:5432/dbname
JWT_SECRET=<random string>
CLAUDE_API_KEY=sk-ant-...
GROQ_API_KEY=gsk_...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+1...
PLIVO_AUTH_ID=...                 # optional — only if using Plivo as call provider
PLIVO_AUTH_TOKEN=...
PLIVO_PHONE_NUMBER=+1...
CALL_PROVIDER=twilio               # optional — default provider ("twilio" or "plivo"), toggle at runtime via /settings
INTERVIEW_MODE=legacy              # optional — "legacy" (press #) or "streaming" (auto end-of-answer), toggle via /settings
DEEPGRAM_API_KEY=...               # streaming mode — Deepgram Flux STT ($200 signup credit)
AZURE_SPEECH_KEY=...               # streaming mode — Azure Neural TTS (F0 tier: 500k chars/mo free forever)
AZURE_SPEECH_REGION=centralindia   # streaming mode — Azure region of the Speech resource
BASE_URL=https://xxxx.ngrok.io    # auto-set on startup if ngrok running
COMPANY_NAME=NickelFox Technologies
SUPER_ADMIN_USERNAME=director
SUPER_ADMIN_PASSWORD=...
SMTP_USER=<SES SMTP user>         # optional — for welcome emails
SMTP_PASSWORD=<SES SMTP password>
SMTP_FROM=noreply@...
APP_URL=http://localhost:3001
```

Loaded by `backend/app/config.py` (first import in `main.py`).

---

## Project Structure

```
backend/
  app/
    main.py          — FastAPI app, lifespan (DB init, store restore, stale-call cleanup, pipeline auto-resume, ngrok detect, callback reschedule); global Twilio error handler
    state.py         — In-memory stores + APScheduler instance
    database.py      — ALL DB functions (init, save, load, sync, helpers); includes _reset_candidate_on_call_failure()
    config.py        — Loads .env via python-dotenv
    callbacks.py     — APScheduler callback re-dial logic
    dependencies.py  — FastAPI dependency injection helpers
  api/routes/
    auth.py          — JWT auth, user management
    interview.py     — Twilio-only TwiML routes + _process_interview background job + shared call-flow helpers (consent detection, callback-time parsing, _get_interview, _xml)
    plivo.py         — Plivo-only TwiML routes — fully independent of interview.py's Twilio routes, imports only the provider-neutral helpers above
    batch.py         — Batch upload, /calls/active endpoint
    pipeline.py      — Pipeline orchestration (start/stop/status, _on_pipeline_call_ended)
    openings.py      — Job openings CRUD
    health.py        — Health check endpoint
    resume.py        — Single resume analyze endpoint
    settings.py      — GET/PUT /settings (super_admin) — toggle call_provider twilio/plivo + interview_mode legacy/streaming
    stream.py        — Streaming-mode routes: /twilio|/plivo/stream-xml/{id} (answer XML) + /ws/twilio|/ws/plivo/{id} (websockets)
  services/
    interviewer.py       — Claude (questions + scoring), Groq Whisper, start_twilio_call()
    voice_agent.py       — Streaming interview agent (Pipecat): InterviewFlowProcessor state machine, STT/TTS builders, run_interview_session()
    email_service.py     — Amazon SES SMTP welcome emails
    recording_cleanup.py — Daily job deleting recordings older than 89 days (Twilio + Plivo)
  utils/
    file_utils.py    — PDF/DOCX text extraction, job title extraction

frontend/src/
  App.jsx              — Shell: routing, pipeline state, active calls polling, sidebar
  api/client.js        — All fetch() wrappers (apiAnalyze, apiStartInterview, apiActiveCalls, etc.)
  context/AppContext.jsx — Openings, batches, callbacks, duplicate detection
  hooks/
    useAnalyze.js      — Resume analyze flow
    useInterview.js    — Single Twilio call flow
    useBatch.js        — Batch upload + per-candidate call flow
  components/
    Sidebar.jsx           — Navigation + active calls / callbacks badges
    BatchResultsTable.jsx — Candidate table with call button
    BatchProgress.jsx     — Batch processing progress display
    InterviewPanel.jsx    — Live interview status during a call
    ResultsDashboard.jsx  — Score breakdown after completion
    ScoreBreakdown.jsx    — Bar charts for interview scoring dimensions
    CallbackAlertModal.jsx — Due callback alert banner
    LoginPage.jsx         — Auth page
    UserManagement.jsx    — super_admin user CRUD
    LocalInterview.jsx    — Browser-based simulated interview (no Twilio)
    SimulateForm.jsx      — Simulate interview with manual answers
    (+ CandidateCard, ContactCard, SkillsPanel, ScoreRing, JsonViewer, ReasonBox, Header)
```

---

## In-Memory Stores (`backend/app/state.py`)

| Store | Key | Value |
|-------|-----|-------|
| `interview_store` | `interview_id` (UUID) | Full interview dict — status, questions, recordings, score |
| `batch_store` | `batch_id` (UUID) | Batch metadata + all candidates list |
| `opening_store` | `opening_id` (UUID) | Job opening title + JD text |
| `pipeline_store` | `pipeline_id` (UUID) | Queue, active, completed, skipped, status |
| `opening_pipeline` | `opening_id` | `pipeline_id` — one active pipeline per opening |
| `settings_store` | `"call_provider"` | Active call provider (`"twilio"` or `"plivo"`) |
| `settings_store` | `"interview_mode"` | `"legacy"` (press #) or `"streaming"` (Pipecat voice agent) — seeded from `INTERVIEW_MODE` env, DB-persisted like `call_provider` |

`interview_store` is the live source of truth during a call. DB is ~1-2s behind. On startup, all stores are restored from PostgreSQL. `settings_store["call_provider"]` is seeded from the `CALL_PROVIDER` env var at import time, then **overridden by the DB-persisted value** (`app_settings` table) during the `main.py` lifespan startup — so a provider switch made via `PUT /settings` survives server restarts. See `app_settings` in Database Schema below.

---

## Database Schema

### `job_openings`
`id, title, jd_text, created_at`

### `interviews`
`id, opening_id, status, consent_status, consent_raw, consent_re_asked, candidate_name, phone, job_title, jd_text, twilio_call_sid, transcript, fail_reason, processing_step, callback_time_raw, callback_scheduled_at, questions (jsonb), recordings (jsonb), transcriptions (jsonb), repeat_counts (jsonb), score_result (jsonb), call_log (jsonb), created_at, updated_at`

`call_log` — array of `{attempt, started_at, status, is_callback}` entries, one per dial attempt (initial call, pipeline retry, or scheduled callback re-dial); used to track multi-attempt history per interview.

### `batch_candidates`
`id (serial), batch_id, single_id, opening_id, interview_id, file_name, name, email, phone, resume_score, filter_status, interview_status, interview_score, combined_score, callback_scheduled_at, resume_text, analyze_result (jsonb), score_result (jsonb), created_at, updated_at`

### `batches`
`id, opening_id, status, jd_text, job_title, total, completed, created_at, updated_at`

### `pipelines`
`id, opening_id, status, queue (jsonb), active (jsonb), completed (jsonb), skipped (jsonb), total, jd_text, job_title, created_at, updated_at`

### `transcript_entries`
`id (serial), interview_id, question_index, question_text, answer_text, recording_url, repeat_count, created_at`

### `users`
`id (serial), username, full_name, password_hash, role, must_change_password, temp_expires_at, created_at`

### `app_settings`
`key (text, primary key), value (text), updated_at`

Generic key/value store for runtime settings that must survive restarts. Currently holds one row: `call_provider`. Read via `_get_setting(key)`, written via `_set_setting(key, value)` (`backend/app/database.py`).

---

## All API Endpoints

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | Login → JWT token |
| POST | `/auth/logout` | Logout (client-side token drop) |
| GET | `/auth/me` | Validate token, get current user |
| GET | `/auth/users` | List users (super_admin only) |
| POST | `/auth/users` | Create user + send welcome email (super_admin only) |
| DELETE | `/auth/users/{username}` | Delete user (super_admin only) |
| PUT | `/auth/users/{username}/password` | Reset password (super_admin only) |
| PUT | `/auth/change-password` | Change own password |

### Resume / Single Candidate
| Method | Path | Description |
|--------|------|-------------|
| POST | `/analyze` | Analyze single resume against JD, return score + candidate details |

### Interview (Single Call)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/interview/start` | Start Twilio call, create interview |
| GET | `/interview/status/{id}` | Poll interview status |
| GET | `/interview/stream/{id}` | SSE stream of interview status |
| POST | `/interview/recall/{id}` | Re-call same candidate |
| POST | `/interview/force-resolve/{id}` | Force processing or mark abandoned |
| POST | `/interview/questions` | Generate questions from resume + JD |
| POST | `/interview/simulate` | Simulate interview with provided answers |
| POST | `/interview/local/next` | Get next question for browser interview |
| POST | `/interview/local/score` | Score a browser interview conversation |

### Twilio Webhooks (`backend/api/routes/interview.py` — Twilio-only, no Plivo code)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/twilio/start/{id}` | Initial TwiML — greeting + consent gather |
| GET/POST | `/twilio/consent/{id}` | Consent response handler |
| GET/POST | `/twilio/answer/{id}/{q}` | Answer recording handler (q = 0–6) |
| GET/POST | `/twilio/callback-time/{id}` | Callback time parser |
| POST | `/twilio/status/{id}` | Twilio call status callback |
| POST | `/twilio/amd/{id}` | Async AMD callback (voicemail detection, active — hangs up on detection) |

### Plivo Webhooks (`backend/api/routes/plivo.py` — Plivo-only, independent file)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/plivo/start/{id}` | Initial Answer XML — greeting + consent gather; also starts full-call recording via `calls.record()` (fire-and-forget background thread) |
| GET/POST | `/plivo/consent/{id}` | Consent response handler |
| GET/POST | `/plivo/answer/{id}/{q}` | Answer recording handler (q = 0–6) |
| GET/POST | `/plivo/callback-time/{id}` | Callback time parser |
| POST | `/plivo/status/{id}` | Plivo call status callback (hangup_url) |
| POST | `/plivo/amd/{id}` | Async AMD callback — **observe-only**: logs machine detection but never hangs up (Plivo's AMD false-positived on 100% of real test pickups) |

### Batch
| Method | Path | Description |
|--------|------|-------------|
| POST | `/batch/start` | Upload resumes + JD, start batch analysis |
| GET | `/batch/status/{id}` | Poll batch + all candidate statuses |
| POST | `/batch/{id}/interview/start` | Start call for one candidate in batch |
| GET | `/calls/active` | Global active calls across all batches + pipelines |

### Pipeline
| Method | Path | Description |
|--------|------|-------------|
| POST | `/openings/{id}/pipeline/start` | Start parallel calling pipeline |
| GET | `/openings/{id}/pipeline/status` | Poll pipeline progress |
| POST | `/openings/{id}/pipeline/stop` | Stop pipeline, drain queue |

### Openings
| Method | Path | Description |
|--------|------|-------------|
| GET | `/openings` | List all job openings |
| POST | `/openings` | Create opening |
| GET | `/openings/{id}` | Get single opening |
| PUT | `/openings/{id}` | Update title/JD |
| DELETE | `/openings/{id}` | Delete opening |

### Other
| Method | Path | Description |
|--------|------|-------------|
| GET | `/callbacks/due` | List overdue scheduled callbacks |
| GET | `/health` | Health check |
| GET | `/settings` | Get active call provider + interview mode (super_admin only) |
| PUT | `/settings` | Set `call_provider` (`"twilio"`/`"plivo"`) and/or `interview_mode` (`"legacy"`/`"streaming"`) (super_admin only) |

### Streaming Mode (`backend/api/routes/stream.py` — public like the other webhooks)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/twilio/stream-xml/{id}` | Answer TwiML: `<Connect><Stream url="wss://…/ws/twilio/{id}"/></Connect>` |
| GET/POST | `/plivo/stream-xml/{id}` | Answer XML: `<Stream bidirectional keepCallAlive contentType="audio/x-mulaw;rate=8000">wss://…</Stream>`; also starts Plivo full-call recording (same fire-and-forget pattern as `/plivo/start`) |
| WS | `/ws/twilio/{id}` | Twilio Media Streams websocket → shared Pipecat pipeline |
| WS | `/ws/plivo/{id}` | Plivo AudioStream websocket → same pipeline, Plivo serializer |

---

## AI / ML Services

### Claude (Anthropic)
Files: `backend/services/interviewer.py`, `backend/api/routes/interview.py`, `backend/utils/file_utils.py`
Model: `claude-haiku-4-5-20251001` (`interviewer.py`) / `claude-haiku-4-5` short alias (`interview.py`)

| Task | Where | Output |
|------|-------|--------|
| Resume scoring + candidate extraction | `resume.py` → `analyze()` | Structured JSON |
| Question generation (7 questions) | `generate_questions()` | JSON array |
| Consent detection (ambiguous speech) | `_detect_consent()` | YES/NO |
| Repeat request detection | `_is_repeat_request()` | YES/NO |
| Callback time parsing | `_parse_callback_time()` | ISO 8601 datetime |
| Interview scoring | `score_interview()` | JSON with 4 dimensions |
| Job title extraction | `extract_job_title()` | Plain text |

### Groq Whisper
File: `backend/services/interviewer.py` → `transcribe_recording(url, fast=False)`

- Model: `whisper-large-v3` (default) / `whisper-large-v3-turbo` (fast mode)
- Multilingual — handles Hindi, English, Hinglish natively
- Fetches audio from Twilio URL with Basic Auth, sends to Groq
- 3 retries with exponential backoff (Twilio recordings can 404 briefly)
- `ThreadPoolExecutor(max_workers=6)` for parallel transcription in `_process_interview`
- **Inline transcription** during live call uses `threading.Thread.join(timeout=12)` — hard 12s cap so Twilio webhook always responds in time. If it times out, `_process_interview` re-transcribes that answer from the recording URL after the call ends.

### Twilio
File: `backend/services/interviewer.py` → `start_twilio_call(phone, interview_id)`

- Outbound calls from `TWILIO_PHONE_NUMBER`
- AMD: `machine_detection="DetectMessageEnd"`, `machine_detection_timeout=30`, `async_amd=True`
- Status callback fires on: `initiated`, `ringing`, `answered`, `completed`
- `<Gather input='speech' language='hi-IN en-IN'>` — bilingual speech recognition
- `<Record maxLength='120' finishOnKey='#'>` — candidate answers, beep to start
- `timeout=20` — ring timeout in seconds before Twilio fires no-answer

### Call Provider — Twilio and Plivo are fully independent implementations
Files: `backend/services/interviewer.py`, `backend/api/routes/interview.py` (Twilio), `backend/api/routes/plivo.py` (Plivo), `backend/api/routes/settings.py`

Twilio and Plivo are **not** a shared/branching abstraction — they are two separate route files with their own XML helpers, field-name parsing, and webhook handlers. Only genuinely provider-neutral business logic is shared (imported by `plivo.py` from `interview.py`): `_detect_consent()`, `_parse_callback_time()`, `_is_repeat_request()`, `_process_interview()`, `_get_interview()`, `_xml()`/`_hangup_xml()`, `REPEAT_KEYWORDS`, `_TRANSITIONS`. None of these know or care which telecom provider placed the call. This split exists because the two providers previously shared branching helpers (`_provider()`-checks inside common functions), which made Plivo-specific bugs (see below) easy to introduce while touching Twilio code and vice versa.

- `start_twilio_call(phone, interview_id)` (in `interviewer.py`) branches on `settings_store["call_provider"]` → calls `_start_twilio_call()` or `_start_plivo_call()` — this is the only place provider selection happens for placing the outbound call.
- Active provider is set via `GET/PUT /settings` (super_admin only) and **persists across restarts** — `PUT` writes through to the `app_settings` DB table, and `main.py`'s startup lifespan restores it into `settings_store` (overriding the `CALL_PROVIDER` env default).
- **Voice**: Twilio uses `Say voice='Google.en-IN-Neural2-A'`. Plivo uses `Speak voice='Polly.Kajal'` (Polly's neural bilingual Hindi/Indian-English voice, more natural than the older standard `Polly.Aditi`) wrapped in `<prosody rate='110%'>` to compensate for Kajal's slower default pace relative to Twilio's voice — tune this percentage by ear if it drifts, it's the only tuning knob (`plivo.py` → `_say()`).
- **Speech recognition language**: Twilio listens for `hi-IN` and `en-IN` simultaneously (`<Gather language='hi-IN en-IN'>`). Plivo's `<GetInput>` only supports **one locale at a time** (a real platform limitation, not a bug) — currently set to `en-IN`. This only affects real-time ASR for consent/callback-time; the actual Q&A answers always go through Whisper (`transcribe_recording`), which handles Hindi/English/Hinglish natively regardless of provider.
- **Full-call recording**: Twilio gets this via `record=True` at call creation. Plivo has no equivalent creation-time flag — `plivo.py`'s `/plivo/start` calls `plivo_client.calls.record(call_uuid=...)` once the call is answered, fired on a background thread so it can never delay/block the greeting response.
- **AMD (voicemail detection)**: Twilio's is active — `/twilio/amd/{id}` hangs up on detection. Plivo's is **observe-only** — `/plivo/amd/{id}` logs `Machine=true` but never acts on it, because Plivo's AMD false-positived on 100% of real test pickups during development (see git history / this doc's revision notes if that needs revisiting).
- Webhook payload field names differ by provider: `RecordingUrl`/`RecordingDuration`/`CallSid` (Twilio) vs `RecordUrl`/`RecordingDuration`/`CallUUID` (Plivo) — note `RecordingDuration` is the real field name for **both** providers, not `RecordDuration`.
- Requires `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER` in `.env` to use Plivo; `start_twilio_call()` raises if these are missing and `call_provider` is `"plivo"`.
- **XML response encoding**: the shared `_xml()` helper (`interview.py`) sets `media_type="application/xml; charset=utf-8"` explicitly — Starlette only auto-appends `charset=utf-8` for `text/*` media types, not `application/*`, so this must stay explicit or non-ASCII characters in prompts (em-dashes, IPA phonemes, etc.) risk being misinterpreted by a stricter XML parser on either provider's end.

### Recording Cleanup
File: `backend/services/recording_cleanup.py`

- `cleanup_old_recordings()` runs daily via APScheduler (job id `recording_cleanup_daily`, `misfire_grace_time=3600`)
- Finds interviews created more than 89 days ago with non-empty `recordings`, deletes each recording from Twilio (`recordings(sid).delete()`) or Plivo (`recordings.delete(uuid)`) based on URL pattern
- Also deletes the full-call recording by `twilio_call_sid` (column reused for Plivo's `CallUUID` too) — tries both `twilio_client.recordings.list(call_sid=...)` and `plivo_client.recordings.list(call_uuid=...)` since interviews don't record which provider placed the call; whichever lookup doesn't match just returns nothing
- Clears `interviews.recordings` to `'{}'` and nulls `transcript_entries.recording_url` for cleaned interviews
- Deletion failures (e.g. already-deleted / 404) are logged and skipped, not fatal

---

## Interview Call Flow (Twilio TwiML)

```
/twilio/start/{id}
  → "Hello, could I speak with {name}? This is Sarah from NickelFox..."
  → <Gather language='hi-IN en-IN'> "Is this a good time?"
  → /twilio/consent/{id}

/twilio/consent/{id}
  → no speech → re-ask once → still silent → status=failed, pipeline: no_answer
  → _detect_consent(text): keyword check → Claude Haiku fallback
      YES → instructions ("press # when done, say repeat to re-hear") + Q1 → <Record> → /twilio/answer/{id}/0
      NO  → "Could you let me know a better time?" → /twilio/callback-time/{id}

/twilio/answer/{id}/{0..6}
  → silence (<3s, no #) → re-prompt up to 3 times
  → recording received → inline Groq transcription (12s timeout via Thread.join — Twilio limit is 15s)
  → if repeat keyword or Claude detects repeat → re-ask same question (max 2 times)
  → if spoke but no # and duration 6–118s → "press # to wrap up" prompt
  → save recording URL to data["recordings"][q_idx]
  → save transcription to data["transcriptions"][q_idx] (only if inline succeeded)
  → if q < 6 → transition phrase + next question
  → if q == 6 → closing message → _process_interview() in background → Hangup

/twilio/amd/{id}  [async, fires concurrently during active call]
  → AnsweredBy starts with "machine" → client.calls(sid).update(status="completed") → status=failed

/twilio/status/{id}  [fires when Twilio call reaches terminal state]
  → no-answer / busy → status=failed, pipeline: no_answer
  → completed + has recordings → status=processing → _process_interview()
  → completed + 0 recordings → status=abandoned, pipeline: no_answer
  → already terminal (processing/completed/failed/etc.) → skip (no double-fire)

/twilio/callback-time/{id}
  → parse time with Claude Haiku → _save_interview → schedule APScheduler job (save BEFORE schedule
    to prevent orphaned APScheduler jobs if save throws)
  → handles English AND Hindi/Hinglish: "10 minute baad", "kal 3 baje", "aaj shaam ko", etc.
  → status=callback_scheduled, pipeline: callback
  → unparseable → status=declined, pipeline: declined
```

**Global error handler** (`main.py`): `@app.exception_handler(Exception)` catches any unhandled
exception on `/twilio/` **or** `/plivo/` routes and returns a graceful voice response ("We're having a
technical issue, we'll call you back shortly") instead of a raw error page — `_get_error_xml(is_plivo)`
picks the right voice tag per provider.

## Plivo Call Flow (`backend/api/routes/plivo.py`)

Mirrors the Twilio flow above message-for-message and threshold-for-threshold (verified line-by-line) — same
greeting, same consent re-ask/decline copy, same silence/repeat/pause-nudge logic and timing, same closing
message. Only the provider-specific mechanics differ:

```
/plivo/start/{id}
  → starts full-call recording via plivo_client.calls.record(call_uuid=...) on a background thread
    (fire-and-forget — never blocks/delays the greeting response)
  → same greeting as Twilio, via <Speak voice='Polly.Kajal'><prosody rate='110%'>
  → <GetInput inputType='speech' language='en-IN'> "Is this a good time?"
  → /plivo/consent/{id}

/plivo/consent/{id} → /plivo/answer/{id}/{0..6} → /plivo/callback-time/{id}
  → identical logic to the Twilio routes; field names are Plivo's own
    (Speech, RecordUrl, RecordingDuration, CallUUID, Digits)

/plivo/amd/{id}  [async, fires concurrently during active call]
  → observe-only: logs "Machine=true" but never hangs up (see Call Provider Abstraction above for why)

/plivo/status/{id}  [fires when Plivo call reaches terminal state — hangup_url]
  → identical logic to /twilio/status/{id}; CallStatus is the same field name for both providers
```

## Streaming Interview Mode (`interview_mode=streaming`)

Files: `backend/services/voice_agent.py`, `backend/api/routes/stream.py`. Toggled at runtime via
`PUT /settings {"interview_mode": "streaming"}` (DB-persisted, like `call_provider`); the legacy
`#`-key flow stays fully intact as `"legacy"` and is the default.

Instead of the webhook/`<Record>` loop, the call is bridged into ONE shared Pipecat pipeline
(same code for both providers — only the websocket serializer differs):

```
transport.input (mulaw 8k)
  → VADProcessor (Silero, local)
  → STT            Deepgram Flux multilingual (default; semantic end-of-turn in the model,
                    eot_threshold 0.8 / eot_timeout 7s) — or nova-3 language=multi, or
                    VOICE_AGENT_STT=groq (free: Groq Whisper segmented per turn)
  → UserTurnProcessor  (strategy depends on STT — see below; also drives barge-in)
  → InterviewFlowProcessor  (scripted state machine: consent → Q1..Q7 → closing |
                             callback_time; reuses _detect_consent/_parse_callback_time/
                             _is_repeat_request/REPEAT_KEYWORDS/_TRANSITIONS unchanged)
  → TTS            Azure Neural (AZURE_TTS_VOICE, default en-IN-NeerjaNeural)
  → transport.output
```

Turn detection is chosen per-STT in `_build_stt()` (`voice_agent.py`), not a single fixed strategy:
- **Deepgram Flux** defines its own turn boundaries and calls Pipecat's `broadcast_interruption()`
  itself — `UserTurnProcessor` is configured with `ExternalUserTurnStrategies()` to defer to Flux
  entirely rather than running a second, uncoordinated VAD+smart-turn-v3 detector in parallel
  (confirmed by reading Pipecat's source: nothing auto-applies Flux's `STTMetadataFrame.user_turn_strategies`
  hint outside the `LLMContextAggregatorPair` pattern, which this app doesn't use).
- **Groq** (segmented/batch, no native turn concept) uses the library default: Silero VAD start +
  local smart-turn-v3 semantic stop (bundled ONNX model, CPU).
- **Deepgram nova-3** (non-Flux fallback, real interim transcripts) uses a min-words start strategy
  for a better backchannel filter than raw VAD onset.
- A short grace window in `InterviewFlowProcessor` (`VOICE_AGENT_INTERRUPT_GRACE_SECS`, default 1.5s)
  additionally catches very short utterances that interrupt right as a prompt starts speaking
  (backchannel filler like "mm"/"haan"/"okay") and re-states the prompt instead of treating it as
  a reply, capped at `_MAX_BACKCHANNEL_REPROMPTS` (2) to avoid looping in persistently noisy audio.

Key behaviors / differences from legacy:
- **No `#` key** — end-of-answer is detected semantically; candidate is told to "just pause".
- Answers are captured as **live text** in `data["transcriptions"]` (no per-question recording
  URLs); `_process_interview()` reuses them as cached transcriptions, so scoring/DB/pipeline
  code is unchanged. Both status callbacks treat non-empty transcriptions as evidence the
  candidate answered (not "abandoned").
- Full-call recording still works: Twilio `record=True`; Plivo via `calls.record()` fired from
  `/plivo/stream-xml`. `twilio_call_sid` is captured from the stream's start message.
- Silence: idle timer (`VOICE_AGENT_IDLE_SECS`, default 14s) re-prompts — consent 1×, questions
  2×, then `[no answer provided]` and advance. Repeat requests: same 2× limit as legacy.
- Mid-call hangup/stream drop: if answers exist → process what we have; else the provider
  status callback classifies it (abandoned/no_answer) exactly as legacy.
- Pipecat missing/broken → `VOICE_AGENT_OK=False`, stream-xml routes return hangup XML, and
  legacy mode is unaffected.

Tuning env knobs: `DEEPGRAM_STT_MODEL` (default `flux-general-multi`), `DEEPGRAM_EOT_THRESHOLD`,
`DEEPGRAM_EOT_TIMEOUT_MS`, `DEEPGRAM_STT_LANGUAGE`, `VOICE_AGENT_STT` (`deepgram`|`groq`),
`AZURE_TTS_VOICE`, `VOICE_AGENT_VAD_STOP_SECS`, `VOICE_AGENT_TURN_SETTLE_SECS`,
`VOICE_AGENT_IDLE_SECS`, `VOICE_AGENT_INTERRUPT_GRACE_SECS`.

Repeat-request detection (`_looks_like_repeat_request()` in `interview.py`, shared by legacy Twilio,
legacy Plivo, and streaming) only matches `REPEAT_KEYWORDS` within the first ~8 words of an utterance
— a long legitimate answer that happens to use the word "repeat" naturally no longer misfires. If the
2-repeat cap is hit, the repeat-request text itself is never stored as the candidate's answer (would
unfairly tank their score) — `"[declined to answer after repeat requests]"` is stored instead, in all
three call flows.

### `_process_interview()` (background thread, `interview.py`)
1. `_processing_started` guard + `try/finally` cleanup (prevents permanent lock on crash)
2. `ThreadPoolExecutor(max_workers=min(total,6))` — parallel Groq Whisper; reuses inline transcription
   from `data["transcriptions"]` if already cached (skips Groq re-call for that answer)
3. `data["transcriptions"].update(answers)` — syncs all final transcriptions back (fills any that timed out inline)
4. Build full transcript: consent + Q1/A1 … Q7/A7
5. `score_interview(transcript, questions, jd_text)` via Claude
6. Save: `_save_interview`, `_save_transcript_entries`, `_sync_candidate_interview`
7. Fire `_on_pipeline_call_ended(id, "completed")` if this was a pipeline call

---

## Pipeline ("Call All Qualified")

```
POST /openings/{id}/pipeline/start
  → _get_qualified_candidates_for_opening(opening_id)
       filter_status='qualified', phone IS NOT NULL
       interview_status NOT IN ('completed', 'processing', 'calling', 'callback_scheduled', 'declined')
       ORDER BY resume_score DESC
  → dedup by phone (keep highest resume_score)
  → pipeline = {queue:[...], active:{}, completed:[], skipped:[], status:"running", max_concurrent:3}
  → _fill_slots() runs in daemon thread

_start_candidate_call(pipeline_id, candidate, pipeline)
  → generate_questions() via Claude
  → create interview_store entry with pipeline_id
  → _link_batch_candidate_interview() → batch_candidates.interview_status = 'calling'
  → start_twilio_call() with AMD
  → on failure: delete interview from store + DB, call _reset_candidate_on_call_failure(bc_id)
    to NULL out batch_candidates.interview_id/interview_status so candidate is re-eligible

_on_pipeline_call_ended(interview_id, outcome)  [called at every terminal point in interview.py]
  → find owning pipeline via pipeline_store scan
  → acquire _pipeline_locks[pipeline_id] (threading.Lock)
  → pop candidate from active
  → "completed"  → completed list
  → "no_answer"  → re-queue once (no_answer_count < 1) → then skipped as "no_answer_twice"
                   (2 total attempts per candidate)
  → "declined"   → skipped as "declined"
  → "callback"   → skipped as "callback"
  → _fill_slots() → start next candidates
  → queue empty + active empty → status="completed", remove from opening_pipeline
  → _save_pipeline() to DB
```

**Server restart recovery** (`main.py` lifespan):
1. Stale active entries (calls stuck mid-flight during downtime) are resolved: completed→completed,
   callback_scheduled→skipped, calling/in_progress/processing→re-queued once (max 1 retry), others→skipped
2. Any running pipeline with empty active + non-empty queue auto-resumes via `_fill_slots()` in daemon thread

**Stop Pipeline button**: available on the Active Calls page in the UI — drains the queue and sets status="stopped".

---

## Callbacks (APScheduler)

File: `backend/app/callbacks.py`

- `BackgroundScheduler` (APScheduler) started in `main.py` lifespan
- `_trigger_callback_call(interview_id)` — resets interview state (clears recordings/transcriptions), re-dials
- `_reschedule_pending_callbacks()` — on server startup, re-registers all `callback_scheduled` interviews with APScheduler; overdue ones fire immediately in daemon threads
- Jobs keyed as `callback_{interview_id}`, `replace_existing=True`
- `misfire_grace_time=3600` — fires up to 1 hour late if server was briefly down

---

## Auth System (`backend/api/routes/auth.py`)

- JWT HS256, 8-hour expiry; payload: `{sub, role, must_change, exp}`
- Passwords: SHA-256 + 16-byte random hex salt → stored as `salt:hash`
- Roles: `super_admin` (full access), `admin` (can call/edit), `user` (view-only, `canEdit=false`)
- New users: temp password (24h expiry) → welcome email → forced change on first login
- Only `@nickelfox.com` emails accepted for new accounts
- Token stored in browser `sessionStorage`, validated on load via `GET /auth/me`
- `_require_auth` / `_require_super_admin` FastAPI dependencies handle authorization

---

## Email Service (`backend/services/email_service.py`)

- Amazon SES via SMTP — host: `email-smtp.us-east-1.amazonaws.com`, port 587, STARTTLS
- Only used for welcome emails on new user creation
- Requires: `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` in `.env`
- Temp password format: `{First4chars_of_name}@{6randomChars}` e.g. `Shiv@aB3kM9`
- Failure is non-fatal — logs and continues if SMTP not configured

---

## File Parsing (`backend/utils/file_utils.py`)

- **PDF**: PyMuPDF (`fitz.open(stream=content, filetype="pdf")`) — page-by-page text join
- **DOCX**: python-docx (`docx.Document(io.BytesIO(content))`) — paragraph text join
- **Job title extraction**: regex label patterns first → Claude Haiku fallback → first non-empty line

---

## Interview Scoring

| Dimension | Max | Measures |
|-----------|-----|---------|
| `communication` | 35 | Clarity, fluency, structure, professionalism |
| `confidence` | 30 | Directness, assertiveness, absence of hedging |
| `motivation_fit` | 20 | Genuine interest, role understanding |
| `behavioral_quality` | 15 | Quality of situational/example answers |
| **Total** | **100** | |

Verdict options: `Strongly Recommended` / `Recommended` / `Consider` / `Not Recommended`

Combined score (batch table): `resume_score × 0.4 + interview_score × 0.6`

---

## Security

- `.env` in `.gitignore` — **never commit credentials**
- All secrets via `os.getenv()` after `backend/app/config.py` loads
- JWT required for all routes except `/twilio/*` and `/plivo/*` webhooks
- Twilio and Plivo webhooks are unauthenticated (public) — both providers sign requests but signature validation isn't implemented for either; `BASE_URL` keeps them secret by obscurity
- Passwords never stored in plain text
