import { apiRequest } from './client'
import type { InvestigationSummary } from './types'

export type TimeWindow = 15 | 60
export type ServiceName = 'game-server' | 'event-processor'
export type MetricName = 'throughput' | 'error_rate' | 'latency'

export interface HealthProbe {
  service: ServiceName
  endpoint: '/health' | '/ready'
  status_code: number
  ok: boolean
  timestamp: string
  duration_ms: number
}

export interface MetricReading {
  value: number | null
  points: { timestamp: number; value: number | null }[]
}

export interface ServiceReading {
  service: ServiceName
  liveness: HealthProbe | null
  readiness: HealthProbe | null
  metrics: Record<MetricName, MetricReading>
}

export interface DashboardSnapshot {
  window_minutes: TimeWindow
  step_seconds: number
  observed_at: string
  services: ServiceReading[]
  metric_errors: Partial<Record<MetricName, string>>
  health_error: string | null
  recent_investigations: InvestigationSummary[]
  recent_error: string | null
}

export function getDashboard(window: TimeWindow, signal?: AbortSignal) {
  return apiRequest<DashboardSnapshot>(`/dashboard?window=${window}`, { signal })
}
