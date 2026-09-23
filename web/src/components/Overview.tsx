import { api, pct } from "../api";
import { usePoll } from "../usePoll";

const AGENT_BLURB: Record<string, string> = {
  onboarding: "credit decisions on new customers",
  risk: "release or hold each order",
  cash: "match payments to invoices",
};

export default function Overview() {
  const { data: m, error } = usePoll(api.metrics, 3000);

  if (error) return <p className="error">Can't reach the API: {error}</p>;
  if (!m) return <p className="muted">Loading…</p>;

  const kinds = Object.entries(m.exceptions_open_by_kind).sort((a, b) => b[1] - a[1]);
  const maxKind = Math.max(1, ...kinds.map(([, n]) => n));

  return (
    <div className="grid">
      <div className="grid kpis">
        <Kpi label="events in log" value={m.events_total.toLocaleString()} />
        <Kpi label="auto-resolved" value={pct(m.auto_resolution_rate)} sub={`${m.auto_resolved_total.toLocaleString()} of ${m.decisions_total.toLocaleString()} decisions`} />
        <Kpi label="open exceptions" value={m.exceptions_open.toLocaleString()} sub={`${m.exceptions_resolved.toLocaleString()} resolved by people`} />
        <Kpi
          label="engine throughput"
          value={m.engine.events_per_busy_s ? `${m.engine.events_per_busy_s.toLocaleString()}/s` : "—"}
          sub={`${m.engine.events_since_boot.toLocaleString()} events since boot · policy ${m.engine.policy}`}
        />
      </div>

      <div className="grid two">
        <section className="panel">
          <h2>Agents</h2>
          <table>
            <thead>
              <tr>
                <th>agent</th>
                <th className="num">decisions</th>
                <th className="num">auto</th>
                <th style={{ width: "30%" }}></th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(m.agents).map(([name, a]) => (
                <tr key={name}>
                  <td>
                    <b>{name}</b>
                    <div className="muted" style={{ fontSize: 12 }}>{AGENT_BLURB[name]}</div>
                  </td>
                  <td className="num">{a.decisions.toLocaleString()}</td>
                  <td className="num">{pct(a.auto_resolution_rate)}</td>
                  <td>
                    <div className="bar">
                      <span style={{ width: `${(a.auto_resolution_rate ?? 0) * 100}%` }} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2>Open exceptions by kind</h2>
          {kinds.length === 0 && <p className="muted">Queue is empty.</p>}
          <table>
            <tbody>
              {kinds.map(([kind, n]) => (
                <tr key={kind}>
                  <td className="mono">{kind}</td>
                  <td className="num">{n.toLocaleString()}</td>
                  <td style={{ width: "40%" }}>
                    <div className="bar">
                      <span style={{ width: `${(n / maxKind) * 100}%` }} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      <section className="panel">
        <h2>Events by type</h2>
        <div className="row">
          {Object.entries(m.events_by_type).map(([t, n]) => (
            <span key={t} className="pill">
              {t} · {n.toLocaleString()}
            </span>
          ))}
          {m.stream && (
            <span className="pill warn">
              stream pending {m.stream.pending ?? 0} · lag {m.stream.lag ?? 0}
            </span>
          )}
        </div>
      </section>
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="panel kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="muted" style={{ fontSize: 12 }}>{sub}</div>}
    </div>
  );
}
