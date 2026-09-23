import os
import json
from datetime import datetime
from sqlalchemy import create_engine, text as _sql

_db_engine = None


def _init_db():
    global _db_engine
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("[DB] DATABASE_URL not set — running without persistence")
        return
    for old, new in [("postgresql://", "postgresql+psycopg://"), ("postgres://", "postgresql+psycopg://")]:
        if db_url.startswith(old):
            db_url = new + db_url[len(old):]
            break
    try:
        _db_engine = create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS job_openings (
                    id         TEXT        PRIMARY KEY,
                    title      TEXT        NOT NULL,
                    jd_text    TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS interviews (
                    id                    TEXT        PRIMARY KEY,
                    opening_id            TEXT        REFERENCES job_openings(id) ON DELETE SET NULL,
                    status                TEXT        NOT NULL DEFAULT 'calling',
                    consent_status        TEXT        NOT NULL DEFAULT 'pending',
                    consent_raw           TEXT,
                    consent_re_asked      BOOLEAN     NOT NULL DEFAULT FALSE,
                    candidate_name        TEXT,
                    phone                 TEXT,
                    job_title             TEXT,
                    jd_text               TEXT,
                    twilio_call_sid       TEXT,
                    transcript            TEXT,
                    fail_reason           TEXT,
                    processing_step       TEXT,
                    callback_time_raw     TEXT,
                    callback_scheduled_at TIMESTAMPTZ,
                    questions             JSONB       NOT NULL DEFAULT '[]',
                    recordings            JSONB       NOT NULL DEFAULT '{}',
                    transcriptions        JSONB       NOT NULL DEFAULT '{}',
                    repeat_counts         JSONB       NOT NULL DEFAULT '{}',
                    score_result          JSONB,
                    call_log              JSONB       NOT NULL DEFAULT '[]',
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            # Migrations for columns added after initial release
            conn.execute(_sql("ALTER TABLE job_openings ADD COLUMN IF NOT EXISTS jd_fields JSONB"))
            conn.execute(_sql("ALTER TABLE job_openings ADD COLUMN IF NOT EXISTS qualification_threshold SMALLINT NOT NULL DEFAULT 70"))
            conn.execute(_sql("ALTER TABLE interviews ADD COLUMN IF NOT EXISTS opening_id TEXT REFERENCES job_openings(id) ON DELETE SET NULL"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_iv_status    ON interviews(status)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_iv_phone     ON interviews(phone)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_iv_opening   ON interviews(opening_id)"))
            conn.execute(_sql("""
                CREATE INDEX IF NOT EXISTS idx_iv_callback
                ON interviews(callback_scheduled_at) WHERE status = 'callback_scheduled'
            """))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS transcript_entries (
                    id             SERIAL      PRIMARY KEY,
                    interview_id   TEXT        NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    question_index INTEGER     NOT NULL,
                    question_text  TEXT        NOT NULL,
                    answer_text    TEXT,
                    recording_url  TEXT,
                    repeat_count   INTEGER     NOT NULL DEFAULT 0,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (interview_id, question_index)
                )
            """))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_te_iv ON transcript_entries(interview_id)"))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS batches (
                    id         TEXT        PRIMARY KEY,
                    opening_id TEXT        REFERENCES job_openings(id) ON DELETE SET NULL,
                    status     TEXT        NOT NULL DEFAULT 'processing',
                    jd_text    TEXT,
                    job_title  TEXT,
                    total      INTEGER     NOT NULL DEFAULT 0,
                    completed  INTEGER     NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            # Add opening_id to existing DBs that predate this column
            conn.execute(_sql("ALTER TABLE batches ADD COLUMN IF NOT EXISTS opening_id TEXT REFERENCES job_openings(id) ON DELETE SET NULL"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_batches_status  ON batches(status)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_batches_opening ON batches(opening_id)"))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS batch_candidates (
                    id                    SERIAL      PRIMARY KEY,
                    batch_id              TEXT        NOT NULL REFERENCES batches(id)    ON DELETE CASCADE,
                    interview_id          TEXT                 REFERENCES interviews(id) ON DELETE SET NULL,
                    file_name             TEXT        NOT NULL,
                    name                  TEXT,
                    email                 TEXT,
                    phone                 TEXT,
                    resume_score          SMALLINT,
                    filter_status         TEXT        NOT NULL DEFAULT 'pending',
                    interview_status      TEXT        NOT NULL DEFAULT 'pending',
                    interview_score       SMALLINT,
                    combined_score        SMALLINT,
                    callback_scheduled_at TIMESTAMPTZ,
                    resume_text           TEXT,
                    analyze_result        JSONB,
                    score_result          JSONB,
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (batch_id, file_name)
                )
            """))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_bc_batch      ON batch_candidates(batch_id)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_bc_interview  ON batch_candidates(interview_id)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_bc_filter     ON batch_candidates(filter_status)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_bc_iv_status  ON batch_candidates(interview_status)"))
            # Migrations for single-candidate support
            conn.execute(_sql("ALTER TABLE batch_candidates ALTER COLUMN batch_id DROP NOT NULL"))
            conn.execute(_sql("ALTER TABLE batch_candidates ADD COLUMN IF NOT EXISTS opening_id TEXT REFERENCES job_openings(id) ON DELETE SET NULL"))
            conn.execute(_sql("ALTER TABLE batch_candidates ADD COLUMN IF NOT EXISTS single_id TEXT"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_bc_opening    ON batch_candidates(opening_id)"))
            conn.execute(_sql("CREATE UNIQUE INDEX IF NOT EXISTS idx_bc_single ON batch_candidates(single_id) WHERE single_id IS NOT NULL"))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS users (
                    id            SERIAL      PRIMARY KEY,
                    username      TEXT        UNIQUE NOT NULL,
                    password_hash TEXT        NOT NULL,
                    role          TEXT        NOT NULL DEFAULT 'recruiter',
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            # Migrations for name + temp password support
            conn.execute(_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT"))
            conn.execute(_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE"))
            conn.execute(_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS temp_expires_at TIMESTAMPTZ"))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS pipelines (
                    id         TEXT        PRIMARY KEY,
                    opening_id TEXT        NOT NULL REFERENCES job_openings(id) ON DELETE CASCADE,
                    status     TEXT        NOT NULL DEFAULT 'running',
                    queue      JSONB       NOT NULL DEFAULT '[]',
                    active     JSONB       NOT NULL DEFAULT '{}',
                    completed  JSONB       NOT NULL DEFAULT '[]',
                    skipped    JSONB       NOT NULL DEFAULT '[]',
                    total      INT         NOT NULL DEFAULT 0,
                    jd_text    TEXT,
                    job_title  TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_pipelines_opening ON pipelines(opening_id)"))
            conn.execute(_sql("CREATE INDEX IF NOT EXISTS idx_pipelines_status  ON pipelines(status)"))
            # Migration: add jd_text/job_title for existing rows
            conn.execute(_sql("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS jd_text TEXT"))
            conn.execute(_sql("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS job_title TEXT"))
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key        TEXT        PRIMARY KEY,
                    value      TEXT        NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            # Resume scoring is an LLM call, so it is not perfectly reproducible even at
            # temperature=0 — the same resume against the same JD scored 51 once and 55 another
            # time (skill_match 5 vs 9), which looks like a broken system to a recruiter. The
            # in-process cache that was meant to prevent this died on every restart, so this
            # persists it: the same inputs return the byte-identical result forever.
            conn.execute(_sql("""
                CREATE TABLE IF NOT EXISTS analyze_cache (
                    key        TEXT        PRIMARY KEY,
                    result     JSONB       NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            conn.commit()
        print("[DB] PostgreSQL connected — tables ready")
    except Exception as e:
        print(f"[DB] Connection failed: {e} — running without persistence")
        _db_engine = None


def _get_setting(key: str) -> str | None:
    if not _db_engine:
        return None
    try:
        with _db_engine.connect() as conn:
            row = conn.execute(_sql("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).first()
            return row[0] if row else None
    except Exception as e:
        print(f"[DB] _get_setting failed: {e}")
        return None


def _set_setting(key: str, value: str):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (:k, :v, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """), {"k": key, "v": value})
            conn.commit()
    except Exception as e:
        print(f"[DB] _set_setting failed: {e}")


def _parse_interview_score(score_result: dict | None) -> int | None:
    """Extract the numeric interview score from a score_result's 'XX / 100' string.

    Single source of truth for this parse — previously copy-pasted with slightly
    different edge-case handling in database.py, batch.py (x2), and openings.py (x2).
    """
    if not score_result:
        return None
    raw = score_result.get("interview_score", "0 / 100")
    try:
        return int(str(raw).split("/")[0].strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _combined_score(resume_score, interview_score) -> int | None:
    """Weighted resume/interview combined score (40/60), or None if either half is missing."""
    if resume_score is None or interview_score is None:
        return None
    return round(resume_score * 0.4 + interview_score * 0.6)


def _get_cached_analysis(key: str) -> dict | None:
    """Previously computed resume analysis for these exact inputs, if any."""
    if not _db_engine or not key:
        return None
    try:
        with _db_engine.connect() as conn:
            row = conn.execute(_sql("SELECT result FROM analyze_cache WHERE key = :k"), {"k": key}).first()
            return row[0] if row else None
    except Exception as e:
        print(f"[DB] _get_cached_analysis failed: {e}")
        return None


def _set_cached_analysis(key: str, result: dict):
    """Store a resume analysis so identical inputs never re-score differently."""
    if not _db_engine or not key or not result:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO analyze_cache (key, result) VALUES (:k, CAST(:r AS jsonb))
                ON CONFLICT (key) DO NOTHING
            """), {"k": key, "r": json.dumps(result)})
            conn.commit()
    except Exception as e:
        print(f"[DB] _set_cached_analysis failed: {e}")


def _cb_ts(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


def _save_opening(oid: str, data: dict):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO job_openings (id, title, jd_text, jd_fields, qualification_threshold, created_at)
                VALUES (:id, :title, :jd_text, CAST(:jd_fields AS jsonb), :threshold, COALESCE(:created_at, NOW()))
                ON CONFLICT (id) DO UPDATE SET
                    title                   = EXCLUDED.title,
                    jd_text                 = EXCLUDED.jd_text,
                    jd_fields               = EXCLUDED.jd_fields,
                    qualification_threshold = EXCLUDED.qualification_threshold
            """), {
                "id":         oid,
                "title":      data.get("title", ""),
                "jd_text":    data.get("jd", ""),
                "jd_fields":  json.dumps(data.get("jd_fields") or {}),
                "threshold":  data.get("qualification_threshold", 70),
                "created_at": _cb_ts(data.get("createdAt")),
            })
            conn.commit()
    except Exception as e:
        print(f"[DB] _save_opening failed for {oid}: {e}")


def _delete_opening(oid: str):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("DELETE FROM job_openings WHERE id = :id"), {"id": oid})
            conn.commit()
    except Exception as e:
        print(f"[DB] _delete_opening failed for {oid}: {e}")


def _upsert_interview_stmt(conn, iid: str, data: dict):
    """Statement body shared by _save_interview() and _save_interview_complete().
    Does not commit — the caller's connection controls the transaction boundary."""
    conn.execute(_sql("""
                INSERT INTO interviews (
                    id, opening_id, status, consent_status, consent_raw, consent_re_asked,
                    candidate_name, phone, job_title, jd_text, twilio_call_sid,
                    transcript, fail_reason, processing_step, callback_time_raw,
                    callback_scheduled_at, questions, recordings, transcriptions,
                    repeat_counts, score_result, call_log, updated_at
                ) VALUES (
                    :id, (SELECT id FROM job_openings WHERE id = :opening_id), :status, :consent_status, :consent_raw, :consent_re_asked,
                    :candidate_name, :phone, :job_title, :jd_text, :twilio_call_sid,
                    :transcript, :fail_reason, :processing_step, :callback_time_raw,
                    :callback_scheduled_at, CAST(:questions AS jsonb), CAST(:recordings AS jsonb),
                    CAST(:transcriptions AS jsonb), CAST(:repeat_counts AS jsonb), CAST(:score_result AS jsonb),
                    CAST(:call_log AS jsonb), NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    opening_id            = EXCLUDED.opening_id,
                    status                = EXCLUDED.status,
                    consent_status        = EXCLUDED.consent_status,
                    consent_raw           = EXCLUDED.consent_raw,
                    consent_re_asked      = EXCLUDED.consent_re_asked,
                    candidate_name        = EXCLUDED.candidate_name,
                    phone                 = EXCLUDED.phone,
                    job_title             = EXCLUDED.job_title,
                    jd_text               = EXCLUDED.jd_text,
                    twilio_call_sid       = EXCLUDED.twilio_call_sid,
                    transcript            = EXCLUDED.transcript,
                    fail_reason           = EXCLUDED.fail_reason,
                    processing_step       = EXCLUDED.processing_step,
                    callback_time_raw     = EXCLUDED.callback_time_raw,
                    callback_scheduled_at = EXCLUDED.callback_scheduled_at,
                    questions             = EXCLUDED.questions,
                    recordings            = EXCLUDED.recordings,
                    transcriptions        = EXCLUDED.transcriptions,
                    repeat_counts         = EXCLUDED.repeat_counts,
                    score_result          = EXCLUDED.score_result,
                    call_log              = EXCLUDED.call_log,
                    updated_at            = NOW()
            """), {
                "id":                    iid,
                "opening_id":            data.get("opening_id"),
                "status":                data.get("status", "calling"),
                "consent_status":        data.get("consent_status", "pending"),
                "consent_raw":           data.get("consent_raw"),
                "consent_re_asked":      data.get("consent_re_asked", False),
                "candidate_name":        data.get("candidate_name"),
                "phone":                 data.get("phone"),
                "job_title":             data.get("job_title"),
                "jd_text":               data.get("jd_text"),
                "twilio_call_sid":       data.get("twilio_call_sid"),
                "transcript":            data.get("transcript"),
                "fail_reason":           data.get("fail_reason"),
                "processing_step":       data.get("processing_step"),
                "callback_time_raw":     data.get("callback_time_raw"),
                "callback_scheduled_at": _cb_ts(data.get("callback_scheduled_at")),
                "questions":             json.dumps(data.get("questions", [])),
                "recordings":            json.dumps({str(k): v for k, v in data.get("recordings", {}).items()}),
                "transcriptions":        json.dumps({str(k): v for k, v in data.get("transcriptions", {}).items()}),
                "repeat_counts":         json.dumps({str(k): v for k, v in data.get("repeat_counts", {}).items()}),
                "score_result":          json.dumps(data["score_result"]) if data.get("score_result") else None,
                "call_log":              json.dumps(data.get("call_log", [])),
            })


def _save_interview(iid: str, data: dict):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            _upsert_interview_stmt(conn, iid, data)
            conn.commit()
    except Exception as e:
        print(f"[DB] _save_interview failed for {iid}: {e}")


def _upsert_transcript_entries_stmt(conn, iid: str, data: dict):
    """Statement body shared by _save_transcript_entries() and _save_interview_complete()."""
    questions      = data.get("questions", [])
    transcriptions = data.get("transcriptions", {})
    recordings     = data.get("recordings", {})
    repeat_counts  = data.get("repeat_counts", {})
    if not questions:
        return
    for i, q_text in enumerate(questions):
        answer = transcriptions.get(i) or transcriptions.get(str(i))
        rec    = recordings.get(i) or recordings.get(str(i))
        rc     = repeat_counts.get(i, 0) or repeat_counts.get(str(i), 0)
        conn.execute(_sql("""
                    INSERT INTO transcript_entries
                        (interview_id, question_index, question_text, answer_text, recording_url, repeat_count)
                    VALUES (:iid, :idx, :q, :a, :rec, :rc)
                    ON CONFLICT (interview_id, question_index) DO UPDATE SET
                        question_text = EXCLUDED.question_text,
                        answer_text   = EXCLUDED.answer_text,
                        recording_url = EXCLUDED.recording_url,
                        repeat_count  = EXCLUDED.repeat_count
                """), {"iid": iid, "idx": i, "q": q_text, "a": answer, "rec": rec, "rc": rc or 0})


def _save_transcript_entries(iid: str, data: dict):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            _upsert_transcript_entries_stmt(conn, iid, data)
            conn.commit()
    except Exception as e:
        print(f"[DB] _save_transcript_entries failed for {iid}: {e}")


def _upsert_candidate_sync_stmt(conn, interview_id: str, data: dict):
    """Statement body shared by _sync_candidate_interview() and _save_interview_complete()."""
    status          = data.get("status", "pending")
    score_result    = data.get("score_result")
    interview_score = _parse_interview_score(score_result)
    row = conn.execute(_sql(
        "SELECT resume_score FROM batch_candidates WHERE interview_id = :iid LIMIT 1"
    ), {"iid": interview_id}).mappings().first()
    combined_score = _combined_score(row["resume_score"], interview_score) if row else None
    conn.execute(_sql("""
                UPDATE batch_candidates SET
                    interview_status      = :status,
                    interview_score       = :iscore,
                    combined_score        = :cscore,
                    score_result          = CAST(:score_result AS jsonb),
                    callback_scheduled_at = :cb_at,
                    updated_at            = NOW()
                WHERE interview_id = :iid
            """), {
                "iid":          interview_id,
                "status":       status,
                "iscore":       interview_score,
                "cscore":       combined_score,
                "score_result": json.dumps(score_result) if score_result else None,
                "cb_at":        _cb_ts(data.get("callback_scheduled_at")),
            })


def _sync_candidate_interview(interview_id: str, data: dict):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            _upsert_candidate_sync_stmt(conn, interview_id, data)
            conn.commit()
    except Exception as e:
        print(f"[DB] _sync_candidate_interview failed for {interview_id}: {e}")


def _save_interview_complete(iid: str, data: dict) -> bool:
    """Atomically persist a finished interview: the interviews row, its per-question
    transcript_entries, and the batch_candidates sync — all in one transaction.

    Previously these were three separate connections/commits (_save_interview,
    _save_transcript_entries, _sync_candidate_interview called in sequence) — if the
    second or third failed after the first succeeded, interviews and batch_candidates
    silently drifted out of sync with nothing beyond a printed log line. Now either
    all three land together or none do (the connection rolls back on exception since
    conn.commit() is never reached), and the caller gets a real True/False to act on
    instead of three independently-swallowed exceptions.
    """
    if not _db_engine:
        return False
    try:
        with _db_engine.connect() as conn:
            _upsert_interview_stmt(conn, iid, data)
            _upsert_transcript_entries_stmt(conn, iid, data)
            _upsert_candidate_sync_stmt(conn, iid, data)
            conn.commit()
        return True
    except Exception as e:
        print(f"[DB] _save_interview_complete failed for {iid} — transaction rolled back, interviews/batch_candidates NOT updated: {e}")
        return False


def _link_single_candidate_interview(single_id: str, interview_id: str):
    if not _db_engine or not single_id:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                UPDATE batch_candidates SET
                    interview_id     = :iid,
                    interview_status = 'calling',
                    updated_at       = NOW()
                WHERE single_id = :sid
            """), {"sid": single_id, "iid": interview_id})
            conn.commit()
    except Exception as e:
        print(f"[DB] _link_single_candidate_interview failed: {e}")


def _save_single_candidate(single_id: str, opening_id: str | None, resume_text: str, result: dict, threshold: int = 70):
    if not _db_engine:
        return
    score_str = result.get("match_score", "0 / 100")
    try:
        score_num = int(str(score_str).split("/")[0].strip())
    except Exception:
        score_num = 0
    filter_status = "qualified" if score_num >= threshold else "filtered_out"
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO batch_candidates (
                    batch_id, single_id, opening_id, file_name, name, email, phone,
                    resume_score, filter_status, interview_status,
                    resume_text, analyze_result, updated_at
                ) VALUES (
                    NULL, :single_id,
                    (SELECT id FROM job_openings WHERE id = :opening_id),
                    :file_name, :name, :email, :phone,
                    :resume_score, :filter_status, 'pending',
                    :resume_text, CAST(:analyze_result AS jsonb), NOW()
                )
                -- The WHERE predicate is REQUIRED, not decoration: single_id is covered by a
                -- PARTIAL unique index (idx_bc_single ... WHERE single_id IS NOT NULL), and
                -- Postgres will only infer a partial index for ON CONFLICT if the statement
                -- repeats its predicate. Without it every insert failed with "there is no
                -- unique or exclusion constraint matching the ON CONFLICT specification", so
                -- NO single-candidate row was ever saved — which meant single candidates never
                -- appeared in the rankings table and their interview scores had no row to sync
                -- to (also why resume_text came back missing for the Whisper vocabulary hint).
                ON CONFLICT (single_id) WHERE single_id IS NOT NULL DO UPDATE SET
                    opening_id    = EXCLUDED.opening_id,
                    name          = EXCLUDED.name,
                    email         = EXCLUDED.email,
                    phone         = EXCLUDED.phone,
                    resume_score  = EXCLUDED.resume_score,
                    filter_status = EXCLUDED.filter_status,
                    resume_text   = EXCLUDED.resume_text,
                    analyze_result= EXCLUDED.analyze_result,
                    updated_at    = NOW()
            """), {
                "single_id":     single_id,
                "opening_id":    opening_id,
                "file_name":     result.get("name") or "Single Candidate",
                "name":          result.get("name"),
                "email":         result.get("email"),
                "phone":         result.get("phone"),
                "resume_score":  score_num,
                "filter_status": filter_status,
                "resume_text":   resume_text,
                "analyze_result": json.dumps(result),
            })
            conn.commit()
    except Exception as e:
        print(f"[DB] _save_single_candidate failed for {single_id}: {e}")


def _save_batch(bid: str, data: dict):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO batches (id, opening_id, status, jd_text, job_title, total, completed, updated_at)
                VALUES (:id, (SELECT id FROM job_openings WHERE id = :opening_id), :status, :jd_text, :job_title, :total, :completed, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    opening_id = EXCLUDED.opening_id,
                    status     = EXCLUDED.status,
                    jd_text    = EXCLUDED.jd_text,
                    job_title  = EXCLUDED.job_title,
                    total      = EXCLUDED.total,
                    completed  = EXCLUDED.completed,
                    updated_at = NOW()
            """), {
                "id":         bid,
                "opening_id": data.get("opening_id"),
                "status":     data.get("status", "processing"),
                "jd_text":    data.get("jd_text"),
                "job_title":  data.get("job_title"),
                "total":      data.get("total", 0),
                "completed":  data.get("completed", 0),
            })
            for c in data.get("candidates", []):
                result = conn.execute(_sql("""
                    INSERT INTO batch_candidates (
                        batch_id, interview_id, file_name, name, email, phone,
                        resume_score, filter_status, interview_status, interview_score,
                        combined_score, callback_scheduled_at, resume_text,
                        analyze_result, score_result, updated_at
                    ) VALUES (
                        :batch_id, (SELECT id FROM interviews WHERE id = :interview_id), :file_name, :name, :email, :phone,
                        :resume_score, :filter_status, :interview_status, :interview_score,
                        :combined_score, :callback_scheduled_at, :resume_text,
                        CAST(:analyze_result AS jsonb), CAST(:score_result AS jsonb), NOW()
                    )
                    ON CONFLICT (batch_id, file_name) DO UPDATE SET
                        interview_id          = EXCLUDED.interview_id,
                        name                  = EXCLUDED.name,
                        email                 = EXCLUDED.email,
                        phone                 = EXCLUDED.phone,
                        resume_score          = EXCLUDED.resume_score,
                        filter_status         = EXCLUDED.filter_status,
                        interview_status      = EXCLUDED.interview_status,
                        interview_score       = EXCLUDED.interview_score,
                        combined_score        = EXCLUDED.combined_score,
                        callback_scheduled_at = EXCLUDED.callback_scheduled_at,
                        resume_text           = EXCLUDED.resume_text,
                        analyze_result        = EXCLUDED.analyze_result,
                        score_result          = EXCLUDED.score_result,
                        updated_at            = NOW()
                    RETURNING id
                """), {
                    "batch_id":              bid,
                    "interview_id":          c.get("interview_id"),
                    "file_name":             c.get("file_name"),
                    "name":                  c.get("name"),
                    "email":                 c.get("email"),
                    "phone":                 c.get("phone"),
                    "resume_score":          c.get("resume_score"),
                    "filter_status":         c.get("filter_status", "pending"),
                    "interview_status":      c.get("interview_status", "pending"),
                    "interview_score":       c.get("interview_score"),
                    "combined_score":        c.get("combined_score"),
                    "callback_scheduled_at": _cb_ts(c.get("callback_scheduled_at")),
                    "resume_text":           c.get("resume_text"),
                    "analyze_result":        json.dumps(c["analyze_result"]) if c.get("analyze_result") else None,
                    "score_result":          json.dumps(c["score_result"])    if c.get("score_result")    else None,
                })
                row = result.first()
                if row:
                    # Attach the DB-assigned PK back onto the same in-memory candidate dict
                    # (data["candidates"] is the live batch_store entry, not a copy) so later
                    # /batch/status polls can reference it — e.g. the qualify-override action.
                    c["_bc_id"] = row[0]
            conn.commit()
    except Exception as e:
        print(f"[DB] _save_batch failed for {bid}: {e}")


import hashlib, secrets as _secrets

def _hash_password(password: str) -> str:
    salt = _secrets.token_hex(16)
    h    = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"

def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False

def _get_user(username: str) -> dict | None:
    if not _db_engine:
        return None
    try:
        with _db_engine.connect() as conn:
            row = conn.execute(_sql("SELECT * FROM users WHERE username = :u"), {"u": username}).mappings().first()
            if not row:
                return None
            return {
                "id":                   row["id"],
                "username":             row["username"],
                "full_name":            row.get("full_name"),
                "password_hash":        row["password_hash"],
                "role":                 row["role"],
                "must_change_password": row.get("must_change_password", False),
                "temp_expires_at":      row["temp_expires_at"].isoformat() if row.get("temp_expires_at") else None,
            }
    except Exception as e:
        print(f"[DB] _get_user failed: {e}")
        return None

def _create_user(username: str, password: str, role: str = "recruiter") -> bool:
    if not _db_engine:
        return False
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO users (username, password_hash, role)
                VALUES (:u, :ph, :r)
                ON CONFLICT (username) DO NOTHING
            """), {"u": username, "ph": _hash_password(password), "r": role})
            conn.commit()
        return True
    except Exception as e:
        print(f"[DB] _create_user failed: {e}")
        return False

def _list_users() -> list[dict]:
    if not _db_engine:
        return []
    try:
        with _db_engine.connect() as conn:
            rows = conn.execute(_sql("SELECT id, username, full_name, role, must_change_password, created_at FROM users ORDER BY created_at")).mappings().all()
            return [{"id": r["id"], "username": r["username"], "full_name": r.get("full_name"), "role": r["role"], "must_change_password": r.get("must_change_password", False)} for r in rows]
    except Exception as e:
        print(f"[DB] _list_users failed: {e}")
        return []

def _create_user_with_email(email: str, full_name: str, role: str) -> str | None:
    if not _db_engine:
        return None
    from datetime import datetime, timedelta, timezone
    from backend.services.email_service import generate_temp_password
    temp_pass = generate_temp_password(full_name)
    expires   = datetime.now(timezone.utc) + timedelta(hours=24)
    try:
        with _db_engine.connect() as conn:
            result = conn.execute(_sql("""
                INSERT INTO users (username, full_name, password_hash, role, must_change_password, temp_expires_at)
                VALUES (:u, :name, :ph, :r, TRUE, :exp)
                ON CONFLICT (username) DO NOTHING
                RETURNING id
            """), {"u": email, "name": full_name, "ph": _hash_password(temp_pass), "r": role, "exp": expires})
            if not result.first():
                return None  # username already exists
            conn.commit()
        return temp_pass
    except Exception as e:
        print(f"[DB] _create_user_with_email failed: {e}")
        return None


def _clear_temp_password(username: str):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                UPDATE users SET must_change_password=FALSE, temp_expires_at=NULL WHERE username=:u
            """), {"u": username})
            conn.commit()
    except Exception as e:
        print(f"[DB] _clear_temp_password failed: {e}")


def _seed_super_admin(username: str, password: str):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            # DO NOTHING (not DO UPDATE) so a director who has changed their password
            # is never silently reset back to SUPER_ADMIN_PASSWORD on every restart.
            # First run still seeds the account; later runs leave the existing row intact.
            conn.execute(_sql("""
                INSERT INTO users (username, password_hash, role, full_name)
                VALUES (:u, :ph, 'super_admin', 'Director')
                ON CONFLICT (username) DO NOTHING
            """), {"u": username, "ph": _hash_password(password)})
            conn.commit()
    except Exception as e:
        print(f"[DB] _seed_super_admin failed: {e}")


def _delete_user(username: str) -> bool:
    if not _db_engine:
        return False
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("DELETE FROM users WHERE username = :u"), {"u": username})
            conn.commit()
        return True
    except Exception as e:
        print(f"[DB] _delete_user failed: {e}")
        return False


def _load_interview(interview_id: str) -> dict | None:
    if not _db_engine:
        return None
    try:
        with _db_engine.connect() as conn:
            row = conn.execute(_sql("SELECT * FROM interviews WHERE id = :id"), {"id": interview_id}).mappings().first()
            if not row:
                return None
            cb = row["callback_scheduled_at"]
            return {
                "interview_id":          row["id"],
                "opening_id":            row["opening_id"],
                "status":                row["status"],
                "consent_status":        row["consent_status"],
                "consent_raw":           row["consent_raw"],
                "consent_re_asked":      row["consent_re_asked"],
                "candidate_name":        row["candidate_name"],
                "phone":                 row["phone"],
                "job_title":             row["job_title"],
                "jd_text":               row["jd_text"],
                "twilio_call_sid":       row["twilio_call_sid"],
                "transcript":            row["transcript"],
                "fail_reason":           row["fail_reason"],
                "processing_step":       row["processing_step"],
                "callback_time_raw":     row["callback_time_raw"],
                "callback_scheduled_at": cb.isoformat() if cb else None,
                "questions":             row["questions"] or [],
                "recordings":            {int(k): v for k, v in (row["recordings"] or {}).items()},
                "transcriptions":        {int(k): v for k, v in (row["transcriptions"] or {}).items()},
                "repeat_counts":         {int(k): v for k, v in (row["repeat_counts"] or {}).items()},
                "score_result":          row["score_result"],
                "call_log":              row["call_log"] or [],
            }
    except Exception as e:
        print(f"[DB] _load_interview failed for {interview_id}: {e}")
        return None


def _save_pipeline(pipeline_id: str, data: dict):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                INSERT INTO pipelines (id, opening_id, status, queue, active, completed, skipped, total, jd_text, job_title, updated_at)
                VALUES (:id, :opening_id, :status, CAST(:queue AS jsonb), CAST(:active AS jsonb),
                        CAST(:completed AS jsonb), CAST(:skipped AS jsonb), :total, :jd_text, :job_title, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    queue      = EXCLUDED.queue,
                    active     = EXCLUDED.active,
                    completed  = EXCLUDED.completed,
                    skipped    = EXCLUDED.skipped,
                    total      = EXCLUDED.total,
                    jd_text    = EXCLUDED.jd_text,
                    job_title  = EXCLUDED.job_title,
                    updated_at = NOW()
            """), {
                "id":         pipeline_id,
                "opening_id": data.get("opening_id"),
                "status":     data.get("status", "running"),
                "queue":      json.dumps(data.get("queue", [])),
                "active":     json.dumps(data.get("active", {})),
                "completed":  json.dumps(data.get("completed", [])),
                "skipped":    json.dumps(data.get("skipped", [])),
                "total":      data.get("total", 0),
                "jd_text":    data.get("jd_text"),
                "job_title":  data.get("job_title"),
            })
            conn.commit()
    except Exception as e:
        print(f"[DB] _save_pipeline failed for {pipeline_id}: {e}")


def _load_pipelines() -> dict:
    if not _db_engine:
        return {}
    try:
        with _db_engine.connect() as conn:
            rows = conn.execute(_sql(
                "SELECT * FROM pipelines WHERE status = 'running' ORDER BY created_at"
            )).mappings().all()
            result = {}
            for row in rows:
                pid = row["id"]
                result[pid] = {
                    "pipeline_id":   pid,
                    "opening_id":    row["opening_id"],
                    "status":        row["status"],
                    "queue":         list(row["queue"] or []),
                    "active":        dict(row["active"] or {}),
                    "completed":     list(row["completed"] or []),
                    "skipped":       list(row["skipped"] or []),
                    "total":         row["total"],
                    "max_concurrent": 3,
                    "jd_text":       row.get("jd_text"),
                    "job_title":     row.get("job_title"),
                }
            return result
    except Exception as e:
        print(f"[DB] _load_pipelines failed: {e}")
        return {}


def _delete_interview(interview_id: str):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("DELETE FROM interviews WHERE id = :id"), {"id": interview_id})
            conn.commit()
    except Exception as e:
        print(f"[DB] _delete_interview failed for {interview_id}: {e}")


def _list_repairable_interviews(minutes: int = 30, limit: int = 25) -> list[str]:
    """Just-finished interviews flagged incomplete, for the answer-repair safety net.

    An interview lands here when a recording arrived (or became transcribable) only after
    scoring had finished, so the row reads "incomplete" while the provider holds the audio.

    The window is short on purpose: this exists to get the CURRENT call right, not to rewrite
    history. An older result may already have been acted on, and re-scoring the same transcript
    does not reproduce an identical number, so silently revising it would do more harm than the
    stale flag it fixes.
    """
    if not _db_engine:
        return []
    try:
        with _db_engine.connect() as conn:
            rows = conn.execute(_sql(f"""
                SELECT id FROM interviews
                 WHERE status = 'completed'
                   AND score_result IS NOT NULL
                   AND (score_result ->> 'incomplete') = 'true'
                   AND updated_at > NOW() - INTERVAL '{int(minutes)} minutes'
                 ORDER BY updated_at DESC
                 LIMIT :lim
            """), {"lim": limit}).fetchall()
            return [str(r[0]) for r in rows]
    except Exception as e:
        print(f"[DB] _list_repairable_interviews failed: {e}")
        return []


def _get_resume_text_for_interview(interview_id: str) -> str | None:
    """Resume text for the candidate linked to this interview, if any.

    Used to seed Whisper's vocabulary hint with the candidate's real proper nouns (college,
    employers, technologies) so they aren't mis-transcribed. interview_store doesn't carry
    resume_text, so it's read from batch_candidates on demand — only from the post-call
    scoring pass, never on a latency-sensitive webhook path.
    """
    if not _db_engine:
        return None
    try:
        with _db_engine.connect() as conn:
            row = conn.execute(_sql(
                "SELECT resume_text FROM batch_candidates WHERE interview_id = :iid AND resume_text IS NOT NULL LIMIT 1"
            ), {"iid": interview_id}).first()
            return row[0] if row else None
    except Exception as e:
        print(f"[DB] _get_resume_text_for_interview failed for {interview_id}: {e}")
        return None


def _get_interview_status_from_db(interview_id: str):
    if not _db_engine:
        return None
    try:
        with _db_engine.connect() as conn:
            row = conn.execute(
                _sql("SELECT status FROM interviews WHERE id = :id"), {"id": interview_id}
            ).fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[DB] _get_interview_status_from_db failed for {interview_id}: {e}")
        return None


def _get_qualified_candidates_for_opening(opening_id: str) -> list:
    if not _db_engine:
        return []
    try:
        with _db_engine.connect() as conn:
            rows = conn.execute(_sql("""
                SELECT bc.id, bc.batch_id, bc.single_id, bc.file_name, bc.name, bc.phone,
                       bc.resume_text, bc.resume_score, bc.interview_status,
                       COALESCE(b.jd_text, jo.jd_text)  AS jd_text,
                       COALESCE(b.job_title, jo.title)   AS job_title
                FROM batch_candidates bc
                LEFT JOIN batches b      ON bc.batch_id = b.id
                LEFT JOIN job_openings jo ON jo.id = :oid
                WHERE (b.opening_id = :oid OR bc.opening_id = :oid)
                  AND bc.filter_status     = 'qualified'
                  AND bc.phone             IS NOT NULL
                  AND bc.interview_status  NOT IN ('completed', 'processing', 'calling', 'callback_scheduled', 'declined')
                ORDER BY bc.resume_score DESC NULLS LAST
            """), {"oid": opening_id}).mappings().all()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB] _get_qualified_candidates_for_opening failed: {e}")
        return []


def _link_batch_candidate_interview(batch_id: str, file_name: str, interview_id: str):
    if not _db_engine:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                UPDATE batch_candidates SET
                    interview_id     = :iid,
                    interview_status = 'calling',
                    updated_at       = NOW()
                WHERE batch_id = :bid AND file_name = :fn
            """), {"bid": batch_id, "fn": file_name, "iid": interview_id})
            conn.commit()
    except Exception as e:
        print(f"[DB] _link_batch_candidate_interview failed: {e}")


def _link_candidate_interview_by_bcid(bc_id: int, interview_id: str):
    """Link by batch_candidates.id primary key — works regardless of batch_id/single_id."""
    if not _db_engine or not bc_id:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                UPDATE batch_candidates SET
                    interview_id     = :iid,
                    interview_status = 'calling',
                    updated_at       = NOW()
                WHERE id = :bcid
            """), {"bcid": bc_id, "iid": interview_id})
            conn.commit()
    except Exception as e:
        print(f"[DB] _link_candidate_interview_by_bcid failed: {e}")


def _override_qualify_candidate(bc_id: int) -> bool:
    """Manually promote a filtered-out (but callable) candidate to 'qualified'.

    Guarded on phone IS NOT NULL — a candidate with no phone number can't
    actually be called, so promoting them wouldn't be meaningful (mirrors the
    existing filter_status='no_phone' invariant: qualified implies callable).
    Returns False if the row doesn't exist or has no phone number.
    """
    if not _db_engine or not bc_id:
        return False
    try:
        with _db_engine.connect() as conn:
            result = conn.execute(_sql("""
                UPDATE batch_candidates
                SET filter_status = 'qualified', updated_at = NOW()
                WHERE id = :bcid AND phone IS NOT NULL
                RETURNING id
            """), {"bcid": bc_id})
            row = result.first()
            conn.commit()
            return row is not None
    except Exception as e:
        print(f"[DB] _override_qualify_candidate failed for {bc_id}: {e}")
        return False


def _reset_candidate_on_call_failure(bc_id: int, interview_id: str):
    """Reset batch_candidates after _start_candidate_call fails — clears stale 'calling' state."""
    if not _db_engine or not bc_id:
        return
    try:
        with _db_engine.connect() as conn:
            conn.execute(_sql("""
                UPDATE batch_candidates
                SET interview_id     = NULL,
                    interview_status = NULL,
                    updated_at       = NOW()
                WHERE id = :bcid AND interview_id = :iid
            """), {"bcid": bc_id, "iid": interview_id})
            conn.commit()
    except Exception as e:
        print(f"[DB] _reset_candidate_on_call_failure failed: {e}")


def load_stores() -> tuple[dict, dict, dict]:
    if not _db_engine:
        return {}, {}, {}

    def _int_keys(d):
        return {int(k): v for k, v in d.items()} if d else {}

    ivs = {}
    batches = {}
    try:
        with _db_engine.connect() as conn:
            openings = {}
            for orow in conn.execute(_sql("SELECT * FROM job_openings ORDER BY created_at")).mappings():
                ca = orow["created_at"]
                openings[orow["id"]] = {
                    "id":        orow["id"],
                    "title":     orow["title"],
                    "jd":        orow["jd_text"] or "",
                    "jd_fields": orow["jd_fields"] or {},
                    "qualification_threshold": orow.get("qualification_threshold", 70),
                    "createdAt": ca.date().isoformat() if ca else "",
                    "stats":     {"total": 0, "qualified": 0, "done": 0},
                    "batchIds":  [],
                    "candidates": [],
                }
            for row in conn.execute(_sql("SELECT * FROM interviews ORDER BY created_at")).mappings():
                cb = row["callback_scheduled_at"]
                ivs[row["id"]] = {
                    "interview_id":          row["id"],
                    "opening_id":            row["opening_id"],
                    "status":                row["status"],
                    "consent_status":        row["consent_status"],
                    "consent_raw":           row["consent_raw"],
                    "consent_re_asked":      row["consent_re_asked"],
                    "candidate_name":        row["candidate_name"],
                    "phone":                 row["phone"],
                    "job_title":             row["job_title"],
                    "jd_text":               row["jd_text"],
                    "twilio_call_sid":       row["twilio_call_sid"],
                    "transcript":            row["transcript"],
                    "fail_reason":           row["fail_reason"],
                    "processing_step":       row["processing_step"],
                    "callback_time_raw":     row["callback_time_raw"],
                    "callback_scheduled_at": cb.isoformat() if cb else None,
                    "questions":             row["questions"] or [],
                    "recordings":            _int_keys(row["recordings"]),
                    "transcriptions":        _int_keys(row["transcriptions"]),
                    "repeat_counts":         _int_keys(row["repeat_counts"]),
                    "score_result":          row["score_result"],
                    "call_log":              row["call_log"] or [],
                }
            for brow in conn.execute(_sql("SELECT * FROM batches ORDER BY created_at")).mappings():
                bid = brow["id"]
                candidates = []
                for crow in conn.execute(
                    _sql("SELECT * FROM batch_candidates WHERE batch_id = :b ORDER BY id"), {"b": bid}
                ).mappings():
                    cb = crow["callback_scheduled_at"]
                    candidates.append({
                        "file_name":             crow["file_name"],
                        "resume_text":           crow["resume_text"],
                        "name":                  crow["name"],
                        "email":                 crow["email"],
                        "phone":                 crow["phone"],
                        "resume_score":          crow["resume_score"],
                        "analyze_result":        crow["analyze_result"],
                        "filter_status":         crow["filter_status"],
                        "interview_id":          crow["interview_id"],
                        "interview_status":      crow["interview_status"],
                        "interview_score":       crow["interview_score"],
                        "combined_score":        crow["combined_score"],
                        "callback_scheduled_at": cb.isoformat() if cb else None,
                        "score_result":          crow["score_result"],
                    })
                batches[bid] = {
                    "batch_id":   bid,
                    "opening_id": brow["opening_id"],
                    "status":     brow["status"],
                    "jd_text":    brow["jd_text"],
                    "job_title":  brow["job_title"],
                    "total":      brow["total"],
                    "completed":  brow["completed"],
                    "candidates": candidates,
                }
            for crow in conn.execute(_sql("""
                SELECT * FROM batch_candidates WHERE batch_id IS NULL ORDER BY created_at
            """)).mappings():
                oid = crow.get("opening_id")
                if not oid or oid not in openings:
                    continue
                cb = crow.get("callback_scheduled_at")
                openings[oid]["candidates"].append({
                    "_singleId":         crow["single_id"] or str(crow["id"]),
                    "_batchId":          None,
                    "_type":             "single",
                    "file_name":         crow["name"] or "Single Candidate",
                    "name":              crow["name"],
                    "email":             crow["email"],
                    "phone":             crow["phone"],
                    "resume_score":      crow["resume_score"],
                    "analyze_result":    crow["analyze_result"],
                    "filter_status":     crow["filter_status"],
                    "interview_id":      crow["interview_id"],
                    "interview_status":  crow["interview_status"],
                    "interview_score":   crow["interview_score"],
                    "combined_score":    crow["combined_score"],
                    "callback_scheduled_at": cb.isoformat() if cb else None,
                    "score_result":      crow["score_result"],
                    "interview_status":  crow["interview_status"] or "pending",
                    "score_result":      crow["score_result"],
                    "transcript":        None,
                    "questions":         [],
                })
        print(f"[DB] Loaded {len(openings)} openings, {len(ivs)} interviews, {len(batches)} batches from PostgreSQL")
    except Exception as e:
        print(f"[DB] load_stores failed: {e}")
        openings = {}
    return ivs, batches, openings
