import { useState, useEffect } from 'react'
import { apiGenerateJd, apiCreateOpening, safeJson } from '../api/client'

const DRAFT_KEY = 'jd_builder_draft'

const EMPTY_FORM = {
  jobTitle:                '',
  experienceLevel:         '',
  responsibilities:        '',
  skills:                  '',
  goodToHave:              '',
  preferredQualifications: '',
  workMode:                '',
  perks:                   '',
  qualificationThreshold:  70,
}

const _loadDraft = () => {
  try { return JSON.parse(sessionStorage.getItem(DRAFT_KEY)) } catch { return null }
}

export default function JdBuilder({ onNavigate, syncOpenings }) {
  const savedDraft = _loadDraft()

  const [form, setForm]               = useState(savedDraft?.form ?? EMPTY_FORM)
  const [generatedJd, setGeneratedJd] = useState(savedDraft?.generatedJd ?? '')
  const [generating, setGenerating]   = useState(false)
  const [creating, setCreating]       = useState(false)
  const [error, setError]             = useState('')
  const [draftRestored, setDraftRestored] = useState(!!savedDraft?.generatedJd)

  // Auto-save draft to sessionStorage on every change
  useEffect(() => {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ form, generatedJd }))
  }, [form, generatedJd])

  const set = (field) => (e) => setForm(f => ({ ...f, [field]: e.target.value }))

  const canGenerate = (
    form.jobTitle.trim() &&
    form.experienceLevel &&
    form.responsibilities.trim() &&
    form.skills.trim() &&
    form.workMode
  )

  const handleGenerate = async () => {
    setGenerating(true)
    setError('')
    setGeneratedJd('')
    try {
      const res  = await apiGenerateJd(form)
      const data = await safeJson(res)
      if (!res.ok) throw new Error(data.detail || 'Generation failed.')
      setGeneratedJd(data.jd || '')
      setDraftRestored(false)
    } catch (e) {
      setError(e.message || 'Something went wrong. Please try again.')
    } finally {
      setGenerating(false)
    }
  }

  const handleCreate = async () => {
    if (!generatedJd.trim()) return
    setCreating(true)
    try {
      await apiCreateOpening({
        title:     form.jobTitle.trim(),
        jd:        generatedJd.trim(),
        createdAt: new Date().toISOString().slice(0, 10),
        qualification_threshold: Number(form.qualificationThreshold) || 70,
        jd_fields: {
          jobTitle:                form.jobTitle,
          experienceLevel:         form.experienceLevel,
          responsibilities:        form.responsibilities,
          skills:                  form.skills,
          goodToHave:              form.goodToHave,
          preferredQualifications: form.preferredQualifications,
          workMode:                form.workMode,
          perks:                   form.perks,
        },
      })
      await syncOpenings()
      sessionStorage.removeItem(DRAFT_KEY)
      onNavigate('dashboard')
    } catch (e) {
      setError('Failed to create opening. Please try again.')
      setCreating(false)
    }
  }

  const handleReset = () => {
    sessionStorage.removeItem(DRAFT_KEY)
    setForm(EMPTY_FORM)
    setGeneratedJd('')
    setError('')
    setDraftRestored(false)
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h2 style={{ fontSize: '1.35rem', fontWeight: 700, color: 'var(--text)', margin: 0 }}>
          JD Builder
        </h2>
        <p style={{ color: 'var(--text-2)', fontSize: '0.87rem', marginTop: 5, marginBottom: 0 }}>
          Answer a few questions and we'll generate a professional job description for NickelFox Technologies.
        </p>
      </div>

      {/* Draft restored notice */}
      {draftRestored && (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          background: 'var(--primary-subtle, #eff6ff)',
          border: '1px solid var(--primary-border, #bfdbfe)',
          borderRadius: 8, padding: '10px 16px', marginBottom: 16,
          fontSize: '0.84rem', color: 'var(--primary, #2563eb)',
        }}>
          <span>Draft restored — your previous work is still here.</span>
          <button
            onClick={handleReset}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--primary, #2563eb)', fontWeight: 600,
              fontSize: '0.82rem', padding: '2px 6px', textDecoration: 'underline',
            }}
          >
            Clear draft
          </button>
        </div>
      )}

      <div className="jd-builder-grid">

        {/* ── Left: Input form ── */}
        <div className="card" style={{ padding: 24 }}>
          <div className="input-card-title" style={{ marginBottom: 20 }}>Role Details</div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>

            {/* Q1: Job Title */}
            <div>
              <label style={labelStyle}>
                Job Title <span style={{ color: 'var(--red)' }}>*</span>
              </label>
              <input
                type="text"
                className="opening-form-input"
                value={form.jobTitle}
                onChange={set('jobTitle')}
                placeholder="e.g. Senior React Developer"
                style={{ width: '100%', boxSizing: 'border-box' }}
              />
            </div>

            {/* Q2: Experience Level */}
            <div>
              <label style={labelStyle}>
                Experience Level <span style={{ color: 'var(--red)' }}>*</span>
              </label>
              <select
                className="opening-form-input"
                value={form.experienceLevel}
                onChange={set('experienceLevel')}
                style={{ width: '100%', boxSizing: 'border-box' }}
              >
                <option value="">Select level…</option>
                <option value="Fresher (0-1 years)">Fresher (0–1 years)</option>
                <option value="Junior (1-3 years)">Junior (1–3 years)</option>
                <option value="Mid-level (3-5 years)">Mid-level (3–5 years)</option>
                <option value="Senior (5+ years)">Senior (5+ years)</option>
              </select>
            </div>

            {/* Q3: Key Responsibilities */}
            <div>
              <label style={labelStyle}>
                Key Responsibilities <span style={{ color: 'var(--red)' }}>*</span>
              </label>
              <textarea
                className="input-area"
                value={form.responsibilities}
                onChange={set('responsibilities')}
                placeholder="e.g. Build REST APIs, lead code reviews, collaborate with clients on feature specs, mentor junior developers…"
                style={{ height: 110 }}
              />
            </div>

            {/* Q4: Technical Skills */}
            <div>
              <label style={labelStyle}>
                Required Technical Skills & Stack <span style={{ color: 'var(--red)' }}>*</span>
              </label>
              <textarea
                className="input-area"
                value={form.skills}
                onChange={set('skills')}
                placeholder="e.g. React, Node.js, TypeScript, PostgreSQL, AWS, Docker"
                style={{ height: 90 }}
              />
            </div>

            {/* Q5: Good to Have Skills (optional) */}
            <div>
              <label style={labelStyle}>
                Good to Have Skills{' '}
                <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>(optional)</span>
              </label>
              <textarea
                className="input-area"
                value={form.goodToHave}
                onChange={set('goodToHave')}
                placeholder="e.g. GraphQL, Redis, CI/CD experience, open-source contributions"
                style={{ height: 80 }}
              />
            </div>

            {/* Q6: Preferred Qualifications (optional) */}
            <div>
              <label style={labelStyle}>
                Preferred Qualifications{' '}
                <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>(optional)</span>
              </label>
              <textarea
                className="input-area"
                value={form.preferredQualifications}
                onChange={set('preferredQualifications')}
                placeholder="e.g. B.Tech in CS or related field, AWS certification, prior experience at a product company"
                style={{ height: 80 }}
              />
            </div>

            {/* Q7: Work Mode */}
            <div>
              <label style={labelStyle}>
                Work Mode <span style={{ color: 'var(--red)' }}>*</span>
              </label>
              <select
                className="opening-form-input"
                value={form.workMode}
                onChange={set('workMode')}
                style={{ width: '100%', boxSizing: 'border-box' }}
              >
                <option value="">Select work mode…</option>
                <option value="On-site (Noida)">On-site (Noida)</option>
                <option value="Hybrid (Noida)">Hybrid (Noida)</option>
                <option value="Remote">Remote</option>
              </select>
            </div>

            {/* Q8: What We Offer (optional) */}
            <div>
              <label style={labelStyle}>
                What We Offer{' '}
                <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>(optional)</span>
              </label>
              <textarea
                className="input-area"
                value={form.perks}
                onChange={set('perks')}
                placeholder="e.g. mentorship programs, cross-domain projects, flexible hours, health insurance, learning budget"
                style={{ height: 80 }}
              />
            </div>

            {/* Qualification threshold — scoring config, not JD content */}
            <div>
              <label style={labelStyle}>
                Qualification Threshold
                <span style={{ color: 'var(--text-3)', fontWeight: 400 }}> (resume score to auto-mark "qualified")</span>
              </label>
              <input
                type="number"
                min={0}
                max={100}
                className="opening-form-input"
                value={form.qualificationThreshold}
                onChange={e => setForm(f => ({ ...f, qualificationThreshold: e.target.value }))}
                style={{ width: 120, boxSizing: 'border-box' }}
              />
            </div>

            <button
              className="btn-analyze"
              onClick={handleGenerate}
              disabled={!canGenerate || generating}
              style={{ width: '100%', marginTop: 4 }}
            >
              {generating
                ? <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                    <span className="spinner" /> Generating…
                  </span>
                : '✨ Generate JD'}
            </button>
          </div>
        </div>

        {/* ── Right: Generated JD ── */}
        <div className="card" style={{ padding: 24, minHeight: 420 }}>
          <div className="input-card-title" style={{ marginBottom: 20 }}>Generated JD</div>

          {/* Empty state */}
          {!generating && !generatedJd && !error && (
            <div style={emptyStateStyle}>
              <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="1.5" style={{ opacity: 0.35 }}>
                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <line x1="16" y1="13" x2="8" y2="13" />
                <line x1="16" y1="17" x2="8" y2="17" />
                <polyline points="10 9 9 9 8 9" />
              </svg>
              <p style={{ fontSize: '0.87rem', color: 'var(--text-3)', margin: 0, lineHeight: 1.6 }}>
                Fill in the details on the left<br />and click <strong>✨ Generate JD</strong>
              </p>
            </div>
          )}

          {/* Generating state */}
          {generating && (
            <div style={emptyStateStyle}>
              <span className="spinner-sm" style={{ width: 28, height: 28, borderWidth: 3 }} />
              <p style={{ fontSize: '0.87rem', color: 'var(--text-2)', margin: 0 }}>
                Generating your JD…
              </p>
            </div>
          )}

          {/* Error */}
          {error && !generating && (
            <div style={{
              color: 'var(--red)', background: 'var(--red-dim)',
              border: '1px solid var(--red-border)', borderRadius: 8,
              padding: '12px 16px', fontSize: '0.84rem', marginBottom: 16,
            }}>
              {error}
            </div>
          )}

          {/* Generated output */}
          {generatedJd && !generating && (
            <>
              <textarea
                className="input-area"
                value={generatedJd}
                onChange={e => setGeneratedJd(e.target.value)}
                style={{ height: 460, width: '100%', boxSizing: 'border-box' }}
              />
              <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
                <button
                  className="btn-clear"
                  onClick={handleReset}
                  style={{ flex: '0 0 auto', padding: '10px 20px' }}
                >
                  Start Over
                </button>
                <button
                  className="btn-clear"
                  onClick={handleGenerate}
                  disabled={generating}
                  title="Re-run generation using the current form fields — replaces the text below, including any manual edits"
                  style={{ flex: '0 0 auto', padding: '10px 20px' }}
                >
                  🔄 Regenerate
                </button>
                <CopyButton text={generatedJd} />
                <button
                  className="btn-analyze"
                  onClick={handleCreate}
                  disabled={creating}
                  style={{ flex: 1 }}
                >
                  {creating ? 'Creating…' : 'Create Opening with this JD →'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // fallback for older browsers
      const el = document.createElement('textarea')
      el.value = text
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }
  return (
    <button
      className="btn-clear"
      onClick={handleCopy}
      style={{ flex: '0 0 auto', padding: '10px 20px' }}
    >
      {copied ? '✓ Copied!' : 'Copy JD'}
    </button>
  )
}

const labelStyle = {
  display: 'block',
  fontSize: '0.82rem',
  fontWeight: 500,
  color: 'var(--text-2)',
  marginBottom: 6,
}

const emptyStateStyle = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  minHeight: 320,
  gap: 14,
  textAlign: 'center',
}
