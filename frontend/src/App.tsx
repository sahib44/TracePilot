import { Activity, Plus } from 'lucide-react';
import { lazy, Suspense, useEffect } from 'react';
import { Link, NavLink, Route, Routes, useMatch, useNavigate } from 'react-router';
import { getHealth } from './api/client';
import { useRequest } from './api/useRequest';
import Investigations from './pages/Investigations';
import NewInvestigation, { useInvestigationSubmission } from './pages/NewInvestigation';

const Overview = lazy(() => import('./pages/Overview'));
const Services = lazy(() => import('./pages/Services'));
const InvestigationDetail = lazy(() => import('./pages/InvestigationDetail'));
const newInvestigationPath = '/investigations/new';

function ConnectionIndicator() {
  const { data, loading, error, refresh } = useRequest(getHealth, []);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) refresh();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const connected = data?.status === 'ok';
  const label = error ? 'Investigator disconnected' : loading && !connected ? 'Checking connection' : connected ? 'Investigator connected' : 'Investigator disconnected';
  const tone = error ? 'danger' : connected ? 'success' : 'neutral';
  return (
    <button type="button" className={`connection-indicator tone-${tone}`} onClick={refresh}
      title={`${label}. Click to recheck.`} aria-label={`${label}. Recheck connection`} disabled={loading}>
      <span className="status-dot" aria-hidden="true" /> <span className="connection-label" role="status">{label}</span>
    </button>
  );
}

export default function App() {
  const workflow = useInvestigationSubmission();
  const { startedAt, savedId, setSavedId } = workflow;
  const navigate = useNavigate();
  const onNewInvestigation = Boolean(useMatch(newInvestigationPath));

  useEffect(() => {
    if (savedId && onNewInvestigation) {
      void navigate(`/investigations/${savedId}`);
      setSavedId(null);
    }
  }, [savedId, onNewInvestigation, navigate, setSavedId]);

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <header className="app-header">
        <div className="header-content">
          <Link to="/" className="wordmark">TracePilot</Link>
          <nav className="app-nav" aria-label="Main navigation">
            <NavLink to="/" end>Overview</NavLink>
            <NavLink to="/investigations">Investigations</NavLink>
            <NavLink to="/services">Services</NavLink>
          </nav>
          <div className="header-actions">
            <ConnectionIndicator />
            <Link className="button button-primary header-new" to={newInvestigationPath}>
              <Plus size={16} aria-hidden="true" /> New Investigation
            </Link>
          </div>
        </div>
      </header>
      <main className="page" id="main-content" tabIndex={-1}>
        {!onNewInvestigation && startedAt !== null && (
          <div className="inline-notice tone-accent workflow-banner" role="status">
            <Activity size={16} aria-hidden="true" /> Investigation running <Link to={newInvestigationPath}>View status</Link>
          </div>
        )}
        {!onNewInvestigation && startedAt === null && savedId && (
          <div className="inline-notice tone-success workflow-banner" role="status">
            Investigation saved <Link to={`/investigations/${savedId}`} onClick={() => setSavedId(null)}>View result</Link>
          </div>
        )}
        {!onNewInvestigation && startedAt === null && workflow.error && (
          <div className="inline-notice tone-warning workflow-banner">
            Investigation request failed <Link to={newInvestigationPath}>Review error</Link>
          </div>
        )}
        <Suspense fallback={<div className="panel loading-block" role="status">Loading workspace…</div>}>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/investigations" element={<Investigations />} />
            <Route path={newInvestigationPath} element={<NewInvestigation workflow={workflow} />} />
            <Route path="/investigations/:investigationId" element={<InvestigationDetail />} />
            <Route path="/services" element={<Services />} />
            <Route path="*" element={
              <div className="panel empty-block">
                <h1>Page not found</h1><Link to="/" className="button button-secondary">Go to overview</Link>
              </div>
            } />
          </Routes>
        </Suspense>
      </main>
    </>
  );
}
