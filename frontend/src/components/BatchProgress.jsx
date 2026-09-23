const STATUS_MAP = {
  calling:            { label: 'Calling',           cls: 'verdict-medium' },
  processing:         { label: 'Processing',         cls: 'verdict-medium' },
  completed:          { label: 'Completed',          cls: 'verdict-high'   },
  failed:             { label: 'Failed',             cls: 'verdict-low'    },
  abandoned:          { label: 'Abandoned',          cls: 'verdict-low'    },
  timeout:            { label: 'Timed Out',          cls: 'verdict-low'    },
  callback_scheduled: { label: '📅 Callback Sched.', cls: 'verdict-medium' },
  skipped:            { label: 'Filtered Out',       cls: ''               },
  no_phone:           { label: 'No Phone',           cls: ''               },
  pending:            { label: 'Pending',            cls: ''               },
}

export default function BatchProgress({ batchData }) {
  const { total = 0, completed = 0, candidates = [] } = batchData || {}
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0

  return (
    <div className="batch-progress-wrap">
      <div className="batch-progress-header">
        <span className="section-label">Processing {total} candidates…</span>
        <span className="char-count">{completed} / {total} done</span>
      </div>

      <div className="batch-progress-bar-wrap">
        <div className="batch-progress-bar-fill" style={{ width: `${pct}%` }} />
      </div>

      <div className="batch-candidate-list">
        {candidates.map((c, i) => {
          const st  = STATUS_MAP[c.interview_status] || { label: c.interview_status || '…', cls: '' }
          const isFiltered = c.filter_status === 'filtered_out' || c.filter_status === 'no_phone' || c.filter_status === 'parse_failed'

          return (
            <div key={i} className="batch-candidate-row">
              <div className="candidate-avatar candidate-avatar--sm">
                {(c.name || c.file_name || '?')[0].toUpperCase()}
              </div>

              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontWeight: 600, fontSize: '0.84rem', color: 'var(--text-1)',
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {c.name || c.file_name}
                </div>
                {c.resume_score != null && (
                  <div style={{ fontSize: '0.74rem', color: 'var(--text-3)' }}>
                    Resume:{' '}
                    <span style={{ color: c.resume_score >= 70 ? 'var(--green)' : 'var(--yellow)' }}>
                      {c.resume_score} / 100
                    </span>
                    {c.filter_status === 'filtered_out' && (
                      <span style={{ color: 'var(--text-3)', marginLeft: 6 }}>· below threshold</span>
                    )}
                  </div>
                )}
              </div>

              {isFiltered ? (
                <span style={{ fontSize: '0.72rem', color: 'var(--text-3)' }}
                  title={c.filter_status === 'parse_failed' ? "Couldn't extract text from this file — try re-uploading it" : undefined}>
                  {c.filter_status === 'no_phone' ? 'No Phone'
                    : c.filter_status === 'parse_failed' ? "⚠ Couldn't Read File"
                    : 'Filtered Out'}
                </span>
              ) : (
                <span className={`score-verdict ${st.cls}`} style={{ fontSize: '0.72rem', padding: '2px 10px' }}>
                  {st.label}
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
