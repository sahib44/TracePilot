import { useRef, useState } from 'react'
import type { ReactNode, RefObject } from 'react'
import {
  Activity, ArrowLeft, ArrowUpRight, Check, ChevronDown, CircleHelp, Clipboard,
  FileText, GitBranch, ListChecks, Search, Terminal, TriangleAlert,
} from 'lucide-react'
import { Link, useParams } from 'react-router'
import { ApiError, getInvestigation, JAEGER_URL } from '../api/client'
import type { EvidenceRecord, InvestigationResult, JsonRecord, JsonValue, ToolStep } from '../api/types'
import { useRequest } from '../api/useRequest'
import { formatDate, formatDuration } from '../components/format'
import { StatusBadge } from '../components/StatusBadge'
import '../styles/detail.css'

type SelectEvidence = (id: string) => void

const evidenceTypes = [
  { tool: 'search_logs', name: 'Logs', icon: FileText },
  { tool: 'query_metrics', name: 'Metrics', icon: Activity },
  { tool: 'get_traces', name: 'Traces', icon: GitBranch },
  { tool: 'get_service_health', name: 'Health', icon: ListChecks },
]

const metricNames = new Map([
  ['requests', 'Request count'],
  ['errors', 'Request error count'],
  ['error_rate', 'HTTP 5xx rate'],
  ['latency', 'P95 latency'],
  ['availability', 'Scrape availability'],
])
const numberFormat = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 })

const stopReasons = {
  timeout: 'The investigation reached its time limit.',
  tool_call_limit: 'The investigation reached its diagnostic tool limit.',
  model_unavailable: 'The local model was unavailable. Check that Ollama is running and qwen3:4b is installed.',
  malformed_model_response: 'The local model returned a response that could not be read.',
  invalid_evidence: 'The local model cited evidence that the investigator did not retrieve.',
}

function textField(fields: JsonRecord, key: string) {
  const value = fields[key]
  return typeof value === 'string' ? value : undefined
}

function numberField(fields: JsonRecord, key: string) {
  const value = fields[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function formatNumber(value: number) {
  return numberFormat.format(value)
}

function EvidenceTime({ value }: { value: string }) {
  return <time dateTime={value} title={value}>{formatDate(value)}</time>
}

function JaegerLink({ traceId }: { traceId?: string }) {
  if (!traceId || !/^[a-f\d]{32}$/i.test(traceId) || /^0+$/.test(traceId)) return null
  return (
    <a className="detail-external" href={`${JAEGER_URL.replace(/\/+$/, '')}/trace/${traceId}`}
      target="_blank" rel="noreferrer">
      Open in Jaeger <ArrowUpRight size={13} aria-hidden="true" />
    </a>
  )
}

function CopyIdentifier({ label, value }: { label: string; value: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'error'>('idle')

  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setState('copied')
    } catch {
      setState('error')
    }
  }

  const Icon = state === 'copied' ? Check : Clipboard
  return (
    <>
      <span className="detail-identifier">
        <code>{value}</code>
        <button type="button" className="icon-button detail-copy" onClick={copy}
          aria-label={`Copy ${label}`} title={`Copy ${label}`}>
          <Icon size={14} aria-hidden="true" />
        </button>
      </span>
      <span className={state === 'error' ? 'detail-error-text' : 'sr-only'} role="status">
        {state === 'copied' && `${label} copied.`}
        {state === 'error' && `Could not copy ${label}. Select the identifier to copy it manually.`}
      </span>
    </>
  )
}

function Citations({ ids, known, onSelect }: {
  ids: string[]
  known: Set<string>
  onSelect: SelectEvidence
}) {
  const resolved = [...new Set(ids)].filter(id => known.has(id))
  if (!resolved.length) return null
  return (
    <span className="detail-citations">
      {resolved.map(id => (
        <button key={id} type="button" className="detail-citation" onClick={() => onSelect(id)}
          aria-label={`Inspect evidence ${id}`}>{id}</button>
      ))}
    </span>
  )
}

function EvidenceFields({ items }: { items: [string, ReactNode][] }) {
  const shown = items.filter(([, value]) => value != null)
  if (!shown.length) return null
  return (
    <dl className="detail-fields">
      {shown.map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
      ))}
    </dl>
  )
}

function formatError(value: JsonValue | undefined) {
  if (!value || typeof value === 'string') return value
  return JSON.stringify(value)
}

function traceDepth(span: JsonRecord, byId: Map<string | undefined, JsonRecord>) {
  const seen = new Set([textField(span, 'span_id')])
  let parent = textField(span, 'parent_span_id')
  let depth = 0
  while (parent && !seen.has(parent)) {
    const ancestor = byId.get(parent)
    if (!ancestor) break
    seen.add(parent)
    depth += 1
    parent = textField(ancestor, 'parent_span_id')
  }
  return Math.min(2, depth)
}

function TraceRecord({ fields }: { fields: JsonRecord }) {
  const rawSpans = fields.spans
  const spans = Array.isArray(rawSpans)
    ? rawSpans.filter((span): span is JsonRecord => span !== null && typeof span === 'object' && !Array.isArray(span))
    : []
  const byId = new Map(spans.map(span => [textField(span, 'span_id'), span]))
  const total = numberField(fields, 'duration_ms')
  const count = numberField(fields, 'span_count')
  const traceId = textField(fields, 'trace_id')
  const started = textField(fields, 'started_at')

  return (
    <>
      <EvidenceFields items={[
        ['Trace ID', traceId && <CopyIdentifier label="evidence trace ID" value={traceId} />],
        ['Started', started && <EvidenceTime value={started} />],
        ['Duration', total == null ? null : `${formatNumber(total)} ms`],
        ['Spans', count == null ? null : `${spans.length} shown of ${count}`],
      ]} />
      <JaegerLink traceId={traceId} />
      {spans.length > 0 && (
        <div className="detail-spans">
          <h4>Trace spans</h4>
          <ol aria-label="Trace spans">
            {spans.map((span, index) => {
              const id = textField(span, 'span_id')
              const parent = textField(span, 'parent_span_id')
              const status = textField(span, 'status')
              const offset = numberField(span, 'start_offset_ms')
              const duration = numberField(span, 'duration_ms')
              const errorType = textField(span, 'error_type')
              const httpStatus = numberField(span, 'http_status')
              const sqlstate = textField(span, 'sqlstate')
              const showBar = total != null && total > 0 && offset != null && duration != null
              const left = showBar ? Math.min(100, Math.max(0, 100 * offset / total)) : 0
              const width = showBar ? Math.min(100 - left, Math.max(0, 100 * duration / total)) : 0
              return (
                <li className={`detail-span detail-span-depth-${traceDepth(span, byId)}`} key={id ?? index}>
                  <div className="detail-span-top">
                    <div>
                      <span className="detail-span-service">{textField(span, 'service')}</span>
                      <strong>{textField(span, 'operation') ?? id ?? 'Span'}</strong>
                    </div>
                    {status && <span className={`detail-tag ${status === 'error' ? 'detail-tag-error' : ''}`}>{status}</span>}
                  </div>
                  <div className="detail-span-time">
                    {offset != null && <span>+{formatNumber(offset)} ms</span>}
                    {duration != null && <span>{formatNumber(duration)} ms duration</span>}
                  </div>
                  {showBar && (
                    <div className="detail-span-track" aria-hidden="true">
                      <span className={status === 'error' ? 'detail-span-error' : ''} style={{
                        insetInlineStart: `${left}%`, width: `${width}%`,
                      }} />
                    </div>
                  )}
                  <div className="detail-span-ids">
                    {id && <span>Span <code>{id}</code></span>}
                    {parent && (
                      <span>Parent <code>{parent}</code>{!byId.has(parent) && ' (not in retrieved spans)'}</span>
                    )}
                  </div>
                  {(errorType || httpStatus != null || sqlstate) && (
                    <div className="detail-span-diagnostics">
                      {httpStatus != null && <span>HTTP {httpStatus}</span>}
                      {errorType && <span className="detail-error-text">{errorType}</span>}
                      {sqlstate && <span>SQLSTATE {sqlstate}</span>}
                    </div>
                  )}
                </li>
              )
            })}
          </ol>
        </div>
      )}
    </>
  )
}

function evidenceLabel({ tool, record }: EvidenceRecord) {
  if (tool === 'search_logs') return textField(record, 'event') ?? 'Log record'
  if (tool === 'query_metrics') {
    const metric = textField(record, 'metric') ?? ''
    return metricNames.get(metric) ?? (metric || 'Metric observation')
  }
  if (tool === 'get_traces') return 'Distributed trace'
  if (tool === 'get_service_health') {
    const endpoint = textField(record, 'endpoint')
    if (endpoint === '/health') return 'Liveness probe'
    if (endpoint === '/ready') return 'Readiness probe'
  }
  return 'Diagnostic record'
}

function EvidenceContents({ evidence }: { evidence: EvidenceRecord }) {
  const { tool, record } = evidence
  const service = textField(record, 'service')
  const traceId = textField(record, 'trace_id')
  const timestamp = textField(record, 'timestamp')
  const statusCode = numberField(record, 'status_code')
  const value = numberField(record, 'value')
  const window = numberField(record, 'window_seconds')
  const duration = numberField(record, 'duration_ms')

  if (tool === 'get_traces') return <TraceRecord fields={record} />
  if (tool === 'query_metrics') {
    return <EvidenceFields items={[
      ['Service', service],
      ['Value', value == null ? null : `${formatNumber(value)} ${textField(record, 'unit') ?? ''}`.trim()],
      ['Sample time', timestamp && <EvidenceTime value={timestamp} />],
      ['Window', window == null ? null : `${formatNumber(window)} s`],
      ['HTTP status', numberField(record, 'http_status')],
    ]} />
  }
  if (tool === 'get_service_health') {
    return <EvidenceFields items={[
      ['Service', service],
      ['Endpoint', textField(record, 'endpoint')],
      ['Result', typeof record.ok === 'boolean' ? (
        <span className={`detail-tag ${record.ok ? 'detail-tag-success' : 'detail-tag-error'}`}>
          {record.ok ? 'Healthy' : 'Unhealthy'}
        </span>
      ) : null],
      ['HTTP status', statusCode],
      ['Checked at', timestamp && <EvidenceTime value={timestamp} />],
      ['Duration', duration == null ? null : `${formatNumber(duration)} ms`],
    ]} />
  }
  if (tool === 'search_logs') {
    const level = textField(record, 'level')
    const route = [textField(record, 'method'), textField(record, 'path')].filter(Boolean).join(' ')
    const delay = numberField(record, 'delay_seconds')
    return (
      <>
        <EvidenceFields items={[
          ['Service', service],
          ['Severity', level && (
            <span className={`detail-tag ${['ERROR', 'CRITICAL'].includes(level) ? 'detail-tag-error' : ''}`}>{level}</span>
          )],
          ['Logged at', timestamp && <EvidenceTime value={timestamp} />],
          ['Route', route || null],
          ['HTTP status', statusCode],
          ['Delay', delay == null ? null : `${formatNumber(delay)} s`],
          ['Request ID', textField(record, 'request_id')],
          ['Trace ID', traceId],
          ['Span ID', textField(record, 'span_id')],
          ['Error', formatError(record.error)],
        ]} />
        <JaegerLink traceId={traceId} />
      </>
    )
  }
  return null
}

function EvidenceExplorer({ records, selected, filter, setFilter, select, heading }: {
  records: EvidenceRecord[]
  selected?: EvidenceRecord
  filter: string
  setFilter: (value: string) => void
  select: SelectEvidence
  heading: RefObject<HTMLHeadingElement | null>
}) {
  const kinds = evidenceTypes.filter(({ tool }) => records.some(record => record.tool === tool))
  const visible = filter === 'all' ? records : records.filter(record => record.tool === filter)
  return (
    <section className="panel detail-panel" aria-labelledby="evidence-explorer-heading">
      <div className="detail-panel-heading">
        <h2 id="evidence-explorer-heading" tabIndex={-1} ref={heading}>
          <Search size={16} aria-hidden="true" /> Evidence explorer
        </h2>
        <span className="detail-count">{records.length} {records.length === 1 ? 'record' : 'records'}</span>
      </div>
      {records.length === 0 ? (
        <p className="muted detail-empty">No diagnostic records were retrieved.</p>
      ) : (
        <>
          {kinds.length > 1 && (
            <div className="detail-evidence-filters" role="group" aria-label="Filter evidence by source">
              <button type="button" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All</button>
              {kinds.map(({ tool, name }) => (
                <button key={tool} type="button" aria-pressed={filter === tool} onClick={() => setFilter(tool)}>{name}</button>
              ))}
            </div>
          )}
          <div className="detail-evidence-grid">
            <ul className="detail-evidence-list" aria-label="Retrieved evidence">
              {visible.map(evidence => {
                const source = evidenceTypes.find(({ tool }) => tool === evidence.tool)
                const Icon = source?.icon ?? Terminal
                return (
                  <li key={evidence.id}>
                    <button type="button" onClick={() => select(evidence.id)} aria-pressed={selected?.id === evidence.id}>
                      <span><code>{evidence.id}</code> <Icon size={13} aria-hidden="true" /> {source?.name ?? evidence.tool}</span>
                      <strong>{evidenceLabel(evidence)}</strong>
                      {textField(evidence.record, 'service') && <small>{textField(evidence.record, 'service')}</small>}
                    </button>
                  </li>
                )
              })}
            </ul>
            {selected && (
              <article className="detail-evidence-record" aria-label={`Evidence ${selected.id}`}>
                <div className="detail-record-source">
                  <span className="detail-tag detail-tag-reference">{selected.id}</span>
                  <code>{selected.tool}</code>
                </div>
                <h3>{evidenceLabel(selected)}</h3>
                <EvidenceContents evidence={selected} />
                <details key={selected.id} className="detail-raw">
                  <summary>Raw record</summary>
                  <pre>{JSON.stringify(selected.record, null, 2)}</pre>
                </details>
              </article>
            )}
          </div>
        </>
      )}
    </section>
  )
}

function ToolTimeline({ steps, known, onSelect }: {
  steps: ToolStep[]
  known: Set<string>
  onSelect: SelectEvidence
}) {
  return (
    <section className="panel detail-panel" aria-labelledby="tool-timeline-heading">
      <div className="detail-panel-heading">
        <h2 id="tool-timeline-heading"><Terminal size={16} aria-hidden="true" /> Tool execution</h2>
        <span className="detail-count">{steps.length} {steps.length === 1 ? 'call' : 'calls'}</span>
      </div>
      {steps.length === 0 ? (
        <p className="muted detail-empty">No diagnostic calls were recorded.</p>
      ) : (
        <ol className="detail-timeline">
          {steps.map((step, index) => {
            const ids = step.evidence_ids.filter(id => known.has(id))
            const failed = step.status === 'error'
            return (
              <li key={`${step.started_at}-${index}`}>
                <details className="detail-step">
                  <summary>
                    <span className={`detail-step-icon ${failed ? 'detail-step-error' : ''}`} aria-hidden="true">
                      {failed ? <TriangleAlert size={14} /> : <Check size={14} />}
                    </span>
                    <span className="detail-step-label">
                      <code>{step.tool}</code>
                      <span>
                        {failed ? 'Failed' : 'Succeeded'} · {formatDuration(step.duration_ms)} · {ids.length} {ids.length === 1 ? 'record' : 'records'}
                      </span>
                    </span>
                    <ChevronDown className="detail-step-chevron" size={15} aria-hidden="true" />
                  </summary>
                  <div className="detail-step-body">
                    <EvidenceFields items={[
                      ['Started', <EvidenceTime value={step.started_at} />],
                      ['Evidence produced', ids.length ? <Citations ids={ids} known={known} onSelect={onSelect} /> : 'None'],
                    ]} />
                    {step.truncated && <p className="detail-warning-text">The tool returned more data than could be retained.</p>}
                    {step.error && <p className={failed ? 'detail-error-text' : 'detail-warning-text'}>{step.error}</p>}
                    <h3>Arguments</h3>
                    <pre>{JSON.stringify(step.arguments, null, 2)}</pre>
                  </div>
                </details>
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}

function FindingsPanel({ result, known, onSelect }: {
  result: InvestigationResult
  known: Set<string>
  onSelect: SelectEvidence
}) {
  return (
    <section className="panel detail-panel detail-findings" aria-labelledby="findings-heading">
      <div className="detail-panel-heading">
        <h2 id="findings-heading"><Activity size={16} aria-hidden="true" /> AI findings</h2>
        <span className="detail-count detail-confidence">{result.confidence} confidence</span>
      </div>
      {result.status !== 'completed' && (
        <div className="detail-notice">
          <TriangleAlert size={17} aria-hidden="true" />
          <div>
            <strong>{result.status === 'partial' ? 'Partial investigation' : 'Insufficient evidence'}</strong>
            <p>{result.stop_reason ? stopReasons[result.stop_reason] : 'The retrieved records did not establish the incident’s cause.'}</p>
          </div>
        </div>
      )}
      <div className="detail-cause">
        <h3>{result.suspected_root_cause ? 'Suspected root cause' : 'Cause undetermined'}</h3>
        {result.suspected_root_cause ? (
          <>
            <p>{result.suspected_root_cause}</p>
            <Citations ids={result.supporting_evidence} known={known} onSelect={onSelect} />
          </>
        ) : <p>The available evidence did not establish a cause.</p>}
      </div>
      <div className="detail-findings-section">
        <h3>Confirmed observations</h3>
        {result.confirmed_observations.length ? (
          <ul className="detail-observations">
            {result.confirmed_observations.map((finding, index) => (
              <li key={index}>
                <Check size={15} aria-hidden="true" />
                <div>
                  <p>{finding.observation}</p>
                  <Citations ids={finding.evidence_ids} known={known} onSelect={onSelect} />
                </div>
              </li>
            ))}
          </ul>
        ) : <p className="muted">No confirmed observations were recorded.</p>}
      </div>
      <div className="detail-followup">
        <div>
          <h3>Recommended next action</h3>
          <p>{result.recommended_next_action ?? 'No next action was recorded.'}</p>
        </div>
        <div>
          <h3><CircleHelp size={14} aria-hidden="true" /> Missing information</h3>
          {result.missing_information.length
            ? <ul>{result.missing_information.map((item, index) => <li key={index}>{item}</li>)}</ul>
            : <p className="muted">No gaps were reported.</p>}
        </div>
      </div>
    </section>
  )
}

function DetailWorkspace({ result }: { result: InvestigationResult }) {
  const [selectedId, select] = useState<string>()
  const [filter, setFilter] = useState('all')
  const evidenceHeading = useRef<HTMLHeadingElement>(null)
  const known = new Set(result.evidence.map(item => item.id))
  const filtered = filter === 'all' ? result.evidence : result.evidence.filter(item => item.tool === filter)
  const selected = filtered.find(item => item.id === selectedId) ?? filtered[0]

  function reveal(id: string) {
    setFilter('all')
    select(id)
    evidenceHeading.current?.focus({ preventScroll: true })
    evidenceHeading.current?.scrollIntoView?.({ block: 'start' })
  }

  return (
    <>
      <header className="detail-header">
        <div className="detail-title">
          <h1>Investigation detail</h1>
          <StatusBadge status={result.status} />
        </div>
        <dl className="detail-header-data">
          <div><dt>Investigation ID</dt><dd><CopyIdentifier label="investigation ID" value={result.id} /></dd></div>
          <div><dt>Started</dt><dd><EvidenceTime value={result.started_at} /></dd></div>
          <div><dt>Duration</dt><dd>{formatDuration(result.duration_ms)}</dd></div>
          <div><dt>Confidence</dt><dd className="detail-confidence">{result.confidence}</dd></div>
        </dl>
        {(result.incident.request_id || result.incident.trace_id) && (
          <dl className="detail-header-data detail-identifiers">
            {result.incident.request_id && (
              <div><dt>Request ID</dt><dd><CopyIdentifier label="request ID" value={result.incident.request_id} /></dd></div>
            )}
            {result.incident.trace_id && (
              <div>
                <dt>Trace ID</dt>
                <dd>
                  <CopyIdentifier label="trace ID" value={result.incident.trace_id} />
                  <JaegerLink traceId={result.incident.trace_id} />
                </dd>
              </div>
            )}
          </dl>
        )}
      </header>
      <div className="detail-layout">
        <div className="detail-column">
          <FindingsPanel result={result} known={known} onSelect={reveal} />
          <EvidenceExplorer records={result.evidence} selected={selected} filter={filter}
            setFilter={setFilter} select={select} heading={evidenceHeading} />
        </div>
        <aside className="detail-column" aria-label="Investigation activity and context">
          <ToolTimeline steps={result.timeline} known={known} onSelect={reveal} />
          <section className="panel detail-panel" aria-labelledby="incident-context-heading">
            <div className="detail-panel-heading">
              <h2 id="incident-context-heading"><FileText size={16} aria-hidden="true" /> Incident context</h2>
            </div>
            <div className="detail-context">
              {result.incident.description && <p>{result.incident.description}</p>}
              {result.incident.context && (
                <div><h3>Additional context</h3><p>{result.incident.context}</p></div>
              )}
              {!result.incident.description && !result.incident.context && (
                <p className="muted">The incident was submitted using identifiers only.</p>
              )}
              <EvidenceFields items={[['Finished', <EvidenceTime value={result.finished_at} />]]} />
            </div>
          </section>
        </aside>
      </div>
    </>
  )
}

function SavedInvestigation({ id }: { id: string }) {
  const { data: result, error, refresh } = useRequest(signal => getInvestigation(id, signal), [id])

  if (error) {
    const message = error instanceof ApiError && error.status === 404
      ? 'This investigation could not be found.'
      : 'Could not load this investigation. Check that the investigator is running.'
    return (
      <section className="panel detail-load">
        <h1>Investigation unavailable</h1>
        <p role="alert">{message}</p>
        <button type="button" className="button button-secondary" onClick={refresh}>Try again</button>
      </section>
    )
  }
  if (!result) {
    return (
      <section className="panel detail-load" aria-busy="true">
        <h1>Investigation detail</h1>
        <p className="muted" role="status">Loading investigation…</p>
      </section>
    )
  }
  return <DetailWorkspace result={result} />
}

export default function InvestigationDetail() {
  const { investigationId } = useParams()
  return (
    <div className="investigation-detail">
      <Link to="/investigations" className="detail-back">
        <ArrowLeft size={15} aria-hidden="true" /> Investigation history
      </Link>
      {investigationId && <SavedInvestigation key={investigationId} id={investigationId} />}
    </div>
  )
}
