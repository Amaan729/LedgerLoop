import { useEffect, useState } from "react";
import { api, ExceptionDetail, ExceptionRow, money } from "../api";
import { usePoll } from "../usePoll";

const AGENTS = ["", "onboarding", "risk", "cash"];

export default function Exceptions() {
  const [status, setStatus] = useState("open");
  const [agent, setAgent] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const { data: rows, error, refresh } = usePoll(() => api.exceptions(status, agent || undefined), 5000, [status, agent]);

  return (
    <div className="split">
      <section className="panel">
        <div className="row spread" style={{ marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>Exception queue</h2>
          <div className="row">
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="open">open</option>
              <option value="resolved">resolved</option>
            </select>
            <select value={agent} onChange={(e) => setAgent(e.target.value)}>
              {AGENTS.map((a) => (
                <option key={a} value={a}>
                  {a || "all agents"}
                </option>
              ))}
            </select>
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {rows && rows.length === 0 && <p className="muted">Nothing here.</p>}
        {rows && rows.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>agent</th>
                <th>kind</th>
                <th>summary</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r: ExceptionRow) => (
                <tr
                  key={r.exception_id}
                  className={`clickable ${selected === r.exception_id ? "selected" : ""}`}
                  onClick={() => setSelected(r.exception_id)}
                >
                  <td>{r.agent}</td>
                  <td className="mono">{r.kind}</td>
                  <td>{r.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="panel">
        {selected ? (
          <Detail id={selected} onResolved={() => { refresh(); }} />
        ) : (
          <p className="muted">Pick an exception to see why the agent stopped and resolve it.</p>
        )}
      </section>
    </div>
  );
}

function Detail({ id, onResolved }: { id: string; onResolved: () => void }) {
  const [d, setD] = useState<ExceptionDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.exception(id).then(setD).catch((e: Error) => setErr(e.message));
  useEffect(() => {
    setD(null);
    setErr(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const submit = async (resolution: Record<string, unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      const res = await api.resolve(id, resolution);
      if (res.exception.status !== "resolved") setErr("The engine rejected that resolution. Check the audit trail for why.");
      await load();
      onResolved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!d) return <p className="muted">{err ?? "Loading…"}</p>;

  return (
    <div className="grid" style={{ gap: 12 }}>
      <div className="row spread">
        <h2 style={{ margin: 0 }}>
          {d.agent} · <span className="mono">{d.kind}</span>
        </h2>
        <span className={`pill ${d.status === "open" ? "warn" : "good"}`}>{d.status}</span>
      </div>
      <p style={{ margin: 0 }}>{d.summary}</p>
      <div className="muted mono">subject {d.subject_id} · decision {d.decision_id}</div>

      {d.decision && (
        <>
          <div className="row">
            {d.decision.reasons.map((r) => (
              <span key={r} className="pill">{r}</span>
            ))}
            <span className="pill">policy {d.decision.policy_version}</span>
          </div>
          <details>
            <summary className="muted">what the agent saw</summary>
            <pre>{JSON.stringify(d.decision.detail, null, 2)}</pre>
          </details>
        </>
      )}
      <details>
        <summary className="muted">current state of {d.subject_id}</summary>
        <pre>{JSON.stringify(d.subject, null, 2)}</pre>
      </details>

      {d.status === "open" ? (
        <ResolveForm d={d} busy={busy} onSubmit={submit} />
      ) : (
        <pre>{JSON.stringify(d.resolution, null, 2)}</pre>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}

function ResolveForm({ d, busy, onSubmit }: { d: ExceptionDetail; busy: boolean; onSubmit: (r: Record<string, unknown>) => void }) {
  const [limit, setLimit] = useState("25000");
  const invoices = d.subject?.candidate_invoices ?? [];
  const [alloc, setAlloc] = useState<Record<string, string>>({});
  const unapplied = (d.subject?.unapplied_cents as number | undefined) ?? 0;

  const btn = (label: string, res: Record<string, unknown>, primary = false) => (
    <button className={primary ? "primary" : "secondary"} disabled={busy} onClick={() => onSubmit(res)}>
      {label}
    </button>
  );

  if (d.agent === "onboarding") {
    return (
      <div className="row">
        <label>
          limit $ <input value={limit} onChange={(e) => setLimit(e.target.value)} style={{ width: 110 }} />
        </label>
        {btn("Approve", { action: "approve", credit_limit_cents: Math.round(Number(limit) * 100) }, true)}
        {btn("Reject", { action: "reject" })}
        {btn("Dismiss", { action: "dismiss" })}
      </div>
    );
  }
  if (d.agent === "risk") {
    return (
      <div className="row">
        {btn("Release order", { action: "release" }, true)}
        {btn("Reject order", { action: "reject" })}
      </div>
    );
  }

  const allocations = Object.entries(alloc)
    .map(([invoice_id, dollars]) => ({ invoice_id, cents: Math.round(Number(dollars) * 100) }))
    .filter((a) => a.cents > 0);
  const total = allocations.reduce((s, a) => s + a.cents, 0);

  return (
    <div className="grid" style={{ gap: 8 }}>
      <div className="muted">Unapplied: {money(unapplied)}</div>
      {invoices.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>invoice</th>
              <th className="num">open</th>
              <th>due</th>
              <th>apply $</th>
            </tr>
          </thead>
          <tbody>
            {invoices.map((inv) => (
              <tr key={inv.invoice_id}>
                <td className="mono">{inv.invoice_id}</td>
                <td className="num">{money(inv.open_cents)}</td>
                <td>{inv.due_date}</td>
                <td>
                  <input
                    style={{ width: 100 }}
                    value={alloc[inv.invoice_id] ?? ""}
                    placeholder="0"
                    onChange={(e) => setAlloc({ ...alloc, [inv.invoice_id]: e.target.value })}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No customer identified, so no candidate invoices. Put it on account or refund it.</p>
      )}
      <div className="row">
        {btn(`Apply ${money(total)}`, { action: "apply", allocations }, true)}
        {btn("On account", { action: "on_account" })}
        {btn("Refund", { action: "refund" })}
        {btn("Dismiss", { action: "dismiss" })}
      </div>
    </div>
  );
}
