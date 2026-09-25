import { useState } from "react";
import { api, pct, ReplayVerify, WhatIf } from "../api";
import { usePoll } from "../usePoll";

export default function Replay() {
  const { data: policies } = usePoll(api.policies, 0);
  const [verify, setVerify] = useState<ReplayVerify | null>(null);
  const [whatIf, setWhatIf] = useState<WhatIf | null>(null);
  const [policy, setPolicy] = useState("v2-strict");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = async <T,>(name: string, fn: () => Promise<T>, set: (t: T) => void) => {
    setBusy(name);
    setErr(null);
    try {
      set(await fn());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="grid two">
      <section className="panel">
        <h2>Deterministic replay</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Rebuild everything from the event log and the recorded tool outputs, re-derive every decision, and compare
          hashes with what was stored.
        </p>
        <button className="primary" disabled={!!busy} onClick={() => run("verify", api.replayVerify, setVerify)}>
          {busy === "verify" ? "Replaying…" : "Replay and verify"}
        </button>
        {verify && (
          <table style={{ marginTop: 12 }}>
            <tbody>
              <tr>
                <td>result</td>
                <td>
                  <span className={`pill ${verify.ok ? "good" : "bad"}`}>{verify.ok ? "every decision matches" : "mismatch"}</span>
                </td>
              </tr>
              <tr>
                <td>events replayed</td>
                <td>{verify.events_replayed.toLocaleString()}</td>
              </tr>
              <tr>
                <td>decisions checked</td>
                <td>{verify.decisions_checked.toLocaleString()}</td>
              </tr>
              <tr>
                <td>mismatches</td>
                <td>{verify.mismatch_count + verify.missing_count}</td>
              </tr>
              <tr>
                <td>state matches live engine</td>
                <td>{verify.matches_live_state ? "yes" : "no"}</td>
              </tr>
              <tr>
                <td>time</td>
                <td>
                  {verify.elapsed_s}s ({verify.events_per_s?.toLocaleString()} events/s)
                </td>
              </tr>
              <tr>
                <td>state hash</td>
                <td className="mono">{verify.state_hash.slice(0, 24)}…</td>
              </tr>
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h2>Policy what-if</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Replay all of history under a different policy version and see which decisions would have changed, before
          shipping the change.
        </p>
        <div className="row">
          <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
            {policies &&
              Object.keys(policies.available).map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
          </select>
          <button className="primary" disabled={!!busy} onClick={() => run("whatif", () => api.whatIf(policy), setWhatIf)}>
            {busy === "whatif" ? "Replaying…" : "Compare with current"}
          </button>
        </div>
        {whatIf && (
          <div style={{ marginTop: 12 }}>
            <div className="row">
              <span className="pill">{whatIf.changed.toLocaleString()} of {whatIf.decisions_compared.toLocaleString()} decisions change</span>
              <span className="pill">
                auto-resolution {pct(whatIf.auto_resolution_rate.current)} → {pct(whatIf.auto_resolution_rate.candidate)}
              </span>
            </div>
            <table style={{ marginTop: 8 }}>
              <tbody>
                {Object.entries(whatIf.changes_by_type).map(([k, n]) => (
                  <tr key={k}>
                    <td className="mono">{k}</td>
                    <td className="num">{n.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {policies && (
          <details style={{ marginTop: 12 }}>
            <summary className="muted">policy settings</summary>
            <pre>{JSON.stringify(policies.available, null, 2)}</pre>
          </details>
        )}
      </section>
      {err && <p className="error">{err}</p>}
    </div>
  );
}
