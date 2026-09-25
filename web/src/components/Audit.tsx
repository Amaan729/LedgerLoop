import { Fragment, useState } from "react";
import { api, AuditVerify } from "../api";
import { usePoll } from "../usePoll";

export default function Audit() {
  const { data: rows, error } = usePoll(() => api.audit(100), 5000);
  const [verify, setVerify] = useState<AuditVerify | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  const runVerify = async () => {
    setBusy(true);
    try {
      setVerify(await api.auditVerify());
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid">
      <section className="panel">
        <div className="row spread">
          <div>
            <h2 style={{ margin: 0 }}>Hash chain</h2>
            <div className="muted" style={{ fontSize: 12 }}>
              Each entry's hash covers the previous one, so editing or deleting any row breaks every hash after it.
            </div>
          </div>
          <button className="primary" onClick={runVerify} disabled={busy}>
            {busy ? "Verifying…" : "Verify whole chain"}
          </button>
        </div>
        {verify && (
          <div className="row" style={{ marginTop: 12 }}>
            <span className={`pill ${verify.ok ? "good" : "bad"}`}>{verify.ok ? "intact" : "broken"}</span>
            <span>{verify.entries.toLocaleString()} entries checked</span>
            {!verify.ok && (
              <span className="error">
                at position {verify.first_bad_position}: {verify.reason}
              </span>
            )}
            <span className="mono muted">head {verify.head_hash.slice(0, 16)}…</span>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Latest entries</h2>
        {error && <p className="error">{error}</p>}
        <table>
          <thead>
            <tr>
              <th className="num">#</th>
              <th>kind</th>
              <th>agent</th>
              <th>action</th>
              <th>subject</th>
              <th>hash</th>
            </tr>
          </thead>
          <tbody>
            {rows?.map((r) => (
              <Fragment key={r.position}>
                <tr className="clickable" onClick={() => setOpen(open === r.position ? null : r.position)}>
                  <td className="num mono">{r.position}</td>
                  <td>{r.kind}</td>
                  <td>{r.body.agent}</td>
                  <td>
                    <span className={`pill ${r.body.auto_resolved ? "good" : r.body.exception ? "warn" : ""}`}>{r.body.action}</span>
                  </td>
                  <td className="mono">{r.body.subject_id}</td>
                  <td className="mono muted">{r.hash.slice(0, 12)}…</td>
                </tr>
                {open === r.position && (
                  <tr>
                    <td colSpan={6}>
                      <div className="mono muted">prev {r.prev_hash}</div>
                      <pre>{JSON.stringify(r.body, null, 2)}</pre>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
