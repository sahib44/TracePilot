import { ArrowLeft, ArrowRight, Clock3, Fingerprint, ScanSearch } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router';
import { ApiError, createInvestigation } from '../api/client';
import type { IncidentInput } from '../api/types';
import { formatDuration } from '../components/format';

type Draft = Required<Record<keyof IncidentInput, string>>;
type FieldErrors = Partial<Record<keyof Draft, string>>;

export const emptyDraft: Draft = { description: '', request_id: '', trace_id: '', context: '' };

export function useInvestigationSubmission() {
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [savedId, setSavedId] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const inFlight = useRef(false);

  async function submit(incident: IncidentInput) {
    if (inFlight.current) return;
    inFlight.current = true;
    setStartedAt(Date.now());
    setSavedId(null);
    setError(null);
    try {
      const result = await createInvestigation(incident);
      setSavedId(result.id);
      setDraft(emptyDraft);
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error('The investigation request failed.'));
    } finally {
      inFlight.current = false;
      setStartedAt(null);
    }
  }
  return { draft, setDraft, startedAt, savedId, setSavedId, error, submit };
}

function validateDraft(draft: Draft) {
  const description = draft.description.trim();
  const context = draft.context.trim();
  const { request_id, trace_id } = draft;
  const errors: FieldErrors = {};
  if (request_id && !/^[!-~]{1,128}$/.test(request_id)) {
    errors.request_id = 'Use 1–128 printable ASCII characters, with no spaces.';
  }
  if (trace_id && (!/^[a-fA-F0-9]{32}$/.test(trace_id) || /^0{32}$/.test(trace_id))) {
    errors.trace_id = 'Enter a 32-character hexadecimal trace ID with at least one nonzero character.';
  }
  for (const [field, value] of [['description', description], ['context', context]] as const) {
    if ([...value].length > 1500 || /[\0\uD800-\uDFFF]/u.test(value)) {
      errors[field] = 'Use up to 1,500 characters without invalid text.';
    }
  }
  const incident: IncidentInput = {
    ...(description && { description }),
    ...(request_id && { request_id }),
    ...(trace_id && { trace_id: trace_id.toLowerCase() }),
    ...(context && { context }),
  };
  return { incident, errors };
}

function submissionMessage(error: Error) {
  if (error instanceof ApiError && error.status === 429) {
    return 'Another investigation is already running. Wait for it to finish before trying again.';
  }
  if (!(error instanceof ApiError) || error.status === 0 || error.status >= 500) {
    return `${error.message} Check saved investigations before retrying. The server may have received your request.`;
  }
  return error.message;
}

export default function NewInvestigation({ workflow }: { workflow: ReturnType<typeof useInvestigationSubmission> }) {
  const { draft, setDraft, startedAt, error, submit } = workflow;
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [empty, setEmpty] = useState(false);
  const [now, setNow] = useState(Date.now);
  const running = startedAt !== null;

  useEffect(() => {
    if (startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  function change(field: keyof Draft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setFieldErrors((current) => ({ ...current, [field]: undefined }));
    setEmpty(false);
  }

  function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const { incident, errors } = validateDraft(draft);
    const noInput = Object.keys(incident).length === 0;
    setFieldErrors(errors);
    setEmpty(noInput);
    if (noInput || Object.keys(errors).length) return;
    void submit(incident);
  }

  return (
    <div className="new-workspace">
      <Link className="back-link" to="/investigations"><ArrowLeft size={15} aria-hidden="true" /> Investigations</Link>
      <header className="page-heading">
        <div>
          <p className="eyebrow">Investigation workspace</p>
          <h1>New investigation</h1>
          <p>Give TracePilot a starting point to look up the incident in local telemetry.</p>
        </div>
      </header>
      <form className="panel investigation-form" onSubmit={send} noValidate aria-busy={running}>
        <div className="form-heading">
          <span className="form-heading-icon"><ScanSearch size={20} aria-hidden="true" /></span>
          <div><h2>Incident input</h2><p>Provide at least one field. A request or trace ID can help find related evidence.</p></div>
        </div>
        <fieldset className="form-fields" disabled={running}>
          <label className="form-field">
            <span>Incident description</span>
            <textarea rows={4} value={draft.description} onChange={(event) => change('description', event.target.value)}
              placeholder="What failed, and when did you notice it?" aria-invalid={Boolean(fieldErrors.description)} aria-describedby={fieldErrors.description ? 'description-error' : undefined} />
            {fieldErrors.description && <span className="field-error" id="description-error">{fieldErrors.description}</span>}
          </label>
          <div className="form-grid">
            <label className="form-field">
              <span><Fingerprint size={14} aria-hidden="true" /> Request ID</span>
              <input className="mono" value={draft.request_id} onChange={(event) => change('request_id', event.target.value)} spellCheck={false}
                autoComplete="off" aria-invalid={Boolean(fieldErrors.request_id)} aria-describedby={fieldErrors.request_id ? 'request-error' : undefined} />
              {fieldErrors.request_id && <span className="field-error" id="request-error">{fieldErrors.request_id}</span>}
            </label>
            <label className="form-field">
              <span><Fingerprint size={14} aria-hidden="true" /> Trace ID</span>
              <input className="mono" value={draft.trace_id} onChange={(event) => change('trace_id', event.target.value)} spellCheck={false}
                autoComplete="off" aria-invalid={Boolean(fieldErrors.trace_id)} aria-describedby={fieldErrors.trace_id ? 'trace-error' : undefined} />
              {fieldErrors.trace_id && <span className="field-error" id="trace-error">{fieldErrors.trace_id}</span>}
            </label>
          </div>
          <label className="form-field">
            <span>Additional context <small>Optional</small></span>
            <textarea rows={3} value={draft.context} onChange={(event) => change('context', event.target.value)}
              placeholder="Add a service name, time window, or relevant change." aria-invalid={Boolean(fieldErrors.context)} aria-describedby={fieldErrors.context ? 'context-error' : undefined} />
            {fieldErrors.context && <span className="field-error" id="context-error">{fieldErrors.context}</span>}
          </label>
        </fieldset>
        {empty && <p className="inline-notice tone-danger" role="alert">Provide an incident description, request ID, trace ID, or context.</p>}
        {error && !running && (
          <div className="inline-notice tone-danger" role="alert">
            {submissionMessage(error)} <Link to="/investigations">View saved investigations</Link>
          </div>
        )}
        {running && (
          <div className="inline-notice tone-accent running-note">
            <Clock3 size={17} aria-hidden="true" />
            <div>
              <strong role="status">Investigation running</strong>
              <p>The local model can take up to 180 seconds with the default settings. You can browse other pages while it runs.</p>
            </div>
            <span className="mono elapsed" aria-label="Elapsed time">{formatDuration(Math.max(0, now - startedAt))}</span>
          </div>
        )}
        <div className="form-footer">
          <span>Results are saved when the investigator finishes.</span>
          <button type="submit" className="button button-primary" disabled={running}>
            {running ? 'Investigation running…' : 'Start investigation'} <ArrowRight size={16} aria-hidden="true" />
          </button>
        </div>
      </form>
    </div>
  );
}
