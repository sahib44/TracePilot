import type { InvestigationStatus } from '../api/types';

const statuses: Record<InvestigationStatus, { label: string; tone: string }> = {
  completed: { label: 'Completed', tone: 'success' },
  partial: { label: 'Partial', tone: 'warning' },
  insufficient_evidence: { label: 'Insufficient evidence', tone: 'neutral' },
};

export function StatusBadge({ status }: { status: InvestigationStatus }) {
  const { label, tone } = statuses[status];
  return <span className={`status-badge tone-${tone}`}><span className="status-dot" aria-hidden="true" />{label}</span>;
}
