export const safeJson = async (res) => {
  try { return await res.json() } catch { return {} }
}

// Central auth-header helper — attaches the JWT to every request so all
// protected backend routes accept the call. Returns a fresh object each time
// (sessionStorage token may change after a password change re-issues a token).
const authHeaders = (extra = {}) => {
  const token = sessionStorage.getItem('auth_token')
  return {
    ...extra,
    ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
  }
}

const jsonHeaders = () => authHeaders({ 'Content-Type': 'application/json' })

export const apiAnalyze = (resumeText, jdText, signal, openingId, singleId) =>
  fetch('/analyze', {
    method:  'POST',
    headers: jsonHeaders(),
    body:    JSON.stringify({ resume_text: resumeText, jd_text: jdText, opening_id: openingId || null, single_id: singleId || null }),
    signal,
  })

export const apiStartInterview = (payload) =>
  fetch('/interview/start', {
    method:  'POST',
    headers: jsonHeaders(),
    body:    JSON.stringify(payload),
  })

export const apiInterviewStatus = (callId) =>
  fetch(`/interview/status/${callId}`, { headers: authHeaders() })

export const apiRecallInterview = (interviewId) =>
  fetch(`/interview/recall/${interviewId}`, { method: 'POST', headers: authHeaders() })

export const apiStartBatch = (formData) =>
  fetch('/batch/start', { method: 'POST', headers: authHeaders(), body: formData })

export const apiBatchStatus = (batchId) =>
  fetch(`/batch/status/${batchId}`, { headers: authHeaders() })

export const apiBatchInterviewStart = (batchId, fileName) =>
  fetch(`/batch/${batchId}/interview/start`, {
    method:  'POST',
    headers: jsonHeaders(),
    body:    JSON.stringify({ file_name: fileName }),
  })

export const apiCallbacksDue = () =>
  fetch('/callbacks/due', { headers: authHeaders() })

export const apiGetOpenings = () =>
  fetch('/openings', { headers: authHeaders() })

export const apiGetOpeningsFull = () =>
  fetch('/openings/full', { headers: authHeaders() })

export const apiCreateOpening = (opening) =>
  fetch('/openings', {
    method:  'POST',
    headers: jsonHeaders(),
    body:    JSON.stringify(opening),
  })

export const apiUpdateOpening = (id, data) =>
  fetch(`/openings/${id}`, {
    method:  'PUT',
    headers: jsonHeaders(),
    body:    JSON.stringify(data),
  })

export const apiDeleteOpening = (id) =>
  fetch(`/openings/${id}`, { method: 'DELETE', headers: authHeaders() })

export const apiGetOpeningCandidates = (id) =>
  fetch(`/openings/${id}/candidates`, { headers: authHeaders() })

export const apiForceResolve = (interviewId) =>
  fetch(`/interview/force-resolve/${interviewId}`, { method: 'POST', headers: authHeaders() })

export const apiQualifyCandidate = (bcId) =>
  fetch('/batch/candidates/qualify', {
    method:  'PUT',
    headers: jsonHeaders(),
    body:    JSON.stringify({ bc_id: bcId }),
  })

export const apiActiveCalls = () =>
  fetch('/calls/active', { headers: authHeaders() })

export const apiStartPipeline = (openingId) =>
  fetch(`/openings/${openingId}/pipeline/start`, { method: 'POST', headers: authHeaders() })

export const apiPipelineStatus = (openingId) =>
  fetch(`/openings/${openingId}/pipeline/status`, { headers: authHeaders() })

export const apiStopPipeline = (openingId) =>
  fetch(`/openings/${openingId}/pipeline/stop`, { method: 'POST', headers: authHeaders() })

export const apiGetSettings = () =>
  fetch('/settings', { headers: authHeaders() })

export const apiUpdateSettings = (data) =>
  fetch('/settings', {
    method:  'PUT',
    headers: jsonHeaders(),
    body:    JSON.stringify(data),
  })

export const apiGenerateJd = (form) =>
  fetch('/openings/generate-jd', {
    method:  'POST',
    headers: jsonHeaders(),
    body:    JSON.stringify({
      job_title:        form.jobTitle,
      experience_level: form.experienceLevel,
      responsibilities: form.responsibilities,
      skills:           form.skills,
      good_to_have:     form.goodToHave  || '',
      preferred_qualifications: form.preferredQualifications || '',
      work_mode:        form.workMode,
      perks:            form.perks       || '',
    }),
  })
