from fastapi import APIRouter, Depends, HTTPException
from backend.app.state import settings_store
from backend.app.database import _set_setting
from backend.api.routes.auth import _require_super_admin

router = APIRouter()


@router.get("/settings")
async def get_settings(session=Depends(_require_super_admin)):
    return {
        "call_provider":  settings_store.get("call_provider", "twilio"),
        "interview_mode": settings_store.get("interview_mode", "legacy"),
    }


@router.put("/settings")
async def update_settings(body: dict, session=Depends(_require_super_admin)):
    if "call_provider" in body:
        provider = body["call_provider"]
        if provider not in ("twilio", "plivo"):
            raise HTTPException(400, "Invalid provider. Must be 'twilio' or 'plivo'.")
        settings_store["call_provider"] = provider
        _set_setting("call_provider", provider)

    if "interview_mode" in body:
        mode = body["interview_mode"]
        if mode not in ("legacy", "streaming"):
            raise HTTPException(400, "Invalid interview_mode. Must be 'legacy' or 'streaming'.")
        settings_store["interview_mode"] = mode
        _set_setting("interview_mode", mode)

    return {
        "call_provider":  settings_store.get("call_provider", "twilio"),
        "interview_mode": settings_store.get("interview_mode", "legacy"),
    }
