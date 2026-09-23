from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from backend.services.analyzer import analyze, parse_resume
from backend.utils.file_utils import extract_text
from backend.app.database import _save_single_candidate
from backend.app.state import opening_store, get_qualification_threshold

router = APIRouter()


class AnalyzeRequest(BaseModel):
    resume_text: str
    jd_text:     str
    opening_id:  str | None = None
    single_id:   str | None = None


class ParseRequest(BaseModel):
    resume_text: str


@router.post("/analyze")
async def analyze_resume(req: AnalyzeRequest):
    if not req.resume_text.strip(): raise HTTPException(400, "Resume text is required.")
    if not req.jd_text.strip():     raise HTTPException(400, "Job description is required.")
    jd_fields = None
    if req.opening_id:
        opening = opening_store.get(req.opening_id)
        if opening:
            jd_fields = opening.get('jd_fields') or None
    result = analyze(req.resume_text, req.jd_text, jd_fields=jd_fields)
    if req.single_id:
        threshold = get_qualification_threshold(req.opening_id)
        _save_single_candidate(req.single_id, req.opening_id, req.resume_text, result, threshold=threshold)
    return result


@router.post("/parse")
async def parse_only(req: ParseRequest):
    if not req.resume_text.strip(): raise HTTPException(400, "Resume text is required.")
    return parse_resume(req.resume_text)


@router.post("/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = extract_text(content, file.filename or "")
    except ValueError as e:
        code = 400 if "supported" in str(e) else 422
        raise HTTPException(status_code=code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to parse file: {e}")
    return {"resume_text": text}
