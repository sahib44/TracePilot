import { ArrowRight, Plus } from 'lucide-react'
import { Link } from 'react-router'
import { InvestigationTable } from '../components/InvestigationTable'
import { MetricPanels, MonitoringControls, MonitoringError, MonitoringLoading, MonitoringNotice, ProbeState, serviceLabel, useMonitoring } from '../components/monitoring'

export default function Overview() {
  const { data, error, loading, refresh, window, setWindow } = useMonitoring()
  const readyCount = data?.services.filter((service) => service.readiness?.ok).length ?? 0
  const unverifiedCount = data?.services.filter((service) => service.readiness === null).length ?? 0

  return (
    <div className="monitor-page">
      <header className="monitor-page-head">
        <div>
          <p className="eyebrow">Local environment</p>
          <h1>Overview</h1>
          <p className="monitor-description">Service health, request telemetry, and recent incident investigations.</p>
        </div>
        <MonitoringControls window={window} setWindow={setWindow} loading={loading} refresh={refresh} observedAt={data?.observed_at} />
      </header>
      {error && <MonitoringError error={error} hasData={Boolean(data)} />}
      {!data && loading && <MonitoringLoading />}
      {data && <>
        <section className="panel monitor-health" aria-labelledby="overview-health">
          <div className="monitor-health-summary">
            <div>
              <h2 id="overview-health">Service health</h2>
              <p className="monitor-description">{unverifiedCount === data.services.length
                ? 'Readiness unavailable'
                : `${readyCount} of ${data.services.length} services ready${unverifiedCount ? ` · ${unverifiedCount} unverified` : ''}`}</p>
            </div>
            <Link className="monitor-link" to="/services" aria-label="View service details"><ArrowRight size={17} aria-hidden="true" /></Link>
          </div>
          {data.services.map((service) => <div className="monitor-health-service" key={service.service}>
            <h3>{serviceLabel(service.service)}</h3>
            <dl>
              <div><dt>Liveness</dt><dd><ProbeState probe={service.liveness} kind="liveness" /></dd></div>
              <div><dt>Readiness</dt><dd><ProbeState probe={service.readiness} kind="readiness" /></dd></div>
            </dl>
          </div>)}
        </section>
        {data.health_error && <MonitoringNotice title="Service checks incomplete" detail={data.health_error} />}
        <section className="panel monitor-telemetry" aria-labelledby="overview-telemetry">
          <header className="monitor-section-head">
            <div>
              <h2 id="overview-telemetry">Request telemetry</h2>
              <p className="monitor-description">5-minute calculations from Prometheus, excluding health probes. Charts show local time.</p>
            </div>
            <span className="monitor-window-label">Last {data.window_minutes} minutes</span>
          </header>
          <MetricPanels snapshot={data} />
        </section>
        <section className="panel monitor-recent" aria-labelledby="overview-investigations">
          <header className="monitor-section-head">
            <div>
              <h2 id="overview-investigations">Recent investigations</h2>
              <p className="monitor-description">Latest saved investigation results.</p>
            </div>
            <Link className="monitor-link" to="/investigations">View history <ArrowRight size={14} aria-hidden="true" /></Link>
          </header>
          {data.recent_error ? <MonitoringNotice title="Investigation history unavailable" detail={data.recent_error} />
            : data.recent_investigations.length > 0 ? <InvestigationTable investigations={data.recent_investigations} />
            : <div className="monitor-recent-empty">
              <div>
                <strong>No saved investigations</strong>
                <p className="monitor-description">Start with an incident description, request ID, or trace ID.</p>
              </div>
              <Link className="button button-primary" to="/investigations/new"><Plus size={15} aria-hidden="true" /> New investigation</Link>
            </div>}
        </section>
      </>}
    </div>
  )
}
