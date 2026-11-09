import { ArrowUpRight } from 'lucide-react';
import { Link } from 'react-router';
import type { InvestigationSummary } from '../api/types';
import { formatDate, formatDuration } from './format';
import { StatusBadge } from './StatusBadge';

export function InvestigationTable({ investigations }: { investigations: InvestigationSummary[] }) {
  return (
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Investigation results">
      <table className="investigation-table">
        <thead><tr>
          <th scope="col">Investigation / context</th>
          <th scope="col">Started</th>
          <th scope="col">Status</th>
          <th scope="col">Suspected cause</th>
          <th scope="col">Confidence</th>
          <th scope="col">Duration</th>
        </tr></thead>
        <tbody>
          {investigations.map(({ id, incident, status, started_at, suspected_root_cause, confidence, duration_ms }) => (
            <tr key={id}>
              <td>
                <Link className="table-id" to={`/investigations/${id}`} title={id} aria-label={`Open investigation ${id}`}>
                  {id.slice(0, 8)} <ArrowUpRight size={12} aria-hidden="true" />
                </Link>
                <span className="table-context" title={incident.description ?? incident.context ?? undefined}>
                  {incident.description ?? incident.context ?? incident.request_id ?? incident.trace_id ?? '—'}
                </span>
              </td>
              <td className="nowrap"><time dateTime={started_at} title={started_at}>{formatDate(started_at)}</time></td>
              <td><StatusBadge status={status} /></td>
              <td><span className="table-cause" title={suspected_root_cause ?? undefined}>{suspected_root_cause ?? 'Undetermined'}</span></td>
              <td><span className="capitalize">{confidence}</span></td>
              <td className="numeric nowrap">{formatDuration(duration_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
