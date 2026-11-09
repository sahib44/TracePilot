export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonRecord = { [key: string]: JsonValue };

export interface IncidentInput {
  description?: string | null;
  request_id?: string | null;
  trace_id?: string | null;
  context?: string | null;
}

export type InvestigationStatus = 'completed' | 'insufficient_evidence' | 'partial';
export type Confidence = 'high' | 'medium' | 'low' | 'undetermined';

export interface InvestigationSummary {
  id: string;
  incident: IncidentInput;
  status: InvestigationStatus;
  started_at: string;
  suspected_root_cause: string | null;
  confidence: Confidence;
  duration_ms: number;
}

export interface InvestigationHistory {
  items: InvestigationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvidenceRecord {
  id: string;
  tool: string;
  record: JsonRecord;
}

export interface ToolStep {
  tool: string;
  arguments: JsonRecord;
  started_at: string;
  duration_ms: number;
  status: 'ok' | 'error';
  evidence_ids: string[];
  error: string | null;
  truncated: boolean;
}

export interface InvestigationResult extends InvestigationSummary {
  finished_at: string;
  confirmed_observations: { observation: string; evidence_ids: string[] }[];
  supporting_evidence: string[];
  missing_information: string[];
  recommended_next_action: string | null;
  stop_reason: 'timeout' | 'tool_call_limit' | 'model_unavailable' | 'malformed_model_response' | 'invalid_evidence' | null;
  timeline: ToolStep[];
  evidence: EvidenceRecord[];
}
