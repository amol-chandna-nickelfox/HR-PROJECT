import { useState, useRef } from 'react'
import { apiStartBatch, apiBatchStatus, apiBatchInterviewStart, apiRecallInterview, safeJson } from '../api/client'

export function useBatch({ onSyncToOpenings, onAutoSaveJd, getActiveOpeningTitle, getActiveOpeningId } = {}) {
  const [batchFiles,   setBatchFiles]   = useState([])
  const [batchId,      setBatchId]      = useState(null)
  const [batchData,    setBatchData]    = useState(null)
  const [batchLoading, setBatchLoading] = useState(false)
  const [batchError,   setBatchError]   = useState('')
  const batchPollRef = useRef(null)

  const startBatchPolling = (id) => {
    clearTimeout(batchPollRef.current)
    const tick = async () => {
      try {
        const res = await apiBatchStatus(id)
        if (!res.ok) return
        const data = await res.json()
        setBatchData(data)
        if (data.candidates?.length) onSyncToOpenings?.(id, data.candidates)
        // Note: 'callback_scheduled' is intentionally NOT "active" — a scheduled callback
        // may be hours/days out, so counting it would keep this batch polling forever.
        const hasActive = data.candidates?.some(c =>
          ['calling', 'in_progress', 'processing'].includes(c.interview_status)
        )
        if (data.status === 'completed' && !hasActive) return
        const interval = hasActive || data.status === 'processing' ? 2500 : 6000
        batchPollRef.current = setTimeout(tick, interval)
      } catch (_) {}
    }
    batchPollRef.current = setTimeout(tick, 2500)
  }

  const handleBatchStart = async (files, jd) => {
    const openingId = getActiveOpeningId?.() || ''
    if (!openingId) {
      const proceed = window.confirm(
        'No job opening is selected — this batch won\'t appear on any opening\'s candidate list ' +
        'or dashboard card, only in this session\'s results view. Continue anyway?'
      )
      if (!proceed) return
    }
    setBatchLoading(true)
    setBatchError('')
    setBatchData(null)
    onAutoSaveJd?.(jd)
    const form = new FormData()
    form.append('jd_text', jd)
    form.append('job_title', getActiveOpeningTitle?.() || '')
    form.append('opening_id', openingId)
    files.forEach(f => form.append('files', f))
    try {
      const res = await apiStartBatch(form)
      if (!res.ok) {
        const e = await safeJson(res)
        throw new Error(e.detail || 'Failed to start batch')
      }
      const data = await res.json()
      setBatchId(data.batch_id)
      startBatchPolling(data.batch_id)
    } catch (e) {
      setBatchError(e.message)
    } finally {
      setBatchLoading(false)
    }
  }

  const handleBatchReset = () => {
    clearTimeout(batchPollRef.current)
    setBatchFiles([])
    setBatchId(null)
    setBatchData(null)
    setBatchError('')
  }

  const handleCallCandidate = async (candidate, currentBatchId, viewingOpeningId, setOpenings, saveOpenings) => {
    if (!candidate.phone) return
    try {
      let newIid = candidate.interview_id
      let res

      if (newIid) {
        res = await apiRecallInterview(newIid)
      } else {
        const bid = candidate._batchId || currentBatchId
        if (!bid) {
          alert('Cannot start call: batch ID not found. Please re-run the batch.')
          return
        }
        res = await apiBatchInterviewStart(bid, candidate.file_name)
        if (res.ok) {
          const d = await res.json()
          newIid = d.interview_id
        }
      }

      if (!res.ok) {
        const err = await safeJson(res)
        alert(err.detail || 'Failed to start call.')
        return
      }

      const pid = candidate._batchId || currentBatchId
      if (batchData) {
        setBatchData(prev => ({
          ...prev,
          candidates: prev.candidates.map(c =>
            c.file_name === candidate.file_name
              ? { ...c, interview_id: newIid, interview_status: 'calling' }
              : c
          ),
        }))
      }
      if (pid) startBatchPolling(pid)

      if (viewingOpeningId && setOpenings && saveOpenings) {
        setOpenings(prev => {
          const next = prev.map(o => {
            if (o.id !== viewingOpeningId) return o
            return {
              ...o,
              candidates: (o.candidates || []).map(c =>
                c.file_name === candidate.file_name
                  ? { ...c, interview_id: newIid, interview_status: 'calling' }
                  : c
              ),
            }
          })
          saveOpenings(next)
          return next
        })
      }
    } catch (e) {
      alert(e.message || 'Failed to start call.')
    }
  }

  return {
    batchFiles, setBatchFiles,
    batchId, setBatchId,
    batchData, setBatchData,
    batchLoading, batchError,
    handleBatchStart, handleBatchReset, handleCallCandidate,
    startBatchPolling,
  }
}
