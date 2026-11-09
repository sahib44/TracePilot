import { ChevronLeft, ChevronRight, ListFilter, Plus, RefreshCw, Search } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { listInvestigations } from '../api/client';
import type { InvestigationStatus } from '../api/types';
import { useRequest } from '../api/useRequest';
import { InvestigationTable } from '../components/InvestigationTable';

const PAGE_SIZE = 20;
const MAX_OFFSET = 10_000;
const statuses: InvestigationStatus[] = ['completed', 'partial', 'insufficient_evidence'];

export default function Investigations() {
  const [params, setParams] = useSearchParams();
  const search = (params.get('search') ?? '').slice(0, 128);
  const candidate = params.get('status') as InvestigationStatus;
  const status = statuses.includes(candidate) ? candidate : '';
  const sort = params.get('sort') === 'oldest' ? 'oldest' : 'newest';
  const offsetParam = Number(params.get('offset'));
  const offset = Number.isSafeInteger(offsetParam) && offsetParam > 0 ? Math.min(offsetParam, MAX_OFFSET) : 0;
  const { data, error, loading, refresh } = useRequest(
    (signal) => listInvestigations({ search, status, sort, offset, limit: PAGE_SIZE }, signal),
    [search, status, sort, offset],
  );

  function updateFilter(key: string, value: string) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set(key, value);
      else next.delete(key);
      next.delete('offset');
      return next;
    });
  }

  function changePage(nextOffset: number) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (nextOffset) next.set('offset', String(nextOffset));
      else next.delete('offset');
      return next;
    });
  }

  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">Investigation workspace</p>
          <h1>Investigations</h1>
          <p>Review saved results and follow findings back to their evidence.</p>
        </div>
        <button className="button button-secondary" type="button" onClick={refresh} disabled={loading}>
          <RefreshCw size={15} aria-hidden="true" /> Refresh
        </button>
      </header>
      <section className="panel history-panel" aria-label="Investigation history">
        <div className="history-toolbar">
          <HistorySearch key={search} search={search} onSearch={(value) => updateFilter('search', value)} />
          <label className="filter-control">
            <ListFilter size={14} aria-hidden="true" />
            <span className="sr-only">Filter by status</span>
            <select value={status} onChange={(event) => updateFilter('status', event.target.value)}>
              <option value="">All statuses</option>
              <option value="completed">Completed</option>
              <option value="partial">Partial</option>
              <option value="insufficient_evidence">Insufficient evidence</option>
            </select>
          </label>
          <label className="filter-control">
            <span className="sr-only">Sort by time</span>
            <select value={sort} onChange={(event) => updateFilter('sort', event.target.value === 'newest' ? '' : 'oldest')}>
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
            </select>
          </label>
        </div>
        {loading ? <div className="loading-block" role="status">Loading investigations…</div> : error ? (
          <div className="empty-block" role="alert">
            <h2>Investigation history unavailable</h2>
            <p>{error.message}</p>
            <button className="button button-secondary" type="button" onClick={refresh}>Try again</button>
          </div>
        ) : data && data.items.length > 0 ? (
          <InvestigationTable investigations={data.items} />
        ) : (
          <div className="empty-block">
            <h2>{search || status || offset ? 'No matching investigations' : 'No saved investigations'}</h2>
            <p>{search || status || offset ? 'Change the search or reset the filters to see more results.' : 'Start an investigation with a description, request ID, or trace ID.'}</p>
            {search || status || offset ? (
              <button className="button button-secondary" type="button" onClick={() => setParams({})}>Reset filters</button>
            ) : (
              <Link className="button button-primary" to="/investigations/new"><Plus size={15} aria-hidden="true" /> New Investigation</Link>
            )}
          </div>
        )}
        {!loading && !error && data && data.items.length > 0 && (
          <div className="table-footer">
            <span>Showing {data.offset + 1}–{data.offset + data.items.length} of {data.total}{offset + PAGE_SIZE > MAX_OFFSET && data.offset + data.items.length < data.total && ' · Narrow the search to see more'}</span>
            <div className="pagination">
              <button className="button button-secondary" type="button" disabled={offset === 0} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}>
                <ChevronLeft size={14} aria-hidden="true" /> Previous
              </button>
              <button className="button button-secondary" type="button" disabled={data.offset + data.items.length >= data.total || offset + PAGE_SIZE > MAX_OFFSET} onClick={() => changePage(offset + PAGE_SIZE)}>
                Next <ChevronRight size={14} aria-hidden="true" />
              </button>
            </div>
          </div>
        )}
      </section>
    </>
  );
}

function HistorySearch({ search, onSearch }: { search: string; onSearch: (value: string) => void }) {
  const [draft, setDraft] = useState(search);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSearch(draft.trim());
  }
  return (
    <form className="history-search" onSubmit={submit} role="search" aria-label="Investigation history">
      <label>
        <Search size={16} aria-hidden="true" />
        <span className="sr-only">Search investigations</span>
        <input type="search" maxLength={128} placeholder="Search by ID, context, or cause…" value={draft} onChange={(event) => setDraft(event.target.value)} />
      </label>
      <button className="button button-secondary" type="submit">Search</button>
    </form>
  );
}
