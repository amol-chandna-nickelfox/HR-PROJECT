import { useState, useCallback, useRef, useEffect } from 'react'

// ── batch file helpers ──
const ACCEPTED_EXT = ['.pdf', '.docx', '.doc']
function isAccepted(name) {
  return ACCEPTED_EXT.some(ext => name.toLowerCase().endsWith(ext))
}
async function readEntry(entry) {
  if (entry.isFile) return new Promise((res, rej) => entry.file(res, rej))
  if (entry.isDirectory) {
    const reader = entry.createReader()
    const all = []
    await new Promise(resolve => {
      const read = () => reader.readEntries(batch => {
        if (!batch.length) { resolve(); return }
        all.push(...batch); read()
      })
      read()
    })
    return (await Promise.all(all.map(readEntry))).flat()
  }
  return []
}

const RESUME_PLACEHOLDER = `Paste the candidate's full resume here…

John Doe · john@email.com · +91-9876543210

Skills: Python, Machine Learning, NLP, Docker
Experience: 3 years at ABC Corp as ML Engineer`

const JD_PLACEHOLDER = `Paste the job description here…

Job Title: Machine Learning Engineer
Required Skills: Python, TensorFlow, NLP
Experience: 2–4 years`

// ─────────────────────────────────────────────
// Page intros (replaces the old hero section)
// ─────────────────────────────────────────────
function PageIntro({ mode }) {
  if (mode === 'batch') {
    return (
      <div className="page-intro">
        <div className="page-intro-steps">
          <span className="page-intro-step"><span className="step-num">1</span> Upload resumes</span>
          <span className="step-arrow">→</span>
          <span className="page-intro-step"><span className="step-num">2</span> AI scores all</span>
          <span className="step-arrow">→</span>
          <span className="page-intro-step"><span className="step-num">3</span> Filter by threshold</span>
          <span className="step-arrow">→</span>
          <span className="page-intro-step"><span className="step-num">4</span> Auto-interview</span>
          <span className="step-arrow">→</span>
          <span className="page-intro-step"><span className="step-num">5</span> Ranked report</span>
        </div>
      </div>
    )
  }
  return (
    <div className="page-intro">
      <div className="page-intro-steps">
        <span className="page-intro-step"><span className="step-num">1</span> Paste resume</span>
        <span className="step-arrow">→</span>
        <span className="page-intro-step"><span className="step-num">2</span> Add job description</span>
        <span className="step-arrow">→</span>
        <span className="page-intro-step"><span className="step-num">3</span> AI match score</span>
        <span className="step-arrow">→</span>
        <span className="page-intro-step"><span className="step-num">4</span> Phone interview</span>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────
export default function InputSection({
  onAnalyze, onClear, loading, error,
  batchFiles, onBatchFilesChange, onBatchStart, batchLoading, batchError,
  mode = 'single', // 'single' | 'batch'
  defaultJd = '',
  readOnly = false,
}) {
  const [resume,     setResume]     = useState('')
  const [jd,         setJd]         = useState(defaultJd)
  useEffect(() => { if (defaultJd) setJd(defaultJd) }, [defaultJd])
  const [uploading,  setUploading]  = useState(false)
  const [fileName,   setFileName]   = useState('')
  const [uploadErr,  setUploadErr]  = useState('')
  const [resumeDrag, setResumeDrag] = useState(false)
  const [jdDrag,     setJdDrag]     = useState(false)

  const fileInputRef = useRef(null)
  const batchFileRef = useRef(null)
  const folderRef    = useRef(null)

  const isBatch = mode === 'batch'

  // ── batch file helpers ──
  const addBatchFiles = (newFiles) => {
    const accepted = Array.from(newFiles).filter(f => isAccepted(f.name))
    const existing = new Set(batchFiles.map(f => f.name))
    onBatchFilesChange([...batchFiles, ...accepted.filter(f => !existing.has(f.name))])
  }
  const removeBatchFile = (name) => onBatchFilesChange(batchFiles.filter(f => f.name !== name))

  // ── submit ──
  const handleSubmit = useCallback(() => {
    if (loading || batchLoading) return
    if (isBatch) {
      if (!jd.trim() || !batchFiles.length) return
      onBatchStart(batchFiles, jd)
    } else {
      if (!resume.trim() || !jd.trim()) return
      onAnalyze(resume, jd)
    }
  }, [loading, batchLoading, isBatch, batchFiles, jd, resume, onBatchStart, onAnalyze])

  const handleKeyDown = useCallback((e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') handleSubmit()
  }, [handleSubmit])

  const handleClear = () => {
    if (!window.confirm('Are you sure you want to clear the job description and resume?')) return
    setResume(''); setJd(defaultJd || ''); setFileName(''); setUploadErr('')
    if (isBatch) onBatchFilesChange([])
    onClear?.()
  }

  // ── single-file upload ──
  const uploadFile = async (file) => {
    setUploading(true); setUploadErr(''); setFileName(file.name)
    const form = new FormData()
    form.append('file', file)
    try {
      const token = sessionStorage.getItem('auth_token')
      const res = await fetch('/upload-resume', {
        method:  'POST',
        // No Content-Type here — the browser must set its own multipart boundary for FormData.
        headers: token ? { 'Authorization': `Bearer ${token}` } : {},
        body:    form,
      })
      if (!res.ok) {
        let detail = 'Upload failed'
        try { const err = await res.json(); detail = err.detail || detail } catch {}
        throw new Error(detail)
      }
      const data = await res.json()
      setResume(data.resume_text)
    } catch (e) {
      setUploadErr(e.message || 'Failed to parse file.'); setFileName('')
    } finally {
      setUploading(false)
    }
  }

  const handleFileChange = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    await uploadFile(file)
    e.target.value = ''
  }

  // ── drag-and-drop ──
  const handleResumeDrop = async (e) => {
    e.preventDefault(); setResumeDrag(false)
    if (isBatch) {
      const items = Array.from(e.dataTransfer.items || [])
      if (items.length > 0 && items[0].webkitGetAsEntry) {
        const entries = items.map(i => i.webkitGetAsEntry()).filter(Boolean)
        const allFiles = (await Promise.all(entries.map(readEntry))).flat()
        addBatchFiles(allFiles)
      } else {
        addBatchFiles(e.dataTransfer.files)
      }
    } else {
      const items = Array.from(e.dataTransfer.items || [])
      if (items.length > 0 && items[0].webkitGetAsEntry) {
        const entries = items.map(i => i.webkitGetAsEntry()).filter(Boolean)
        const file = await (async () => {
          if (entries[0]?.isFile) return new Promise((res, rej) => entries[0].file(res, rej))
          return e.dataTransfer.files?.[0]
        })()
        if (file) await uploadFile(file)
      } else {
        const file = e.dataTransfer.files?.[0]
        if (file) await uploadFile(file)
      }
    }
  }

  const handleJdDrop = (e) => {
    e.preventDefault(); setJdDrag(false)
    const file = e.dataTransfer.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => setJd(ev.target.result || '')
    reader.readAsText(file)
  }

  return (
    <section className="input-section" onKeyDown={handleKeyDown}>

      <PageIntro mode={mode} />

      <div className="input-grid">

        {/* ── LEFT CARD ── */}
        <div
          className={`input-card${resumeDrag ? ' drag-over' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setResumeDrag(true) }}
          onDragLeave={() => setResumeDrag(false)}
          onDrop={handleResumeDrop}
        >
          <div className="input-card-header">
            <div className="input-card-title">
              <span className="input-card-icon">{isBatch ? '📂' : '📄'}</span>
              {isBatch ? `Resume Files${batchFiles.length > 0 ? ` (${batchFiles.length})` : ''}` : 'Resume'}
            </div>
            {!isBatch && (
              <div className="resume-header-right">
                {fileName && !uploadErr && (
                  <span className="upload-filename">{fileName}</span>
                )}
                <button
                  className="btn-upload-pdf"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading || loading}
                >
                  {uploading
                    ? <><div className="spinner-sm" /> Parsing…</>
                    : <>↑ Upload PDF</>
                  }
                </button>
                {resume.length > 0 && (
                  <span className="char-count">{resume.length} chars</span>
                )}
              </div>
            )}
          </div>

          {uploadErr && <div className="upload-error">{uploadErr}</div>}

          {/* BATCH: empty drop zone */}
          {isBatch && batchFiles.length === 0 && (
            <div className="batch-dropzone-empty">
              <div className="batch-dz-icon">📂</div>
              <div className="batch-dz-title">Drop resume files here</div>
              <div className="batch-dz-sub">PDF, DOCX or DOC · supports folders and multiple files</div>
              <div className="batch-dz-btns">
                <button className="batch-browse-btn" type="button"
                  onClick={() => batchFileRef.current?.click()} disabled={batchLoading}>
                  📄 Browse Files
                </button>
                <button className="batch-browse-btn batch-browse-btn--folder" type="button"
                  onClick={() => folderRef.current?.click()} disabled={batchLoading}>
                  📁 Select Folder
                </button>
              </div>
            </div>
          )}

          {/* BATCH: files loaded */}
          {isBatch && batchFiles.length > 0 && (
            <div className="batch-files-loaded">
              <div className="batch-file-chips">
                {batchFiles.map(f => (
                  <span key={f.name} className="batch-file-chip">
                    <span className="batch-file-chip-name">📄 {f.name}</span>
                    <button className="batch-file-chip-remove"
                      onClick={() => removeBatchFile(f.name)} title="Remove">×</button>
                  </span>
                ))}
              </div>
              <div className="batch-drop-btns" style={{ marginTop: 10 }}>
                <button className="batch-browse-btn" type="button"
                  onClick={() => batchFileRef.current?.click()} disabled={batchLoading}>
                  📄 Add More
                </button>
                <button className="batch-browse-btn batch-browse-btn--folder" type="button"
                  onClick={() => folderRef.current?.click()} disabled={batchLoading}>
                  📁 Add Folder
                </button>
              </div>
            </div>
          )}

          {/* SINGLE: resume textarea */}
          {!isBatch && (
            <textarea
              className="input-area"
              placeholder={RESUME_PLACEHOLDER}
              value={resume}
              onChange={e => setResume(e.target.value)}
              spellCheck={false}
            />
          )}
        </div>

        {/* ── RIGHT CARD: Job Description ── */}
        <div
          className={`input-card${jdDrag ? ' drag-over' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setJdDrag(true) }}
          onDragLeave={() => setJdDrag(false)}
          onDrop={handleJdDrop}
        >
          <div className="input-card-header">
            <div className="input-card-title">
              <span className="input-card-icon">📋</span>
              Job Description
            </div>
            {jd.length > 0 && <span className="char-count">{jd.length} chars</span>}
          </div>
          <textarea
            className="input-area"
            placeholder={JD_PLACEHOLDER}
            value={jd}
            onChange={e => setJd(e.target.value)}
            spellCheck={false}
          />
        </div>
      </div>

      {/* ── Actions ── */}
      <div className="btn-row">
        {readOnly ? (
          <div style={{ padding: '10px 20px', borderRadius: 8, background: 'var(--bg)', border: '1px solid var(--border)', fontSize: '0.82rem', color: 'var(--text-3)' }}>
            👁️ View only — you don't have permission to run analysis
          </div>
        ) : (
          <button
            className="btn-analyze"
            onClick={handleSubmit}
            disabled={
              isBatch
                ? (batchLoading || !jd.trim() || batchFiles.length === 0)
                : (loading || !resume.trim() || !jd.trim())
            }
          >
            {isBatch
              ? batchLoading
                ? <><div className="spinner" /> Starting pipeline…</>
                : <>⚡ Run Batch Pipeline</>
              : loading
                ? <><div className="spinner" /> Analyzing…</>
                : <>⚡ Analyze Candidate</>
            }
          </button>
        )}
        <button className="btn-clear" onClick={handleClear}>
          Clear
        </button>
      </div>

      <p className="hint">
        {isBatch
          ? batchFiles.length > 0
            ? `${batchFiles.length} resume${batchFiles.length !== 1 ? 's' : ''} queued · candidates above the qualification threshold will be auto-interviewed`
            : 'Drop files above or use the browser to add resumes'
          : <><kbd>Ctrl</kbd>+<kbd>Enter</kbd> to analyze · or drag a PDF onto the resume card</>
        }
      </p>

      {!isBatch && error     && <div className="error-banner">⚠️ {error}</div>}
      {isBatch  && batchError && <div className="error-banner">⚠️ {batchError}</div>}

      {/* Hidden file inputs */}
      <input ref={fileInputRef} type="file" accept=".pdf,.docx,.doc"
        style={{ display: 'none' }} onChange={handleFileChange} />
      <input ref={batchFileRef} type="file" multiple accept=".pdf,.docx,.doc"
        style={{ display: 'none' }}
        onChange={e => { addBatchFiles(e.target.files); e.target.value = '' }} />
      <input ref={folderRef} type="file" webkitdirectory="true" mozdirectory="true" multiple
        style={{ display: 'none' }}
        onChange={e => { addBatchFiles(e.target.files); e.target.value = '' }} />
    </section>
  )
}
