import type { IncidentInput, InvestigationHistory, InvestigationResult, InvestigationStatus } from './types';

export const JAEGER_URL = import.meta.env.VITE_JAEGER_URL || 'http://127.0.0.1:16686';
export const PROMETHEUS_URL = import.meta.env.VITE_PROMETHEUS_URL || 'http://127.0.0.1:9090';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new ApiError(0, 'Could not reach the investigator. Check that it is running locally.');
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === 'string' ? body.detail : null;
    throw new ApiError(response.status, detail ?? `Investigator returned HTTP ${response.status}.`);
  }
  try {
    return await response.json() as T;
  } catch {
    throw new ApiError(response.status, 'The investigator returned an unreadable response.');
  }
}

export function getHealth(signal?: AbortSignal) {
  return apiRequest<{ status: 'ok' }>('/health', { signal });
}

export function getInvestigation(id: string, signal?: AbortSignal) {
  return apiRequest<InvestigationResult>(`/investigations/${encodeURIComponent(id)}`, { signal });
}

export function listInvestigations(
  { search, status, sort, offset, limit }: {
    search: string;
    status: InvestigationStatus | '';
    sort: 'newest' | 'oldest';
    offset: number;
    limit: number;
  },
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ sort, offset: String(offset), limit: String(limit) });
  if (search) params.set('search', search);
  if (status) params.set('status', status);
  return apiRequest<InvestigationHistory>(`/investigations?${params}`, { signal });
}

export function createInvestigation(incident: IncidentInput) {
  return apiRequest<InvestigationResult>('/investigations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(incident),
  });
}
