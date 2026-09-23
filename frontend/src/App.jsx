import { useState, useRef, useEffect } from 'react'
import {
  Briefcase, ChartBar, CheckCircle, Microphone,
  Trophy, PencilSimple, X, Plus, WarningCircle,
  Users, ArrowRight, UserPlus, Stack,
} from '@phosphor-icons/react'
import Sidebar           from './components/Sidebar'
import InputSection      from './components/InputSection'
import ResultsDashboard  from './components/ResultsDashboard'
import BatchProgress     from './components/BatchProgress'
import BatchResultsTable    from './components/BatchResultsTable'
import CallbackAlertModal  from './components/CallbackAlertModal'
import LoginPage         from './components/LoginPage'
import UserManagement   from './components/UserManagement'
import JdBuilder        from './components/JdBuilder'
import Settings         from './components/Settings'

import { apiStartPipeline, apiPipelineStatus, apiStopPipeline, apiActiveCalls, safeJson } from './api/client'
import { useAppContext }  from './context/AppContext'
import { useAnalyze }    from './hooks/useAnalyze'
import { useInterview }  from './hooks/useInterview'
import { useBatch }      from './hooks/useBatch'

function JdCopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try { await navigator.clipboard.writeText(text) }
    catch {
      const el = document.createElement('textarea')
      el.value = text; document.body.appendChild(el); el.select()
      document.execCommand('copy'); document.body.removeChild(el)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <button className="btn-clear" onClick={handleCopy}
      style={{ flex: '0 0 auto', padding: '9px 18px' }}>
      {copied ? '✓ Copied!' : 'Copy JD'}
    </button>
  )
}

function OpeningContextBar({ opening, openings, onLink, onUnlink, showEmpty = true }) {
  if (opening) return (
    <div className="opening-context-bar">
      <span className="opening-context-label">📁 {opening.title}</span>
      <button className="opening-context-clear" onClick={onUnlink} title="Unlink opening">✕</button>
    </div>
  )
  if (showEmpty && openings.length > 0) return (
    <div className="opening-context-bar opening-context-bar--empty">
      <span style={{ color: 'var(--text-3)', fontSize: '0.8rem' }}>No opening selected — </span>
      <select className="opening-context-select" value="" onChange={e => onLink(e.target.value)}>
        <option value="" disabled>select a job opening to pre-fill JD</option>
        {openings.map(o => <option key={o.id} value={o.id}>{o.title}</option>)}
      </select>
    </div>
  )
  return null
}

const PAGE_TITLES = {
  dashboard:         'Job Openings',
  single:            'Single Candidate',
  batch:             'Batch Pipeline',
  'active-calls':    'Active Calls',
  callbacks:         'Scheduled Callbacks',
  rankings:          'Rankings',
  'change-password': 'Change Password',
  'add-user':        'Add User',
  'user-list':       'User List',
  'jd-builder':      'JD Builder',
  'settings':        'Settings',
}

export default function App() {
  const [authUser, setAuthUser] = useState(() => {
    try {
      const token = sessionStorage.getItem('auth_token')
      const user  = sessionStorage.getItem('auth_user')
      if (token && user) return JSON.parse(user)
    } catch {}
    return null
  })

  // Validate token with backend on load
  useEffect(() => {
    const token = sessionStorage.getItem('auth_token')
    if (!token) { setAuthUser(null); return }
    fetch('/auth/me', { headers: { 'Authorization': `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(u => {
        setAuthUser(u)
        if (u.must_change_password) setActivePage('change-password')
      })
      .catch(() => { sessionStorage.removeItem('auth_token'); sessionStorage.removeItem('auth_user'); setAuthUser(null) })
  }, [])

  const handleLogout = async () => {
    const token = sessionStorage.getItem('auth_token')
    if (token) await fetch('/auth/logout', { method: 'POST', headers: { 'Authorization': `Bearer ${token}` } }).catch(() => {})
    sessionStorage.removeItem('auth_token')
    sessionStorage.removeItem('auth_user')
    setAuthUser(null)
  }

  const {
    allTime, addAllTime,
    openings, setOpenings, saveOpenings,
    activeOpeningId, setActiveOpening, activeOpening, defaultJd,
    showOpeningForm, setShowOpeningForm,
    newOpeningTitle, setNewOpeningTitle,
    newOpeningJd, setNewOpeningJd,
    editingOpeningId, setEditingOpeningId,
    editingJd, setEditingJd,
    viewingOpeningId, setViewingOpeningId,
    duplicateModal, setDuplicateModal,
    dueCallbacks, dismissedCallbacks, setDismissedCallbacks,
    createOpening, deleteOpening, updateOpeningJd,
    addSingleToOpening, updateSingleInterviewInOpening,
    saveOpeningBatch, syncBatchToOpenings,
    findDuplicateInOpening, candidateStatusLabel,
    syncOpenings,
  } = useAppContext()

  // Kick an immediate sync right after login — the context's own polling loop only
  // resumes on its next tick (up to 10s later) once a token exists, which would
  // otherwise leave the dashboard looking empty/stale for a moment post-login.
  useEffect(() => {
    if (authUser) syncOpenings()
  }, [authUser, syncOpenings])

  // ── navigation ──
  const [activePage, setActivePage] = useState('dashboard')
  const [addCandidatesOpeningId, setAddCandidatesOpeningId] = useState(null)
  const [viewingJdId, setViewingJdId] = useState(null)

  const resultsRef       = useRef(null)
  const currentSingleIdRef = useRef(null)
  const prevResultRef    = useRef(null)
  const prevIvStatusRef  = useRef(null)

  // ── analyze hook ──
  const {
    result, setResult, singleId, loading, error,
    resumeText, jdText,
    handleAnalyze, clearAnalyze,
  } = useAnalyze({
    openingId: activeOpeningId,
    onAutoSaveJd: (jText) => {
      if (activeOpeningId && jText.trim()) {
        const op = openings.find(o => o.id === activeOpeningId)
        if (op && !op.jd) updateOpeningJd(activeOpeningId, jText.trim())
      }
    },
  })

  // ── interview hook ──
  const {
    interview, setInterview, callLoading, callError,
    handleStartInterview, clearInterview,
  } = useInterview()

  // ── batch hook ──
  const {
    batchFiles, setBatchFiles,
    batchId, batchData,
    batchLoading, batchError,
    handleBatchStart, handleBatchReset,
    handleCallCandidate: _handleCallCandidate,
    startBatchPolling,
  } = useBatch({
    onSyncToOpenings: syncBatchToOpenings,
    onAutoSaveJd: (jd) => {
      if (activeOpeningId && jd.trim()) {
        const op = openings.find(o => o.id === activeOpeningId)
        if (op && !op.jd) updateOpeningJd(activeOpeningId, jd.trim())
      }
    },
    getActiveOpeningTitle: () => openings.find(o => o.id === activeOpeningId)?.title || '',
    getActiveOpeningId:    () => activeOpeningId || '',
  })

  const handleCallCandidate = (candidate) =>
    _handleCallCandidate(candidate, batchId, viewingOpeningId, setOpenings, saveOpenings)

  // ── combined clear ──
  const handleClearAll = () => {
    clearAnalyze()
    clearInterview()
  }

  // `interview` is one flat piece of state shared across every single-candidate analysis in
  // this session, independent of which candidate is on screen. Analyzing a résumé already
  // resets the score/result for the new candidate, but nothing cleared the PREVIOUS
  // candidate's leftover interview status — so if any earlier test call on this opening (or
  // any other) ended up abandoned/declined/failed, that banner kept showing under every new
  // candidate analyzed afterward, even though no call had been placed for them at all.
  const handleAnalyzeFresh = (rText, jText) => {
    clearInterview()
    return handleAnalyze(rText, jText)
  }

  // ── pipeline state ──
  const [pipelineStatuses, setPipelineStatuses] = useState({}) // {opening_id: status_obj}
  const pipelinePolls = useRef({}) // {opening_id: intervalId}

  // ── global active calls state ──
  const [allActiveCalls, setAllActiveCalls] = useState([])
  const activeCallsPollRef = useRef(null)

  const _pollPipeline = async (openingId) => {
    try {
      const res  = await apiPipelineStatus(openingId)
      const data = await safeJson(res)
      if (data.status === 'none') {
        clearInterval(pipelinePolls.current[openingId])
        delete pipelinePolls.current[openingId]
        setPipelineStatuses(prev => { const n = {...prev}; delete n[openingId]; return n })
        syncOpenings()
        return
      }
      setPipelineStatuses(prev => ({ ...prev, [openingId]: data }))
      if (data.status !== 'running') {
        clearInterval(pipelinePolls.current[openingId])
        delete pipelinePolls.current[openingId]
      }
      syncOpenings() // keep candidate statuses fresh while pipeline is active
    } catch {}
  }

  const handleStartPipeline = async (openingId) => {
    try {
      const res  = await apiStartPipeline(openingId)
      const data = await safeJson(res)
      if (!res.ok) { alert(data.detail || 'Failed to start pipeline'); return }
      setPipelineStatuses(prev => ({ ...prev, [openingId]: data }))
      pipelinePolls.current[openingId] = setInterval(() => _pollPipeline(openingId), 5000)
    } catch (e) { alert('Failed to start pipeline') }
  }

  const handleStopPipeline = async (openingId) => {
    try {
      await apiStopPipeline(openingId)
      setPipelineStatuses(prev => ({ ...prev, [openingId]: { ...prev[openingId], status: 'stopped' } }))
      clearInterval(pipelinePolls.current[openingId])
      delete pipelinePolls.current[openingId]
    } catch {}
  }

  // Poll any running pipeline statuses on mount (e.g. after page refresh)
  useEffect(() => {
    openings.forEach(op => {
      if (!pipelinePolls.current[op.id]) {
        apiPipelineStatus(op.id).then(r => safeJson(r)).then(data => {
          if (data.status === 'running') {
            setPipelineStatuses(prev => ({ ...prev, [op.id]: data }))
            pipelinePolls.current[op.id] = setInterval(() => _pollPipeline(op.id), 5000)
          }
        }).catch(() => {})
      }
    })
    return () => {
      Object.values(pipelinePolls.current).forEach(clearInterval)
      pipelinePolls.current = {}
    }
  }, [openings.length]) // eslint-disable-line

  // ── When user opens Rankings for a specific opening, immediately check pipeline status ──
  useEffect(() => {
    if (activePage !== 'rankings' || !viewingOpeningId) return
    if (pipelinePolls.current[viewingOpeningId]) return // already polling
    apiPipelineStatus(viewingOpeningId).then(r => safeJson(r)).then(data => {
      if (data.status === 'none') return
      setPipelineStatuses(prev => ({ ...prev, [viewingOpeningId]: data }))
      if (data.status === 'running' && !pipelinePolls.current[viewingOpeningId]) {
        pipelinePolls.current[viewingOpeningId] = setInterval(() => _pollPipeline(viewingOpeningId), 5000)
      }
    }).catch(() => {})
  }, [viewingOpeningId, activePage]) // eslint-disable-line

  // ── Poll all active + callback calls globally (always running) ──
  useEffect(() => {
    const ms = activePage === 'active-calls' ? 3000 : 6000
    const poll = () =>
      apiActiveCalls().then(r => safeJson(r)).then(d => setAllActiveCalls(d.calls || [])).catch(() => {})
    poll()
    activeCallsPollRef.current = setInterval(poll, ms)
    return () => {
      clearInterval(activeCallsPollRef.current)
      activeCallsPollRef.current = null
    }
  }, [activePage]) // eslint-disable-line

  // ── Heal stale single interview state ──
  // If interview shows active but DB says it's done, re-fetch the real status
  useEffect(() => {
    const iid = interview?.interview_id
    if (!iid) return
    if (!['calling', 'in_progress', 'processing'].includes(interview?.status)) return
    if (!allActiveCalls.length) return
    const stillActive = allActiveCalls.find(c => c.interview_id === iid)
    if (!stillActive) {
      fetch(`/interview/status/${iid}`)
        .then(r => r.json())
        .then(d => { if (d?.status && d.status !== interview.status) setInterview(d) })
        .catch(() => {})
    }
  }, [allActiveCalls]) // eslint-disable-line

  // Track single-candidate analysis → openings
  useEffect(() => {
    if (result && result !== prevResultRef.current) {
      const score    = parseInt(result.match_score) || 0
      const delta    = { total: 1, qualified: score >= 70 ? 1 : 0 }
      const sid      = singleId || Date.now().toString()
      const entry    = {
        _singleId:        sid,
        _batchId:         null,
        _type:            'single',
        name:             result.name   || null,
        email:            result.email  || null,
        phone:            result.phone  || null,
        file_name:        result.name   || 'Single Candidate',
        resume_score:     score,
        filter_status:    score >= 70 ? 'qualified' : 'filtered_out',
        interview_status: 'pending',
        interview_score:  null,
        combined_score:   null,
        score_result:     null,
        transcript:       null,
        questions:        [],
        analyze_result:   result,
      }

      if (activeOpeningId) {
        const opening  = openings.find(o => o.id === activeOpeningId)
        const existing = findDuplicateInOpening(opening, result.name, result.email)
        if (existing) {
          setDuplicateModal({
            existing,
            onAddAnyway: () => {
              currentSingleIdRef.current = sid
              addSingleToOpening(activeOpeningId, entry, delta)
              addAllTime(delta)
              setDuplicateModal(null)
            },
            onCancel: () => setDuplicateModal(null),
          })
        } else {
          currentSingleIdRef.current = sid
          addSingleToOpening(activeOpeningId, entry, delta)
          addAllTime(delta)
        }
      } else {
        addAllTime(delta)
      }
      setTimeout(() => resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
    }
    prevResultRef.current = result
  }, [result])

  // Track single interview status → openings
  useEffect(() => {
    const s   = interview?.status
    const prev = prevIvStatusRef.current
    if (s && s !== prev) {
      if (activeOpeningId && currentSingleIdRef.current) {
        updateSingleInterviewInOpening(activeOpeningId, currentSingleIdRef.current, interview)
      }
      if (s === 'completed' && prev !== 'completed') addAllTime({ done: 1 })
    }
    prevIvStatusRef.current = s
  }, [interview?.status])

  // Track batch completion → openings + allTime
  useEffect(() => {
    if (batchData?.status === 'completed' && batchId && !allTime.batches?.includes(batchId)) {
      const bTotal     = batchData.total || 0
      const bQualified = batchData.candidates?.filter(c => c.filter_status === 'qualified').length || 0
      const bDone      = batchData.candidates?.filter(c =>
        c.interview_status === 'completed'
      ).length || 0
      addAllTime({ total: bTotal, qualified: bQualified, done: bDone, batchId })
      if (activeOpeningId) {
        const opening = openings.find(o => o.id === activeOpeningId)
        if (opening && !opening.batchIds?.includes(batchId)) {
          const cands = batchData.candidates.map(c => {
            const dup = findDuplicateInOpening(opening, c.name, c.email)
            return dup ? { ...c, _duplicate_of: dup._singleId || dup._batchId || 'existing' } : c
          })
          saveOpeningBatch(activeOpeningId, batchId, cands,
            { total: bTotal, qualified: bQualified, done: bDone })
        }
      }
    }
  }, [batchData?.status, batchId])

  // Auto-switch to batch page when files added on single page
  useEffect(() => {
    if (batchFiles.length > 0 && activePage === 'single') setActivePage('batch')
  }, [batchFiles.length])

  // ── navigation handler ──
  const handleNavigate = (page) => {
    setActivePage(page)
    if (page === 'single') setBatchFiles([])
  }

  const handleStartInterviewWrapped = () => {
    if (!result?.phone) return
    handleStartInterview({
      phone:          result.phone,
      resume_text:    resumeText,
      jd_text:        jdText,
      candidate_name: result.name,
      job_title:      openings.find(o => o.id === activeOpeningId)?.title || '',
      opening_id:     activeOpeningId || null,
      single_id:      currentSingleIdRef.current || null,
    })
  }

  // ── derived values ──
  const singleActive = (interview && ['calling', 'in_progress', 'processing'].includes(interview.status))
    ? [{
        interview_id:     interview.interview_id,
        name:             interview.candidate_name,
        phone:            interview.phone,
        interview_status: interview.status,
        processing_step:  interview.processing_step,
        file_name:        interview.candidate_name || 'Single Candidate',
        filter_status:    'qualified',
        resume_score:     result ? (parseInt(result.match_score) || null) : null,
        score_result:     interview.score_result,
      }]
    : []

  const activeCandidates = (() => {
    const base = allActiveCalls.length > 0
      ? allActiveCalls.filter(c => ['calling', 'in_progress', 'processing'].includes(c.interview_status))
      : (batchData?.candidates?.filter(c =>
          ['calling', 'in_progress', 'processing'].includes(c.interview_status)
        ) || [])
    const deduped = singleActive.filter(s => !base.find(a => a.interview_id === s.interview_id))
    return [...base, ...deduped]
  })()

  const callbackCandidates = (() => {
    const fromPoll  = allActiveCalls.filter(c => c.interview_status === 'callback_scheduled')
    const fromBatch = (batchData?.candidates || []).filter(c => c.interview_status === 'callback_scheduled')
    const fromSingle = interview?.status === 'callback_scheduled'
      ? [{
          interview_id:          interview.interview_id,
          name:                  interview.candidate_name,
          phone:                 interview.phone,
          interview_status:      'callback_scheduled',
          callback_scheduled_at: interview.callback_scheduled_at,
          file_name:             interview.candidate_name || 'Single Candidate',
          filter_status:         'qualified',
          resume_score:          result ? (parseInt(result.match_score) || null) : null,
        }]
      : []
    const seen = new Set()
    return [...fromPoll, ...fromBatch, ...fromSingle].filter(c => {
      const key = c.interview_id || c.file_name
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  })()
  const visibleDueCallbacks = dueCallbacks.filter(cb => {
    const snoozedAt = dismissedCallbacks[cb.interview_id]
    if (!snoozedAt) return true
    return Date.now() - snoozedAt > 10 * 60 * 1000
  })

  const nextInQueue = Object.values(pipelineStatuses)
    .filter(ps => ps.status === 'running')
    .flatMap(ps => ps.queue || [])

  const canEdit = authUser?.role !== 'user'

  if (!authUser) return <LoginPage onLogin={setAuthUser} />

  return (
    <div className="app-shell">
      <Sidebar
        activePage={activePage}
        onNavigate={handleNavigate}
        batchData={batchData}
        batchId={batchId}
        userRole={authUser?.role}
        activeCallsCount={activeCandidates.length + nextInQueue.length}
        callbackCount={callbackCandidates.length}
      />

      <div className="app-body">
        <div className="app-topbar">
          <h1 className="topbar-title">{PAGE_TITLES[activePage]}</h1>
          <div className="topbar-actions">
            <span className="topbar-online-badge">● System Online</span>
            <div className="topbar-user">
              <span className="topbar-user-name">
                {authUser.full_name || authUser.username.charAt(0).toUpperCase() + authUser.username.slice(1)}
              </span>
              <span className={`topbar-role-badge topbar-role-badge--${authUser.role}`}>
                {authUser.role === 'super_admin' ? 'Director' : authUser.role === 'admin' ? 'Admin' : 'View Only'}
              </span>
            </div>
            <button className="btn-clear topbar-signout" onClick={handleLogout}>
              Sign out
            </button>
          </div>
        </div>

        <main className="app-main">

          {/* ── Dashboard ── */}
          {activePage === 'dashboard' && (
            <div>
              {(activeCandidates.length > 0 || callbackCandidates.length > 0 || nextInQueue.length > 0) && (
                <div className="live-panel">
                  <div className="live-panel-header">
                    <span className="live-pulse-dot" />
                    Live Pipeline
                  </div>
                  <div className="live-panel-body">
                    {activeCandidates.length > 0 && (
                      <div className="live-section" onClick={() => handleNavigate('active-calls')}>
                        <div className="live-section-label">📞 On Call ({activeCandidates.length})</div>
                        {activeCandidates.map(c => (
                          <div className="live-cand" key={c.interview_id || c.file_name}>
                            <div className="live-avatar live-avatar--active">
                              {(c.name || c.file_name || '?')[0].toUpperCase()}
                            </div>
                            <div className="live-cand-info">
                              <div className="live-cand-name">{c.name || c.file_name || 'Candidate'}</div>
                              <div className="live-cand-status live-cand-status--active">
                                {c.interview_status === 'processing'
                                  ? `⚙ ${c.processing_step || 'Processing'}`
                                  : c.interview_status === 'in_progress' ? '🎤 In interview'
                                  : '📞 Connecting'}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                    {nextInQueue.length > 0 && (
                      <div className="live-section">
                        <div className="live-section-label">⏳ Up Next ({nextInQueue.length})</div>
                        {nextInQueue.slice(0, 5).map((c, i) => (
                          <div className="live-cand" key={i}>
                            <div className="live-avatar live-avatar--queue">
                              {(c.name || c.file_name || '?')[0].toUpperCase()}
                            </div>
                            <div className="live-cand-info">
                              <div className="live-cand-name">{c.name || c.file_name || 'Candidate'}</div>
                              <div className="live-cand-status live-cand-status--queue">Queued</div>
                            </div>
                          </div>
                        ))}
                        {nextInQueue.length > 5 && (
                          <div className="live-queue-more">+{nextInQueue.length - 5} more waiting</div>
                        )}
                      </div>
                    )}
                    {callbackCandidates.length > 0 && (
                      <div className="live-section" onClick={() => handleNavigate('callbacks')}>
                        <div className="live-section-label">📅 Callbacks ({callbackCandidates.length})</div>
                        {callbackCandidates.map(c => (
                          <div className="live-cand" key={c.interview_id || c.file_name}>
                            <div className="live-avatar live-avatar--callback">
                              {(c.name || c.file_name || '?')[0].toUpperCase()}
                            </div>
                            <div className="live-cand-info">
                              <div className="live-cand-name">{c.name || c.file_name || 'Candidate'}</div>
                              <div className="live-cand-status live-cand-status--callback">
                                {c.callback_scheduled_at
                                  ? new Date(c.callback_scheduled_at).toLocaleString('en-IN',
                                      { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
                                  : 'Scheduled'}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}
              <div className="opening-grid">
                {openings.map(op => {
                  const cands     = op.candidates || []
                  const total     = cands.filter(c => !c._duplicate_of).length
                  const qualified = cands.filter(c => !c._duplicate_of && c.filter_status === 'qualified').length
                  const done      = cands.filter(c => !c._duplicate_of && c.interview_status === 'completed').length
                  const hasResults = op.stats.total > 0
                  return (
                    <div key={op.id} className={`opening-card${hasResults ? ' opening-card--has-results' : ''}`}>
                      <div className="opening-card-header">
                        <div className="opening-card-title-row">
                          <div className="opening-card-icon-wrap">
                            <Briefcase size={18} weight="duotone" />
                          </div>
                          <span className="opening-card-title">{op.title}</span>
                        </div>
                        {canEdit && (
                          <div className="opening-card-actions-top">
                            <button className="opening-icon-btn opening-icon-btn--edit"
                              onClick={() => { setEditingOpeningId(op.id); setEditingJd(op.jd || '') }}
                              title="Edit JD">
                              <PencilSimple size={14} weight="bold" />
                            </button>
                            <button className="opening-icon-btn opening-icon-btn--delete"
                              onClick={() => { if (window.confirm(`Delete "${op.title}"? This cannot be undone.`)) deleteOpening(op.id) }}
                              title="Delete opening">
                              <X size={14} weight="bold" />
                            </button>
                          </div>
                        )}
                      </div>

                      {editingOpeningId === op.id ? (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                          <textarea
                            className="opening-form-jd"
                            placeholder="Paste the job description here…"
                            value={editingJd}
                            onChange={e => setEditingJd(e.target.value)}
                            autoFocus
                          />
                          <div style={{ display: 'flex', gap: 8 }}>
                            <button className="btn-analyze" style={{ flex: 1, fontSize: '0.8rem', padding: '7px 0' }}
                              onClick={() => updateOpeningJd(op.id, editingJd.trim())}>
                              Save JD
                            </button>
                            <button className="btn-clear" style={{ flex: 1, fontSize: '0.8rem', padding: '7px 0' }}
                              onClick={() => { setEditingOpeningId(null); setEditingJd('') }}>
                              Cancel
                            </button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="opening-card-stats">
                            <div className="opening-stat">
                              <div className="opening-stat-icon opening-stat-icon--blue">
                                <ChartBar size={16} weight="duotone" />
                              </div>
                              <div className="opening-stat-value">{total}</div>
                              <div className="opening-stat-label">Analyzed</div>
                            </div>
                            <div className="opening-stat-divider" />
                            <div className="opening-stat">
                              <div className="opening-stat-icon opening-stat-icon--green">
                                <CheckCircle size={16} weight="duotone" />
                              </div>
                              <div className="opening-stat-value opening-stat-value--green">{qualified}</div>
                              <div className="opening-stat-label">Qualified</div>
                            </div>
                            <div className="opening-stat-divider" />
                            <div className="opening-stat">
                              <div className="opening-stat-icon opening-stat-icon--violet">
                                <Microphone size={16} weight="duotone" />
                              </div>
                              <div className="opening-stat-value opening-stat-value--violet">{done}</div>
                              <div className="opening-stat-label">Interviewed</div>
                            </div>
                          </div>

                          {!op.jd && (
                            <div className="opening-no-jd-warn">
                              <WarningCircle size={13} weight="fill" />
                              No JD saved — click edit to add one
                            </div>
                          )}

                          <div className="opening-card-footer">
                            {hasResults ? (
                              <button className="opening-btn opening-btn--results"
                                onClick={() => { setViewingOpeningId(op.id); handleNavigate('rankings') }}>
                                <Trophy size={16} weight="duotone" />
                                View Results &amp; Rankings
                                <ArrowRight size={14} weight="bold" className="opening-btn-arrow" />
                              </button>
                            ) : (
                              <button className="opening-btn opening-btn--add"
                                onClick={() => setAddCandidatesOpeningId(op.id)}>
                                <UserPlus size={15} weight="duotone" />
                                Add Candidates
                              </button>
                            )}
                            {op.jd && (
                              <button className="opening-btn opening-btn--jd"
                                onClick={() => setViewingJdId(op.id)}
                                style={{ marginTop: 8 }}>
                                <Stack size={15} weight="duotone" />
                                View JD
                              </button>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  )
                })}

                {canEdit && !showOpeningForm && (
                  <button className="opening-new-card" onClick={() => setShowOpeningForm(true)}>
                    <div className="opening-new-icon-wrap">
                      <Plus size={28} weight="light" />
                    </div>
                    <div className="opening-new-label">New Job Opening</div>
                  </button>
                )}
              </div>

              {/* ── JD Viewer Modal ── */}
              {viewingJdId && (() => {
                const op = openings.find(o => o.id === viewingJdId)
                if (!op) return null
                return (
                  <div className="modal-overlay" onClick={() => setViewingJdId(null)}>
                    <div className="modal-box" style={{ maxWidth: 680, width: '90vw' }}
                         onClick={e => e.stopPropagation()}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16 }}>
                        <div>
                          <div style={{ fontWeight: 700, fontSize: '1rem', color: 'var(--text)' }}>{op.title}</div>
                          <div style={{ fontSize: '0.8rem', color: 'var(--text-3)', marginTop: 3 }}>Job Description</div>
                        </div>
                        <button className="opening-icon-btn" onClick={() => setViewingJdId(null)} title="Close">
                          <X size={16} weight="bold" />
                        </button>
                      </div>
                      <pre style={{
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                        background: 'var(--bg)', border: '1px solid var(--border)',
                        borderRadius: 8, padding: '14px 16px',
                        fontSize: '0.83rem', color: 'var(--text-2)', lineHeight: 1.65,
                        maxHeight: 420, overflowY: 'auto', margin: 0,
                      }}>
                        {op.jd}
                      </pre>
                      <div style={{ display: 'flex', gap: 10, marginTop: 16 }}>
                        <JdCopyButton text={op.jd} />
                        {canEdit && (
                          <button className="btn-clear" style={{ flex: '0 0 auto', padding: '9px 18px' }}
                            onClick={() => { setViewingJdId(null); setEditingOpeningId(op.id); setEditingJd(op.jd || '') }}>
                            Edit JD
                          </button>
                        )}
                        <button className="btn-analyze" style={{ flex: 1 }}
                          onClick={() => setViewingJdId(null)}>
                          Close
                        </button>
                      </div>
                    </div>
                  </div>
                )
              })()}

              {showOpeningForm && (
                <div className="opening-create-form">
                  <div className="opening-form-title">New Job Opening</div>
                  <input
                    className="opening-form-input"
                    placeholder="Job title (e.g. AI Engineer)"
                    value={newOpeningTitle}
                    onChange={e => setNewOpeningTitle(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && createOpening()}
                    autoFocus
                  />
                  <textarea
                    className="opening-form-jd"
                    placeholder="Paste the job description here…"
                    value={newOpeningJd}
                    onChange={e => setNewOpeningJd(e.target.value)}
                  />
                  <div className="opening-form-actions">
                    <button className="btn-analyze" onClick={createOpening}
                      disabled={!newOpeningTitle.trim()}>
                      Create Opening
                    </button>
                    <button className="btn-clear" onClick={() => {
                      setShowOpeningForm(false); setNewOpeningTitle(''); setNewOpeningJd('')
                    }}>
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {openings.length === 0 && !showOpeningForm && (
                <div className="db-empty-state" style={{ marginTop: 24 }}>
                  <div className="db-empty-icon">💼</div>
                  <div className="db-empty-title">No job openings yet</div>
                  <div className="db-empty-desc">
                    Create a job opening to organise candidates by role and pre-fill the JD automatically.
                  </div>
                </div>
              )}

              {addCandidatesOpeningId && (() => {
                const targetOp = openings.find(o => o.id === addCandidatesOpeningId)
                return (
                  <div className="add-cand-overlay" onClick={() => setAddCandidatesOpeningId(null)}>
                    <div className="add-cand-modal" onClick={e => e.stopPropagation()}>
                      <div className="add-cand-header">
                        <div className="add-cand-title">
                          <Briefcase size={16} weight="duotone" />
                          Add Candidates
                        </div>
                        <span className="add-cand-job">{targetOp?.title}</span>
                        <button className="add-cand-close" onClick={() => setAddCandidatesOpeningId(null)}>
                          <X size={16} weight="bold" />
                        </button>
                      </div>
                      <div className="add-cand-body">
                        <button className="add-cand-option" onClick={() => {
                          setActiveOpening(addCandidatesOpeningId)
                          setAddCandidatesOpeningId(null)
                          handleNavigate('single')
                        }}>
                          <div className="add-cand-option-icon add-cand-option-icon--single">
                            <UserPlus size={28} weight="duotone" />
                          </div>
                          <div className="add-cand-option-text">
                            <div className="add-cand-option-title">Single Candidate</div>
                            <div className="add-cand-option-desc">Upload one resume and get an instant AI analysis</div>
                          </div>
                          <ArrowRight size={16} weight="bold" className="add-cand-option-arrow" />
                        </button>
                        <button className="add-cand-option" onClick={() => {
                          setActiveOpening(addCandidatesOpeningId)
                          setAddCandidatesOpeningId(null)
                          handleNavigate('batch')
                        }}>
                          <div className="add-cand-option-icon add-cand-option-icon--batch">
                            <Stack size={28} weight="duotone" />
                          </div>
                          <div className="add-cand-option-text">
                            <div className="add-cand-option-title">Batch Pipeline</div>
                            <div className="add-cand-option-desc">Upload multiple resumes and run them in parallel</div>
                          </div>
                          <ArrowRight size={16} weight="bold" className="add-cand-option-arrow" />
                        </button>
                      </div>
                    </div>
                  </div>
                )
              })()}
            </div>
          )}

          {/* ── Single Candidate ── */}
          {activePage === 'single' && (
            <div>
              <OpeningContextBar
                opening={activeOpening} openings={openings}
                onLink={setActiveOpening} onUnlink={() => setActiveOpening(null)}
              />
              <InputSection
                key={activeOpeningId || 'single-no-opening'}
                mode="single"
                defaultJd={defaultJd}
                onAnalyze={handleAnalyzeFresh}
                onClear={handleClearAll}
                loading={loading}
                error={error}
                readOnly={!canEdit}
                batchFiles={[]}
                onBatchFilesChange={setBatchFiles}
                onBatchStart={handleBatchStart}
                batchLoading={batchLoading}
                batchError={batchError}
              />
              <div ref={resultsRef}>
                {result && (
                  <ResultsDashboard
                    data={result}
                    interview={interview}
                    callLoading={callLoading}
                    callError={callError}
                    onStartInterview={canEdit ? handleStartInterviewWrapped : null}
                  />
                )}
              </div>
            </div>
          )}

          {/* ── Batch Pipeline ── */}
          {activePage === 'batch' && (
            <div>
              <OpeningContextBar
                opening={activeOpening} openings={openings}
                onLink={setActiveOpening} onUnlink={() => setActiveOpening(null)}
                showEmpty={!batchId}
              />
              {!batchId && (
                <InputSection
                  key={activeOpeningId || 'batch-no-opening'}
                  mode="batch"
                  defaultJd={defaultJd}
                  onAnalyze={handleAnalyzeFresh}
                  onClear={handleClearAll}
                  loading={loading}
                  error={error}
                  readOnly={!canEdit}
                  batchFiles={batchFiles}
                  onBatchFilesChange={setBatchFiles}
                  onBatchStart={handleBatchStart}
                  batchLoading={batchLoading}
                  batchError={batchError}
                />
              )}
              {batchId && !batchData && (
                <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--text-2)' }}>
                  <div className="spinner" style={{ margin: '0 auto 14px' }} />
                  <p style={{ fontSize: '0.9rem' }}>Starting batch pipeline…</p>
                </div>
              )}
              {batchData?.status === 'processing' && <BatchProgress batchData={batchData} />}
              {batchData?.candidates?.length > 0 && (() => {
                const batchOpening =
                  (batchData?.opening_id ? openings.find(o => o.id === batchData.opening_id) : null) ||
                  (activeOpeningId       ? openings.find(o => o.id === activeOpeningId)       : null)
                return (
                  <>
                    {batchData.status === 'completed' && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
                        {batchOpening && (
                          <div style={{ fontWeight: 700, fontSize: '1rem', color: 'var(--text)' }}>
                            {batchOpening.title}
                          </div>
                        )}
                        <span className="char-count">
                          {batchData.candidates.filter(c => !c._duplicate_of && c.filter_status === 'qualified').length} qualified
                          {' · '}
                          {batchData.candidates.filter(c => !c._duplicate_of && c.filter_status !== 'qualified').length} filtered
                        </span>
                        <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                          {batchOpening && canEdit && (
                            <button className="btn-analyze" style={{ fontSize: '0.85rem', padding: '8px 18px' }}
                              onClick={() => { setActiveOpening(batchOpening.id); setResult(null); setInterview(null); handleNavigate('single') }}>
                              👤 Add Single
                            </button>
                          )}
                          {batchOpening && canEdit && (
                            <button className="btn-analyze" style={{ fontSize: '0.85rem', padding: '8px 18px' }}
                              onClick={() => { setActiveOpening(batchOpening.id); handleBatchReset() }}>
                              📂 Add Batch
                            </button>
                          )}
                          {batchOpening && canEdit && (() => {
                            const ps = pipelineStatuses[batchOpening.id]
                            const qualifiedCount = batchData.candidates.filter(c => !c._duplicate_of && c.filter_status === 'qualified').length
                            if (ps?.status === 'running') return (
                              <>
                                <span style={{ fontSize: '0.8rem', color: '#1d4ed8', background: '#eff6ff', padding: '5px 12px', borderRadius: 6, border: '1px solid #bfdbfe', whiteSpace: 'nowrap' }}>
                                  {ps.active_count} active · {ps.queue_remaining} queued · {ps.completed_count} done
                                </span>
                                <button className="btn-clear" style={{ padding: '6px 14px', fontSize: '0.8rem', color: '#ef4444', borderColor: '#fca5a5' }}
                                  onClick={() => handleStopPipeline(batchOpening.id)}>
                                  Stop
                                </button>
                              </>
                            )
                            if (ps?.status === 'completed' || ps?.status === 'stopped') return (
                              <span style={{ fontSize: '0.8rem', color: '#059669', background: '#f0fdf4', padding: '5px 12px', borderRadius: 6, border: '1px solid #a7f3d0', whiteSpace: 'nowrap' }}>
                                ✓ {ps.completed_count} interviewed
                              </span>
                            )
                            if (qualifiedCount > 0) return (
                              <button className="btn-analyze" style={{ fontSize: '0.85rem', padding: '8px 18px' }}
                                onClick={() => handleStartPipeline(batchOpening.id)}>
                                📞 Call All Qualified ({qualifiedCount})
                              </button>
                            )
                            return null
                          })()}
                          {batchOpening && (
                            <button className="btn-clear" style={{ padding: '6px 14px', fontSize: '0.8rem' }}
                              onClick={() => { setViewingOpeningId(batchOpening.id); handleNavigate('rankings') }}>
                              📊 View Rankings
                            </button>
                          )}
                          <button className="btn-clear" style={{ padding: '6px 14px', fontSize: '0.8rem' }} onClick={handleBatchReset}>← Back</button>
                        </div>
                      </div>
                    )}
                    <BatchResultsTable
                      candidates={batchData.candidates}
                      isComplete={batchData.status === 'completed'}
                      onCallCandidate={canEdit ? handleCallCandidate : null}
                      canEdit={canEdit}
                    />
                  </>
                )
              })()}
            </div>
          )}

          {/* ── Active Calls ── */}
          {activePage === 'active-calls' && (() => {
            // Build queue-position map: candidate name → queue position (1-indexed)
            const queuePosMap = {}
            Object.values(pipelineStatuses).forEach(ps => {
              ;(ps.queue || []).forEach((c, i) => {
                const key = c.name || c.file_name
                if (key && !queuePosMap[key]) queuePosMap[key] = i + 1
              })
            })

            // Build live-status map from allActiveCalls
            const liveMap = {}
            allActiveCalls.forEach(ac => {
              if (ac.name) liveMap[ac.name] = ac
            })

            // Gather base candidates from all pipeline openings
            const pipelineOpeningIds = Object.keys(pipelineStatuses)
            let rows = []
            pipelineOpeningIds.forEach(oid => {
              const op = openings.find(o => o.id === oid)
              if (op?.candidates?.length > 0) {
                op.candidates.forEach(c => { if (!rows.some(r => r.name === c.name)) rows.push(c) })
              }
            })

            // Fallback: if opening candidates not loaded yet, show active calls + queue
            if (rows.length === 0) {
              rows = [...allActiveCalls]
              Object.values(pipelineStatuses).forEach(ps => {
                ;(ps.queue || []).forEach(c => {
                  if (!rows.some(r => r.name === c.name)) rows.push(c)
                })
              })
            }

            // Overlay live status + add queue position
            rows = rows.map(r => {
              const live = liveMap[r.name]
              const qPos = queuePosMap[r.name || r.file_name]
              return {
                ...r,
                ...(live ? { interview_status: live.interview_status, processing_step: live.processing_step || null } : {}),
                ...(qPos ? { _queue_position: qPos } : {}),
              }
            })

            // Sort: in-call → processing → queued (by pos) → callback → completed → failed/abandoned
            const statusOrder = s => {
              if (s === 'calling' || s === 'in_progress') return 0
              if (s === 'processing') return 1
              if (s === 'callback_scheduled') return 3
              if (s === 'completed') return 4
              return 5
            }
            rows.sort((a, b) => {
              if (a._queue_position && b._queue_position) return a._queue_position - b._queue_position
              if (a._queue_position) return 1   // queued after active
              if (b._queue_position) return -1
              const oa = statusOrder(a.interview_status), ob = statusOrder(b.interview_status)
              if (oa !== ob) return oa - ob
              return (b.resume_score || 0) - (a.resume_score || 0)
            })

            const pipelineSummaries = Object.entries(pipelineStatuses).filter(([, ps]) => ps.status !== 'none')

            if (rows.length === 0 && pipelineSummaries.length === 0) return (
              <div className="db-empty-state">
                <div className="db-empty-icon">📞</div>
                <div className="db-empty-title">No active calls right now</div>
                <div className="db-empty-desc">Start a batch pipeline to see live call status here.</div>
                <button className="btn-analyze" onClick={() => handleNavigate('batch')}>
                  Go to Batch Pipeline
                </button>
              </div>
            )

            return (
              <div>
                {pipelineSummaries.map(([oid, ps]) => {
                  const op = openings.find(o => o.id === oid)
                  return (
                    <div key={oid} style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
                      <div style={{ fontWeight: 700 }}>{op?.title || 'Pipeline'}</div>
                      <span className="char-count">
                        {ps.active_count} in call · {ps.queue_remaining} queued · {ps.completed_count} completed · {ps.skipped_count} skipped
                      </span>
                      <span className={`score-verdict score-verdict--sm ${ps.status === 'running' ? 'verdict-medium' : 'verdict-high'}`} style={{ marginLeft: 'auto' }}>
                        {ps.status === 'running' ? '● Running' : ps.status === 'stopped' ? '■ Stopped' : '✓ Completed'}
                      </span>
                      {ps.status === 'running' && (
                        <button
                          className="btn-clear"
                          style={{ padding: '5px 12px', fontSize: '0.78rem', color: '#ef4444', borderColor: '#fca5a5' }}
                          onClick={() => handleStopPipeline(oid)}
                        >
                          ■ Stop Pipeline
                        </button>
                      )}
                    </div>
                  )
                })}
                <BatchResultsTable
                  candidates={rows}
                  isComplete={false}
                  onCallCandidate={canEdit ? handleCallCandidate : null}
                  canEdit={canEdit}
                />
              </div>
            )
          })()}

          {/* ── Callbacks ── */}
          {activePage === 'callbacks' && (
            callbackCandidates.length > 0
              ? <BatchResultsTable candidates={callbackCandidates} isComplete={false} />
              : (
                <div className="db-empty-state">
                  <div className="db-empty-icon">📅</div>
                  <div className="db-empty-title">No callbacks scheduled</div>
                  <div className="db-empty-desc">
                    Candidates who request a callback will appear here with their scheduled time.
                  </div>
                  <button className="btn-analyze" onClick={() => handleNavigate('batch')}>
                    Go to Batch Pipeline
                  </button>
                </div>
              )
          )}

          {/* ── Rankings ── */}
          {activePage === 'rankings' && (() => {
            const viewOpening =
              (viewingOpeningId  ? openings.find(o => o.id === viewingOpeningId)          : null) ||
              (batchData?.opening_id ? openings.find(o => o.id === batchData.opening_id) : null) ||
              (activeOpeningId   ? openings.find(o => o.id === activeOpeningId)           : null)
            const savedCandidates = viewOpening?.candidates?.length > 0 ? viewOpening.candidates : null
            // Only use batchData if it belongs to the opening being viewed (prevents stale data from deleted openings leaking in)
            const batchBelongsHere = !viewOpening || (batchData?.opening_id && batchData.opening_id === viewOpening.id)
            const liveCandidates  = (batchBelongsHere && batchData?.candidates?.length > 0) ? batchData.candidates : null
            const rankCandidates  = savedCandidates || liveCandidates
            return rankCandidates
              ? (
                <div>
                  {viewOpening && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
                      <div style={{ fontWeight: 700, fontSize: '1rem', color: 'var(--text)' }}>
                        {viewOpening.title}
                      </div>
                      <span className="char-count">{rankCandidates.length} candidate{rankCandidates.length !== 1 ? 's' : ''}{viewingOpeningId ? ' · all runs' : savedCandidates ? ' · all runs' : ' · current batch'}</span>
                      <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center' }}>
                        {canEdit && <button className="btn-analyze"
                          style={{ fontSize: '0.85rem', padding: '8px 18px' }}
                          onClick={() => { setActiveOpening(viewOpening.id); setResult(null); setInterview(null); handleNavigate('single') }}>
                          👤 Add Single
                        </button>}
                        {canEdit && <button className="btn-analyze"
                          style={{ fontSize: '0.85rem', padding: '8px 18px' }}
                          onClick={() => { setActiveOpening(viewOpening.id); handleBatchReset(); handleNavigate('batch') }}>
                          📂 Add Batch
                        </button>}
                        {canEdit && (() => {
                          const ps = pipelineStatuses[viewOpening.id]
                          const qualifiedCount = (viewOpening.candidates || rankCandidates || []).filter(c => !c._duplicate_of && c.filter_status === 'qualified').length
                          if (ps?.status === 'running') return (
                            <>
                              <span style={{ fontSize: '0.8rem', color: '#1d4ed8', background: '#eff6ff', padding: '5px 12px', borderRadius: 6, border: '1px solid #bfdbfe', whiteSpace: 'nowrap' }}>
                                {ps.active_count} active · {ps.queue_remaining} queued · {ps.completed_count} done
                              </span>
                              <button className="btn-clear" style={{ padding: '6px 14px', fontSize: '0.8rem', color: '#ef4444', borderColor: '#fca5a5' }}
                                onClick={() => handleStopPipeline(viewOpening.id)}>
                                Stop
                              </button>
                            </>
                          )
                          if (ps?.status === 'completed' || ps?.status === 'stopped') return (
                            <span style={{ fontSize: '0.8rem', color: '#059669', background: '#f0fdf4', padding: '5px 12px', borderRadius: 6, border: '1px solid #a7f3d0', whiteSpace: 'nowrap' }}>
                              ✓ {ps.completed_count} interviewed
                            </span>
                          )
                          if (qualifiedCount > 0) return (
                            <button className="btn-analyze" style={{ fontSize: '0.85rem', padding: '8px 18px' }}
                              onClick={() => handleStartPipeline(viewOpening.id)}>
                              📞 Call All Qualified ({qualifiedCount})
                            </button>
                          )
                          return null
                        })()}
                        <button className="btn-clear" style={{ padding: '6px 14px', fontSize: '0.8rem' }}
                          onClick={() => { setViewingOpeningId(null); handleNavigate('dashboard') }}>
                          ← Back
                        </button>
                      </div>
                    </div>
                  )}
                  <BatchResultsTable
                    candidates={rankCandidates}
                    isComplete={true}
                    onCallCandidate={canEdit ? handleCallCandidate : null}
                    canEdit={canEdit}
                    onResolve={canEdit ? syncOpenings : null}
                    allActiveCalls={allActiveCalls}
                  />
                </div>
              )
              : result
              ? (
                <div>
                  <div className="db-section-title" style={{ marginBottom: 16 }}>
                    Single Candidate Result
                  </div>
                  <div className="db-recent-card" onClick={() => handleNavigate('single')}
                    style={{ cursor: 'pointer', maxWidth: 560 }}>
                    <div className="db-recent-avatar">
                      {(result.name || '?')[0].toUpperCase()}
                    </div>
                    <div style={{ flex: 1 }}>
                      <div className="db-recent-name">{result.name || 'Candidate'}</div>
                      <div className="db-recent-meta">
                        Resume score: {result.match_score || '—'}
                        {result.email ? ` · ${result.email}` : ''}
                      </div>
                    </div>
                    <span className={`score-verdict ${
                      (parseInt(result.match_score) || 0) >= 75 ? 'verdict-high'
                      : (parseInt(result.match_score) || 0) >= 60 ? 'verdict-medium'
                      : 'verdict-low'
                    }`} style={{ fontSize: '0.75rem' }}>
                      {result.verdict || 'Analyzed'}
                    </span>
                  </div>
                  <p style={{ fontSize: '0.8rem', color: 'var(--text-2)', marginTop: 12 }}>
                    Click the card to view the full analysis. Run a batch to see ranked results across multiple candidates.
                  </p>
                  <button className="btn-analyze" style={{ marginTop: 16 }} onClick={() => handleNavigate('batch')}>
                    Run Batch Pipeline
                  </button>
                </div>
              )
              : (
                <div className="db-empty-state">
                  <div className="db-empty-icon">🏆</div>
                  <div className="db-empty-title">No rankings yet</div>
                  <div className="db-empty-desc">
                    Run a batch pipeline to see candidates ranked by their combined resume and interview score.
                  </div>
                  <button className="btn-analyze" onClick={() => handleNavigate('batch')}>
                    Run Batch Pipeline
                  </button>
                </div>
              )
          })()}

          {/* ── JD Builder (admin + super_admin only) ── */}
          {canEdit && activePage === 'jd-builder' && (
            <JdBuilder onNavigate={handleNavigate} syncOpenings={syncOpenings} />
          )}

          {/* ── Settings (super_admin only) ── */}
          {authUser?.role === 'super_admin' && activePage === 'settings' && (
            <Settings />
          )}

          {/* ── Change Password (all roles) + Manage Access (super_admin only) ── */}
          {(activePage === 'change-password' ||
            (authUser?.role === 'super_admin' && (activePage === 'add-user' || activePage === 'user-list'))
          ) && (
            <UserManagement
              onClose={null}
              initialSection={activePage}
              onPasswordChanged={({ token }) => {
                sessionStorage.setItem('auth_token', token)
                const updated = { ...authUser, must_change_password: false }
                sessionStorage.setItem('auth_user', JSON.stringify(updated))
                setAuthUser(updated)
                if (activePage === 'change-password') setActivePage('dashboard')
              }}
            />
          )}

        </main>
      </div>

      {/* ── Duplicate Candidate Modal ── */}
      {duplicateModal && (
        <div className="batch-modal-overlay" onClick={duplicateModal.onCancel}>
          <div
            className="batch-modal-panel"
            style={{ maxWidth: 460, padding: '32px 28px' }}
            onClick={e => e.stopPropagation()}
          >
            <div style={{ fontSize: '1.5rem', marginBottom: 10 }}>⚠️</div>
            <div style={{ fontWeight: 700, fontSize: '1.05rem', color: 'var(--text-1)', marginBottom: 6 }}>
              Candidate Already Exists
            </div>
            <div style={{ fontSize: '0.85rem', color: 'var(--text-2)', marginBottom: 20 }}>
              A candidate with the same{' '}
              {duplicateModal.existing.email ? 'email or name' : 'name'}{' '}
              was previously submitted to this opening.
            </div>

            <div style={{ background: 'var(--surface-2, #f8f9fa)', borderRadius: 10, padding: '14px 16px', marginBottom: 22 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
                <div className="candidate-avatar candidate-avatar--sm">
                  {(duplicateModal.existing.name || '?')[0].toUpperCase()}
                </div>
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.9rem', color: 'var(--text-1)' }}>
                    {duplicateModal.existing.name || '—'}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-3)' }}>
                    {duplicateModal.existing.email || duplicateModal.existing.file_name || '—'}
                  </div>
                </div>
              </div>
              <div style={{ fontSize: '0.8rem', color: 'var(--text-2)', display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div>
                  <span style={{ color: 'var(--text-3)' }}>Resume Score: </span>
                  <strong>{duplicateModal.existing.resume_score ?? '—'}</strong>
                  {duplicateModal.existing.resume_score != null && ' / 100'}
                </div>
                <div>
                  <span style={{ color: 'var(--text-3)' }}>Last Status: </span>
                  <strong>{candidateStatusLabel(duplicateModal.existing)}</strong>
                </div>
                {duplicateModal.existing.combined_score != null && (
                  <div>
                    <span style={{ color: 'var(--text-3)' }}>Combined Score: </span>
                    <strong>{duplicateModal.existing.combined_score}</strong>
                  </div>
                )}
              </div>
            </div>

            <div style={{ display: 'flex', gap: 10 }}>
              <button
                className="btn-analyze"
                style={{ flex: 1, fontSize: '0.85rem', padding: '9px 0' }}
                onClick={duplicateModal.onAddAnyway}
              >
                Add Anyway
              </button>
              <button
                className="btn-clear"
                style={{ flex: 1, fontSize: '0.85rem', padding: '9px 0' }}
                onClick={duplicateModal.onCancel}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {visibleDueCallbacks.length > 0 && (
        <CallbackAlertModal
          callbacks={visibleDueCallbacks}
          onCall={(cb) => {
            handleCallCandidate({ interview_id: cb.interview_id, phone: cb.phone, file_name: null })
            setDismissedCallbacks(prev => ({ ...prev, [cb.interview_id]: Date.now() }))
          }}
          onSnooze={(cb) => setDismissedCallbacks(prev => ({ ...prev, [cb.interview_id]: Date.now() }))}
          onDismissAll={() => {
            const all = {}
            visibleDueCallbacks.forEach(cb => { all[cb.interview_id] = Date.now() })
            setDismissedCallbacks(prev => ({ ...prev, ...all }))
          }}
        />
      )}

    </div>
  )
}
