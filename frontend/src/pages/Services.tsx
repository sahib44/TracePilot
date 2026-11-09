import { Activity } from 'lucide-react'
import { MetricPanels, MonitoringControls, MonitoringError, MonitoringLinks, MonitoringLoading, MonitoringNotice, ProbeDetail, serviceLabel, useMonitoring } from '../components/monitoring'

export default function Services() {
  const { data, error, loading, refresh, window, setWindow } = useMonitoring()

  return (
    <div className="monitor-page">
      <header className="monitor-page-head">
        <div>
          <p className="eyebrow">Local environment</p>
          <h1>Services</h1>
          <p className="monitor-description">Health checks and recent telemetry for the demo stack.</p>
        </div>
        <MonitoringControls window={window} setWindow={setWindow} loading={loading} refresh={refresh} observedAt={data?.observed_at} />
      </header>
      <div className="monitor-source-bar">
        <p className="monitor-description">5-minute calculations from Prometheus, excluding health probes. Charts show local time.</p>
        <MonitoringLinks />
      </div>
      {error && <MonitoringError error={error} hasData={Boolean(data)} />}
      {!data && loading && <MonitoringLoading />}
      {data?.health_error && <MonitoringNotice title="Service checks incomplete" detail={data.health_error} />}
      {data?.services.map((service) => <section className="panel monitor-service" key={service.service} aria-labelledby={`heading-${service.service}`}>
        <header className="monitor-section-head">
          <div className="monitor-service-title">
            <Activity aria-hidden="true" size={17} />
            <div>
              <h2 id={`heading-${service.service}`}>{serviceLabel(service.service)}</h2>
              <p className="monitor-description"><code>{service.service}</code></p>
            </div>
          </div>
          <span className="monitor-window-label">Last {data.window_minutes} minutes</span>
        </header>
        <div className="monitor-probe-grid">
          <ProbeDetail probe={service.liveness} kind="liveness" />
          <ProbeDetail probe={service.readiness} kind="readiness" />
        </div>
        <MetricPanels snapshot={data} services={[service]} />
      </section>)}
    </div>
  )
}
