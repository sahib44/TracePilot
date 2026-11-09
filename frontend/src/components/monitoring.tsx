import { useEffect, useState } from 'react'
import { Activity, ArrowUpRight, CircleAlert, Clock3, RefreshCw } from 'lucide-react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { JAEGER_URL, PROMETHEUS_URL } from '../api/client'
import { getDashboard } from '../api/monitoring'
import type { DashboardSnapshot, HealthProbe, MetricName, ServiceName, ServiceReading, TimeWindow } from '../api/monitoring'
import { useRequest } from '../api/useRequest'
import '../styles/monitoring.css'

const SERVICES: Record<ServiceName, { label: string; color: string; dash?: string }> = {
  'game-server': { label: 'Game Server', color: 'var(--accent)' },
  'event-processor': { label: 'Event Processor', color: 'var(--chart-peer)', dash: '4 3' },
}

const METRICS: { key: MetricName; label: string; unit: string }[] = [
  { key: 'throughput', label: 'Request throughput', unit: 'req/s' },
  { key: 'error_rate', label: 'HTTP 5xx rate', unit: 'errors/s' },
  { key: 'latency', label: 'P95 request latency', unit: 'ms' },
]

const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 })
const minuteFormat = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const timeFormat = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })

export function serviceLabel(service: ServiceName) {
  return SERVICES[service].label
}

function displayValue(value: number) {
  return value > 0 && value < 0.001 ? '<0.001' : numberFormat.format(value)
}

function toDisplayUnits(value: number, metric: MetricName) {
  return metric === 'latency' ? value * 1000 : value
}

export function useMonitoring() {
  const [window, setWindow] = useState<TimeWindow>(15)
  const request = useRequest((signal) => getDashboard(window, signal), [window])

  useEffect(() => {
    const id = globalThis.setInterval(() => {
      if (document.visibilityState === 'visible' && !request.loading) request.refresh()
    }, 30_000)
    return () => globalThis.clearInterval(id)
  }, [request.loading, request.refresh])

  return {
    ...request,
    data: request.data?.window_minutes === window ? request.data : undefined,
    window,
    setWindow,
  }
}

export function MonitoringControls({ window, setWindow, loading, refresh, observedAt }: {
  window: TimeWindow
  setWindow: (window: TimeWindow) => void
  loading: boolean
  refresh: () => void
  observedAt?: string
}) {
  return (
    <div className="monitor-controls">
      {observedAt && <span className="monitor-updated" title={new Date(observedAt).toLocaleString()}>
        {loading ? 'Refreshing…' : <>Updated <time dateTime={observedAt}>{timeFormat.format(new Date(observedAt))}</time></>}
      </span>}
      <div className="monitor-window" role="group" aria-label="Chart time range">
        <Clock3 aria-hidden="true" size={14} />
        <button type="button" aria-pressed={window === 15} aria-label="Last 15 minutes" onClick={() => setWindow(15)}>15m</button>
        <button type="button" aria-pressed={window === 60} aria-label="Last 60 minutes" onClick={() => setWindow(60)}>1h</button>
      </div>
      <button type="button" className="button button-secondary monitor-refresh" onClick={refresh} disabled={loading}>
        <RefreshCw aria-hidden="true" size={14} />
        Refresh
      </button>
    </div>
  )
}

export function MonitoringNotice({ title, detail }: { title: string; detail: string }) {
  return <div className="monitor-notice" role="status">
    <CircleAlert aria-hidden="true" size={17} />
    <div><strong>{title}</strong><p>{detail}</p></div>
  </div>
}

export function MonitoringError({ error, hasData }: { error: Error; hasData: boolean }) {
  return <MonitoringNotice title={hasData ? 'Dashboard refresh failed' : 'Dashboard unavailable'}
    detail={hasData ? `${error.message} Showing the last successful refresh.` : error.message} />
}

export function MonitoringLoading() {
  return <div className="panel monitor-loading" role="status" aria-busy="true">
    <Activity aria-hidden="true" size={18} />
    Loading service health and request telemetry…
  </div>
}

export function ProbeState({ probe, kind }: { probe: HealthProbe | null; kind: 'liveness' | 'readiness' }) {
  const tone = probe ? (probe.ok ? 'ok' : 'failed') : 'unknown'
  const positive = kind === 'liveness' ? 'Live' : 'Ready'
  const negative = kind === 'liveness' ? 'Down' : 'Not ready'
  return <span className={`monitor-probe monitor-probe--${tone}`}>
    <span className="monitor-dot" aria-hidden="true" />
    {probe ? (probe.ok ? positive : negative) : 'Unavailable'}
  </span>
}

export function ProbeDetail({ probe, kind }: { probe: HealthProbe | null; kind: 'liveness' | 'readiness' }) {
  const endpoint = kind === 'liveness' ? '/health' : '/ready'
  return (
    <div className="probe-detail">
      <div className="probe-detail-head">
        <span>{kind === 'liveness' ? 'Liveness' : 'Readiness'}</span>
        <ProbeState probe={probe} kind={kind} />
      </div>
      <div className="probe-detail-meta">
        <code>{endpoint}</code>
        {probe ? <>
          <span>HTTP {probe.status_code}</span>
          <span>{probe.duration_ms} ms</span>
          <time dateTime={probe.timestamp} title={new Date(probe.timestamp).toLocaleString()}>{timeFormat.format(new Date(probe.timestamp))}</time>
        </> : <span>No probe response</span>}
      </div>
    </div>
  )
}

export function MonitoringLinks() {
  return <div className="monitor-links">
    <a className="button button-secondary" href={PROMETHEUS_URL} target="_blank" rel="noreferrer">Prometheus <ArrowUpRight aria-hidden="true" size={14} /></a>
    <a className="button button-secondary" href={JAEGER_URL} target="_blank" rel="noreferrer">Jaeger <ArrowUpRight aria-hidden="true" size={14} /></a>
  </div>
}

type ChartPoint = { timestamp: number } & Partial<Record<ServiceName, number | null>>

function chartPoints(services: ServiceReading[], metric: MetricName) {
  const byTime = new Map<number, ChartPoint>()
  for (const service of services) {
    for (const sample of service.metrics[metric].points) {
      const point = byTime.get(sample.timestamp) ?? { timestamp: sample.timestamp }
      point[service.service] = sample.value === null ? null : toDisplayUnits(sample.value, metric)
      byTime.set(sample.timestamp, point)
    }
  }
  return [...byTime.values()].sort((a, b) => a.timestamp - b.timestamp)
}

function MetricChart({ metric, services, observedAt, window }: {
  metric: typeof METRICS[number]
  services: ServiceReading[]
  observedAt: string
  window: TimeWindow
}) {
  const points = chartPoints(services, metric.key)
  if (!points.length) return <div className="monitor-chart-empty">No samples in this window</div>

  const end = Date.parse(observedAt) / 1000
  const timeLabel = (value: number) => minuteFormat.format(new Date(value * 1000))

  return (
    <div className="monitor-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 12, left: 0, bottom: 0 }} aria-label={`${metric.label} in ${metric.unit} over the last ${window} minutes`}>
          <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 4" />
          <XAxis dataKey="timestamp" type="number" domain={[end - window * 60, end]} tickCount={4} tickFormatter={timeLabel} tickLine={false} axisLine={false} tick={{ fill: 'var(--muted)', fontSize: 10 }} minTickGap={28} />
          <YAxis width={43} domain={[0, 'auto']} tickCount={4} tickFormatter={displayValue} tickLine={false} axisLine={false} tick={{ fill: 'var(--muted)', fontSize: 10 }} />
          <Tooltip
            labelFormatter={(value) => timeFormat.format(new Date(Number(value) * 1000))}
            formatter={(value) => `${displayValue(Number(value))} ${metric.unit}`}
            contentStyle={{ background: 'var(--surface-raised)', borderColor: 'var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 12 }}
            labelStyle={{ color: 'var(--muted)', marginBottom: 4 }}
          />
          {services.map(({ service }) => <Line key={service} dataKey={service} name={serviceLabel(service)} type="linear"
            stroke={SERVICES[service].color} strokeDasharray={SERVICES[service].dash} strokeWidth={1.7} isAnimationActive={false} connectNulls={false}
            dot={points.filter((point) => typeof point[service] === 'number').length === 1 ? { r: 2 } : false}
            activeDot={{ r: 3 }} />)}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function MetricPanels({ snapshot, services = snapshot.services }: { snapshot: DashboardSnapshot; services?: ServiceReading[] }) {
  return (
    <div className="monitor-metric-grid">
      {METRICS.map((metric) => <article className="monitor-metric" key={metric.key}>
        <h3>{metric.label}</h3>
        <dl className="monitor-metric-values">
          {services.map((service) => {
            const value = service.metrics[metric.key].value
            const series = SERVICES[service.service]
            return <div key={service.service}>
              <dt><span className="monitor-series-key" aria-hidden="true"
                style={{ color: series.color, borderTopStyle: series.dash ? 'dashed' : 'solid' }} />{series.label}</dt>
              <dd>{value === null ? <span className="monitor-no-data">No data</span> : <>
                {displayValue(toDisplayUnits(value, metric.key))} <span className="monitor-unit">{metric.unit}</span>
              </>}</dd>
            </div>
          })}
        </dl>
        {snapshot.metric_errors[metric.key] ? <div className="monitor-chart-empty monitor-chart-error" role="status">
          <CircleAlert aria-hidden="true" size={15} />{snapshot.metric_errors[metric.key]}
        </div> : <MetricChart metric={metric} services={services} observedAt={snapshot.observed_at} window={snapshot.window_minutes} />}
      </article>)}
    </div>
  )
}
