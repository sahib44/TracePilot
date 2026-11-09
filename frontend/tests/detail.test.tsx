import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { InvestigationResult } from '../src/api/types'
import InvestigationDetail from '../src/pages/InvestigationDetail'

const id = '7bccd8e5-41f5-45fa-a55d-521e5aece811'
const time = '2026-11-09T17:15:22Z'
const traceId = '7a577ea063684d31bf6a4896ca16aa15'
const requestId = 'dashboard-detail-test'
const unsafeLog = '<img src=x onerror=alert(1)> Failed to persist gameplay event'

const investigation: InvestigationResult = {
  id,
  incident: { description: 'A client reported a failed event submission', request_id: requestId, trace_id: null, context: 'Local smoke environment' },
  status: 'completed',
  started_at: time,
  finished_at: '2026-11-09T17:15:24Z',
  duration_ms: 2000,
  confidence: 'high',
  suspected_root_cause: 'PostgreSQL rejected the query',
  confirmed_observations: [{ observation: 'The event processor returned HTTP 503', evidence_ids: ['E1', 'E9'] }],
  supporting_evidence: ['E2'],
  missing_information: ['Database query source'],
  recommended_next_action: 'Inspect the failing query',
  stop_reason: null,
  timeline: [{
    tool: 'search_logs',
    arguments: { request_id: requestId },
    started_at: time,
    duration_ms: 122,
    status: 'ok',
    evidence_ids: ['E1'],
    error: null,
    truncated: true,
  }, {
    tool: 'query_metrics',
    arguments: { metric: 'latency' },
    started_at: time,
    duration_ms: 240,
    status: 'error',
    evidence_ids: [],
    error: 'Prometheus is unavailable.',
    truncated: false,
  }],
  evidence: [{
    id: 'E1',
    tool: 'search_logs',
    record: { service: 'event-processor', event: unsafeLog, status_code: 503, trace_id: traceId, level: 'ERROR', timestamp: time },
  }, {
    id: 'E2',
    tool: 'get_traces',
    record: {
      trace_id: traceId, started_at: time, duration_ms: 33.25, span_count: 2,
      spans: [{
        service: 'game-server', operation: 'POST /events', span_id: 'aabbccddeeff0033',
        start_offset_ms: 0, duration_ms: 33.25, status: 'error',
      }, {
        service: 'event-processor', operation: 'SELECT game_events', span_id: 'aabbccddeeff0055',
        parent_span_id: 'aabbccddeeff0033', start_offset_ms: 7, duration_ms: 2.5, status: 'error',
        error_type: 'DivisionByZero', sqlstate: '22012',
      }],
    },
  }, {
    id: 'E3',
    tool: 'query_metrics',
    record: { service: 'event-processor', metric: 'error_rate', value: 0.2, unit: 'errors/second', timestamp: time, window_seconds: 300 },
  }, {
    id: 'E4',
    tool: 'get_service_health',
    record: { service: 'game-server', endpoint: '/ready', status_code: 200, ok: true, duration_ms: 9, timestamp: time },
  }],
}

function response(result: InvestigationResult) {
  return new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } })
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={[`/investigations/${id}`]}>
      <Routes>
        <Route path="/investigations/:investigationId" element={<InvestigationDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Investigation detail', () => {
  it('loads the saved result and follows real citations to their trace and log records', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(investigation))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const { container } = renderDetail()

    expect(screen.getByText('Loading investigation…')).toBeVisible()
    expect(await screen.findByText('PostgreSQL rejected the query')).toBeVisible()
    expect(fetchMock.mock.calls[0][0]).toMatch(new RegExp(`/investigations/${id}$`))
    expect(screen.getByText(requestId)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Copy request ID' }))
    expect(await navigator.clipboard.readText()).toBe(requestId)
    expect(screen.getByText('The event processor returned HTTP 503')).toBeVisible()
    expect(screen.getByText('Inspect the failing query')).toBeVisible()
    expect(screen.getByText('Database query source')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Inspect evidence E9' })).not.toBeInTheDocument()

    const log = screen.getByRole('article', { name: 'Evidence E1' })
    expect(within(log).getByText(unsafeLog)).toBeVisible()
    expect(container.querySelector('img')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Inspect evidence E2' }))
    expect(screen.getByRole('heading', { name: 'Evidence explorer' })).toHaveFocus()

    const trace = screen.getByRole('article', { name: 'Evidence E2' })
    expect(within(trace).getByText('get_traces')).toBeVisible()
    expect(within(trace).getByText('SELECT game_events')).toBeVisible()
    expect(within(trace).getByText('DivisionByZero')).toBeVisible()
    expect(within(trace).getByText('SQLSTATE 22012')).toBeVisible()
    expect(within(trace).getByText('2.5 ms duration')).toBeVisible()
    expect(within(trace).getByText('Parent')).toHaveTextContent('aabbccddeeff0033')
    expect(within(trace).getByRole('link', { name: 'Open in Jaeger' })).toHaveAttribute('href', expect.stringMatching(`/trace/${traceId}$`))
  })

  it('filters structured evidence and expands tool execution with arguments, errors and truncation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(investigation)))
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { name: 'Evidence explorer' })

    await user.click(screen.getByRole('button', { name: 'Metrics' }))
    const metric = screen.getByRole('article', { name: 'Evidence E3' })
    expect(within(metric).getByText('HTTP 5xx rate')).toBeVisible()
    expect(within(metric).getByText('0.2 errors/second')).toBeVisible()
    expect(within(metric).getByText('300 s')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Health' }))
    const health = screen.getByRole('article', { name: 'Evidence E4' })
    expect(within(health).getByText('Readiness probe')).toBeVisible()
    expect(within(health).getByText('/ready')).toBeVisible()
    expect(within(health).getByText('Healthy')).toBeVisible()

    const timeline = screen.getByRole('region', { name: 'Tool execution' })
    const logsTool = within(timeline).getByText('search_logs')
    const logStep = within(logsTool.closest('details')!)
    expect(logStep.getByText('Arguments')).not.toBeVisible()
    await user.click(logsTool)
    expect(logStep.getByText('Arguments')).toBeVisible()
    expect(logStep.getByText(/"request_id": "dashboard-detail-test"/)).toBeVisible()
    expect(logStep.getByText('The tool returned more data than could be retained.')).toBeVisible()
    await user.click(logStep.getByRole('button', { name: 'Inspect evidence E1' }))
    expect(screen.getByRole('article', { name: 'Evidence E1' })).toBeVisible()
    await user.click(within(timeline).getByText('query_metrics'))
    expect(within(timeline).getByText('Prometheus is unavailable.')).toBeVisible()
  })

  it('shows partial findings and missing information when the cause is undetermined', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({
      ...investigation,
      status: 'partial',
      confidence: 'undetermined',
      suspected_root_cause: null,
      supporting_evidence: [],
      confirmed_observations: [],
      timeline: [],
      evidence: [],
      stop_reason: 'model_unavailable',
      missing_information: ['The incident needs matching trace records'],
    })))
    renderDetail()
    expect(await screen.findByRole('heading', { name: 'Cause undetermined' })).toBeVisible()
    expect(screen.getByText('Partial investigation')).toBeVisible()
    expect(screen.getByText(/The local model was unavailable/)).toBeVisible()
    expect(screen.getByText('The incident needs matching trace records')).toBeVisible()
    expect(screen.getByText('No diagnostic records were retrieved.')).toBeVisible()
    expect(screen.getByText('No diagnostic calls were recorded.')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Inspect evidence/ })).not.toBeInTheDocument()
  })
})
