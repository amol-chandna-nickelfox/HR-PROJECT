"""Streaming-mode call routes (interview_mode=streaming).

Two kinds of routes, both public like the other provider webhooks:

1. Answer-XML endpoints — what the provider fetches when the candidate picks up.
   They return the provider's Stream element pointing at our websocket:
     /twilio/stream-xml/{id}  →  <Connect><Stream url="wss://.../ws/twilio/{id}"/></Connect>
     /plivo/stream-xml/{id}   →  <Stream bidirectional="true" ...>wss://.../ws/plivo/{id}</Stream>
   (Paths deliberately live under /twilio/ and /plivo/ so main.py's global
   error handler returns graceful voice XML on any unhandled exception.)

2. Websocket endpoints — the bidirectional audio streams that feed the shared
   Pipecat pipeline in backend/services/voice_agent.py.

Status/AMD callbacks are unchanged — they keep hitting the legacy
/twilio/status, /twilio/amd, /plivo/status, /plivo/amd routes, whose logic is
provider-terminal-state handling that applies to streaming calls too.
"""

import os

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from backend.api.routes.interview import _get_interview, _xml, _hangup_xml
from backend.api.routes.plivo import _recording_pool
from backend.services.interviewer import plivo_client
from backend.services.voice_agent import (
    VOICE_AGENT_OK, VOICE_AGENT_IMPORT_ERROR, run_interview_session,
)

router = APIRouter()


def _ws_base_url() -> str:
    """Derive the wss:// base from BASE_URL (the ngrok https URL)."""
    base = os.getenv("BASE_URL", "").rstrip("/")
    return base.replace("https://", "wss://").replace("http://", "ws://")


# ── Answer XML ────────────────────────────────────────────────────────────────
@router.api_route("/twilio/stream-xml/{interview_id}", methods=["GET", "POST"])
async def twilio_stream_xml(interview_id: str):
    data = _get_interview(interview_id)
    if not data or not VOICE_AGENT_OK:
        if not VOICE_AGENT_OK:
            print(f"[Stream] voice agent unavailable ({VOICE_AGENT_IMPORT_ERROR}) — hanging up")
        return _hangup_xml()
    ws_url = f"{_ws_base_url()}/ws/twilio/{interview_id}"
    print(f"[Stream] Twilio answer XML → {ws_url}")
    return _xml(
        f"<Response>"
        f"<Connect>"
        f"<Stream url='{ws_url}'/>"
        f"</Connect>"
        f"<Hangup/>"
        f"</Response>"
    )


@router.api_route("/plivo/stream-xml/{interview_id}", methods=["GET", "POST"])
async def plivo_stream_xml(interview_id: str, request: Request):
    data = _get_interview(interview_id)
    if not data or not VOICE_AGENT_OK:
        if not VOICE_AGENT_OK:
            print(f"[Stream] voice agent unavailable ({VOICE_AGENT_IMPORT_ERROR}) — hanging up")
        return _hangup_xml()

    # Plivo has no create-time record flag — start full-call recording now, same
    # fire-and-forget pattern as the legacy /plivo/start route.
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
                print(f"[Stream] Failed to start Plivo full-call recording: {e}")
        try:
            _recording_pool.submit(_start_recording_bg)
        except Exception as e:
            print(f"[Stream] Could not queue recording-start task: {e}")

    ws_url = f"{_ws_base_url()}/ws/plivo/{interview_id}"
    print(f"[Stream] Plivo answer XML → {ws_url}")
    # keepCallAlive: the Stream element runs exclusively until the websocket closes.
    # audio/x-mulaw;rate=8000 matches Twilio's format so the shared pipeline is identical.
    # The trailing <Hangup/> is a defense-in-depth fallback (mirrors Twilio's XML):
    # normal/graceful teardown hangs up via PlivoFrameSerializer's auto_hang_up
    # (a direct REST call) before this is ever reached, but if the websocket dies
    # before that serializer is even constructed (e.g. a handshake failure), Plivo
    # would otherwise have nothing further to execute and the call could be left
    # connected indefinitely.
    return _xml(
        f"<Response>"
        f"<Stream bidirectional='true' keepCallAlive='true' "
        f"contentType='audio/x-mulaw;rate=8000'>"
        f"{ws_url}"
        f"</Stream>"
        f"<Hangup/>"
        f"</Response>"
    )


# ── Websockets ────────────────────────────────────────────────────────────────
@router.websocket("/ws/twilio/{interview_id}")
async def ws_twilio(websocket: WebSocket, interview_id: str):
    await websocket.accept()
    if not VOICE_AGENT_OK:
        await websocket.close()
        return
    try:
        await run_interview_session(websocket, "twilio", interview_id)
    except WebSocketDisconnect:
        print(f"[Stream] Twilio websocket disconnected interview={interview_id}")


@router.websocket("/ws/plivo/{interview_id}")
async def ws_plivo(websocket: WebSocket, interview_id: str):
    await websocket.accept()
    if not VOICE_AGENT_OK:
        await websocket.close()
        return
    try:
        await run_interview_session(websocket, "plivo", interview_id)
    except WebSocketDisconnect:
        print(f"[Stream] Plivo websocket disconnected interview={interview_id}")
