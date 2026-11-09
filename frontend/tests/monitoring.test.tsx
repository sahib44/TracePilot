import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { cloneElement, type ReactElement } from 'react'
import { MemoryRouter } from 'react-router'
import { expect, test, vi } from 'vitest'
import type { DashboardSnapshot, ServiceReading } from '../src/api/monitoring'
import Overview from '../src/pages/Overview'
import Services from '../src/pages/Services'

vi.mock('recharts', async (importOriginal) => {
  const recharts = await importOriginal<typeof import('recharts')>()
  return {
    ...recharts,
    ResponsiveContainer: ({ children }: { children: ReactElement<{ width: number; height: number }> }) =>
      cloneElement(children, { width: 320, height: 132 }),
  }
})

const now = '2026-11-08T20:30:00Z'
const investigationId = '3e7fd079-9d04-486e-a105-67719d405eef'

function service(name: ServiceReading['service']): ServiceReading {
  return {
    service: name,
    liveness: { service: name, endpoint: '/health', status_code: 200, ok: true, timestamp: now, duration_ms: 3 },
    readiness: { service: name, endpoint: '/ready', status_code: 200, ok: true, timestamp: now, duration_ms: 6 },
    metrics: {
      throughput: { value: null, points: [] },
      error_rate: { value: null, points: [] },
      latency: { value: null, points: [] },
    },
  }
}

function snapshot(overrides: Partial<DashboardSnapshot> = {}): DashboardSnapshot {
  return {
    window_minutes: 15,
    step_seconds: 15,
    observed_at: now,
    services: [service('game-server'), service('event-processor')],
    metric_errors: {},
    health_error: null,
    recent_investigations: [],
    recent_error: null,
    ...overrides,
  }
}

test('shows loading, explicit missing samples, and an empty history action', async () => {
  let respond!: (response: Response) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { respond = resolve })))
  render(<MemoryRouter><Overview /></MemoryRouter>)
  expect(screen.getByRole('status')).toHaveTextContent('Loading service health')

  respond(Response.json(snapshot()))
  expect(await screen.findByText('2 of 2 services ready')).toBeVisible()
  expect(screen.getAllByText('No data')).toHaveLength(6)
  expect(screen.getAllByText('No samples in this window')).toHaveLength(3)
  expect(screen.getByText('No saved investigations')).toBeVisible()
  expect(screen.getByRole('link', { name: 'New investigation' })).toHaveAttribute('href', '/investigations/new')
})

test('renders saved investigations and metric units, then fetches and refreshes the selected chart window', async () => {
  const game = service('game-server')
  const sampleTime = Date.parse(now) / 1000
  game.metrics = {
    throughput: { value: 12.5, points: [{ timestamp: sampleTime - 15, value: 11 }, { timestamp: sampleTime, value: 12.5 }] },
    error_rate: { value: 0, points: [{ timestamp: sampleTime, value: 0 }] },
    latency: { value: 0.025, points: [{ timestamp: sampleTime, value: 0.025 }] },
  }
  const initial = snapshot({
    services: [game, service('event-processor')],
    recent_investigations: [{
      id: investigationId, incident: { description: 'Event failed to persist' },
      status: 'partial', started_at: now, suspected_root_cause: null,
      confidence: 'undetermined', duration_ms: 1900,
    }],
  })
  const fetcher = vi.fn((url: string) => Promise.resolve(Response.json(
    url.endsWith('window=60') ? { ...initial, window_minutes: 60, step_seconds: 60 } : initial,
  )))
  vi.stubGlobal('fetch', fetcher)
  const user = userEvent.setup()
  render(<MemoryRouter><Overview /></MemoryRouter>)

  expect(await screen.findByRole('link', { name: `Open investigation ${investigationId}` })).toHaveAttribute('href', `/investigations/${investigationId}`)
  expect(screen.getByText('Event failed to persist')).toBeVisible()
  expect(screen.getByText('12.5')).toHaveTextContent('12.5 req/s')
  expect(screen.getByText('errors/s').parentElement).toHaveTextContent('0 errors/s')
  expect(screen.getByText('25')).toHaveTextContent('25 ms')
  expect(screen.getByRole('application', { name: 'Request throughput in req/s over the last 15 minutes' })).toBeVisible()

  await user.click(screen.getByRole('button', { name: 'Last 60 minutes' }))
  expect(await screen.findByText('Last 60 minutes')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Last 60 minutes' })).toHaveAttribute('aria-pressed', 'true')
  await user.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
    '/api/dashboard?window=15', '/api/dashboard?window=60', '/api/dashboard?window=60',
  ]))
})

test('shows per-service readiness and liveness when Prometheus is unavailable', async () => {
  const processor = service('event-processor')
  processor.readiness = { ...processor.readiness!, status_code: 503, ok: false }
  const failedMetrics = { throughput: 'Prometheus is unavailable.', error_rate: 'Prometheus is unavailable.', latency: 'Prometheus is unavailable.' }
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(snapshot({
    services: [service('game-server'), processor],
    metric_errors: failedMetrics,
  }))))
  render(<Services />)

  const section = await screen.findByRole('region', { name: 'Event Processor' })
  expect(within(section).getByText('Live')).toBeVisible()
  expect(within(section).getByText('Not ready')).toBeVisible()
  expect(within(section).getByText('HTTP 503')).toBeVisible()
  expect(within(section).getAllByText('Prometheus is unavailable.')).toHaveLength(3)
  expect(screen.getByRole('link', { name: 'Jaeger' })).toHaveAttribute('target', '_blank')
})

test('recovers from a disconnected investigator after a manual refresh', async () => {
  const fetcher = vi.fn()
    .mockRejectedValueOnce(new TypeError('Failed to fetch'))
    .mockResolvedValueOnce(Response.json(snapshot()))
  vi.stubGlobal('fetch', fetcher)
  const user = userEvent.setup()
  render(<MemoryRouter><Overview /></MemoryRouter>)

  expect(await screen.findByText('Dashboard unavailable')).toBeVisible()
  expect(screen.getByText(/Could not reach the investigator/)).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByText('2 of 2 services ready')).toBeVisible()
  expect(screen.queryByText('Dashboard unavailable')).not.toBeInTheDocument()
})

test('distinguishes an unverified service from failed readiness and marks stale data after a refresh failure', async () => {
  const processor = service('event-processor')
  processor.readiness = null
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json(snapshot({
      services: [service('game-server'), processor],
      health_error: 'event-processor /ready could not be reached',
    })))
    .mockRejectedValueOnce(new TypeError('Failed to fetch'))
  vi.stubGlobal('fetch', fetcher)
  const user = userEvent.setup()
  render(<MemoryRouter><Overview /></MemoryRouter>)

  expect(await screen.findByText('1 of 2 services ready · 1 unverified')).toBeVisible()
  expect(screen.getByText('Service checks incomplete')).toBeVisible()
  expect(screen.queryByText('Not ready')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByText('Dashboard refresh failed')).toBeVisible()
  expect(screen.getByText(/Showing the last successful refresh/)).toBeVisible()
  expect(screen.getByText('1 of 2 services ready · 1 unverified')).toBeVisible()
})
