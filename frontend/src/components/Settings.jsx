import { useState, useEffect } from 'react'
import { apiGetSettings, apiUpdateSettings, safeJson } from '../api/client'

const MODE_INFO = {
  legacy: {
    label: 'Classic (press #)',
    desc:  'Webhook flow — candidates press the # key when they finish an answer. No extra API keys needed.',
  },
  streaming: {
    label: 'Streaming AI agent',
    desc:  'Real-time voice agent — detects automatically when the candidate finishes answering, no # key. Requires DEEPGRAM_API_KEY + AZURE_SPEECH_KEY.',
  },
}

export default function Settings() {
  const [provider, setProvider] = useState(null)
  const [mode, setMode]         = useState(null)
  const [saving, setSaving]     = useState(false)
  const [message, setMessage]   = useState(null)
  const [loading, setLoading]   = useState(true)

  useEffect(() => {
    apiGetSettings()
      .then(r => safeJson(r))
      .then(d => {
        setProvider(d.call_provider || 'twilio')
        setMode(d.interview_mode || 'legacy')
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  const handleSave = async (patch, successText) => {
    setSaving(true)
    setMessage(null)
    try {
      const res  = await apiUpdateSettings(patch)
      const data = await safeJson(res)
      if (res.ok) {
        setProvider(data.call_provider)
        setMode(data.interview_mode)
        setMessage({ type: 'success', text: successText(data) })
      } else {
        setMessage({ type: 'error', text: data.detail || 'Failed to update settings.' })
      }
    } catch {
      setMessage({ type: 'error', text: 'Network error — could not save.' })
    }
    setSaving(false)
  }

  const radioBtn = (selected, onClick, title, desc, key) => (
    <button
      key={key}
      onClick={onClick}
      disabled={saving}
      style={{
        display: 'flex', alignItems: 'center', gap: 14,
        padding: '14px 18px', borderRadius: 10, cursor: saving ? 'wait' : 'pointer',
        border: selected ? '2px solid var(--primary)' : '2px solid var(--border)',
        background: selected ? 'var(--primary-subtle, #eff6ff)' : 'var(--bg-card)',
        textAlign: 'left', transition: 'all 0.15s',
      }}
    >
      <div style={{
        width: 18, height: 18, borderRadius: '50%', flexShrink: 0,
        border: selected ? '5px solid var(--primary)' : '2px solid var(--text-3)',
        background: selected ? 'white' : 'transparent',
      }} />
      <div>
        <div style={{ fontWeight: 600, fontSize: '0.95rem', color: 'var(--text)' }}>{title}</div>
        <div style={{ fontSize: '0.8rem', color: 'var(--text-2)', marginTop: 2 }}>{desc}</div>
      </div>
      {selected && (
        <span style={{ marginLeft: 'auto', fontSize: '0.75rem', fontWeight: 600, color: 'var(--primary)', background: 'var(--primary-subtle, #eff6ff)', padding: '3px 10px', borderRadius: 20 }}>
          Active
        </span>
      )}
    </button>
  )

  if (loading) return (
    <div style={{ padding: '40px 0', textAlign: 'center', color: 'var(--text-2)' }}>
      <div className="spinner" style={{ margin: '0 auto 12px' }} />
      Loading settings…
    </div>
  )

  return (
    <div style={{ maxWidth: 520 }}>
      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ fontWeight: 700, fontSize: '1rem', marginBottom: 6 }}>Call Provider</div>
        <div style={{ fontSize: '0.85rem', color: 'var(--text-2)', marginBottom: 20 }}>
          Choose the telephony provider for outbound screening calls. Changes take effect immediately — no restart needed.
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {['twilio', 'plivo'].map(p => radioBtn(
            provider === p,
            () => provider !== p && handleSave(
              { call_provider: p },
              d => `Switched to ${d.call_provider === 'twilio' ? 'Twilio' : 'Plivo'} successfully.`,
            ),
            p === 'twilio' ? 'Twilio' : 'Plivo',
            p === 'twilio'
              ? 'Default provider — requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER'
              : 'Lower cost for India — requires PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_PHONE_NUMBER',
            p,
          ))}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ fontWeight: 700, fontSize: '1rem', marginBottom: 6 }}>Interview Mode</div>
        <div style={{ fontSize: '0.85rem', color: 'var(--text-2)', marginBottom: 20 }}>
          How candidate answers are captured during phone screenings. Applies to both providers; affects new calls only.
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {['legacy', 'streaming'].map(m => radioBtn(
            mode === m,
            () => mode !== m && handleSave(
              { interview_mode: m },
              d => `Interview mode set to ${MODE_INFO[d.interview_mode]?.label || d.interview_mode}.`,
            ),
            MODE_INFO[m].label,
            MODE_INFO[m].desc,
            m,
          ))}
        </div>

        {message && (
          <div style={{
            marginTop: 16, padding: '10px 14px', borderRadius: 8, fontSize: '0.85rem',
            background: message.type === 'success' ? '#f0fdf4' : '#fef2f2',
            color:      message.type === 'success' ? '#166534' : '#991b1b',
            border:     `1px solid ${message.type === 'success' ? '#bbf7d0' : '#fecaca'}`,
          }}>
            {message.text}
          </div>
        )}
      </div>

      <div className="card" style={{ fontSize: '0.82rem', color: 'var(--text-2)', lineHeight: 1.7 }}>
        <div style={{ fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>Env vars required per provider</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px' }}>
          <div>
            <div style={{ fontWeight: 600, color: 'var(--text-2)', marginBottom: 4 }}>Twilio</div>
            <code style={{ display: 'block' }}>TWILIO_ACCOUNT_SID</code>
            <code style={{ display: 'block' }}>TWILIO_AUTH_TOKEN</code>
            <code style={{ display: 'block' }}>TWILIO_PHONE_NUMBER</code>
          </div>
          <div>
            <div style={{ fontWeight: 600, color: 'var(--text-2)', marginBottom: 4 }}>Plivo</div>
            <code style={{ display: 'block' }}>PLIVO_AUTH_ID</code>
            <code style={{ display: 'block' }}>PLIVO_AUTH_TOKEN</code>
            <code style={{ display: 'block' }}>PLIVO_PHONE_NUMBER</code>
          </div>
        </div>
        <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div style={{ fontWeight: 600, color: 'var(--text-2)', marginBottom: 4 }}>Streaming mode</div>
          <code style={{ display: 'block' }}>DEEPGRAM_API_KEY</code>
          <code style={{ display: 'block' }}>AZURE_SPEECH_KEY</code>
          <code style={{ display: 'block' }}>AZURE_SPEECH_REGION</code>
        </div>
        <div style={{ marginTop: 12 }}>
          Defaults on server start: <code>CALL_PROVIDER=plivo</code>, <code>INTERVIEW_MODE=streaming</code> in .env.
        </div>
      </div>
    </div>
  )
}
