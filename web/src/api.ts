export const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export type AgentStats = { decisions: number; auto_resolved: number; auto_resolution_rate: number | null };

export type Metrics = {
  events_by_type: Record<string, number>;
  events_total: number;
  agents: Record<string, AgentStats>;
  decisions_total: number;
  auto_resolved_total: number;
  auto_resolution_rate: number | null;
  exceptions_open_by_kind: Record<string, number>;
  exceptions_open: number;
  exceptions_resolved: number;
  engine: {
    batches: number;
    events_since_boot: number;
    busy_s: number;
    events_per_busy_s: number | null;
    policy: string;
  };
  stream: { pending?: number; lag?: number } | null;
};

export type ExceptionRow = {
  exception_id: string;
  decision_id: string;
  agent: "onboarding" | "risk" | "cash";
  kind: string;
  subject_id: string;
  summary: string;
  status: "open" | "resolved";
  opened_seq: number;
  resolved_seq: number | null;
  resolution: Record<string, unknown> | null;
};

export type Decision = {
  decision_id: string;
  agent: string;
  action: string;
  subject_id: string;
  auto_resolved: boolean;
  reasons: string[];
  detail: Record<string, unknown>;
  policy_version: string;
};

export type Invoice = { invoice_id: string; customer_id: string; amount_cents: number; open_cents: number; due_date: string };

export type ExceptionDetail = ExceptionRow & {
  decision: Decision | null;
  subject: Record<string, unknown> & { candidate_invoices?: Invoice[]; open_invoices?: Invoice[] };
};

export type AuditEntry = {
  position: number;
  event_seq: number;
  kind: string;
  ref_id: string;
  body: Decision & { decision_hash: string; exception: { kind: string } | null };
  prev_hash: string;
  hash: string;
};

export type AuditVerify = { ok: boolean; entries: number; head_hash: string; first_bad_position: number | null; reason: string | null };

export type ReplayVerify = {
  ok: boolean;
  events_replayed: number;
  decisions_checked: number;
  mismatch_count: number;
  missing_count: number;
  state_hash: string;
  live_state_hash: string;
  matches_live_state: boolean;
  elapsed_s: number;
  events_per_s: number | null;
};

export type WhatIf = {
  policy: string;
  decisions_compared: number;
  changed: number;
  changes_by_type: Record<string, number>;
  examples: { decision_id: string; subject_id: string; change: string }[];
  auto_resolution_rate: { current: number; candidate: number };
};

export type Policies = { active: string; available: Record<string, Record<string, unknown>> };

export const api = {
  metrics: () => request<Metrics>("/metrics"),
  exceptions: (status: string, agent?: string) =>
    request<ExceptionRow[]>(`/exceptions?status=${status}&limit=200${agent ? `&agent=${agent}` : ""}`),
  exception: (id: string) => request<ExceptionDetail>(`/exceptions/${id}`),
  resolve: (id: string, resolution: Record<string, unknown>, note?: string) =>
    request<{ exception: ExceptionRow; batch: { decisions: number } }>(`/exceptions/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolution, resolved_by: "dashboard", note }),
    }),
  audit: (limit = 50) => request<AuditEntry[]>(`/audit?limit=${limit}`),
  auditVerify: () => request<AuditVerify>("/audit/verify"),
  replayVerify: () => request<ReplayVerify>("/replay/verify", { method: "POST" }),
  whatIf: (policy: string) => request<WhatIf>("/replay/what-if", { method: "POST", body: JSON.stringify({ policy }) }),
  policies: () => request<Policies>("/policies"),
};

export const money = (cents: number) =>
  (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

export const pct = (x: number | null | undefined, digits = 1) =>
  x == null ? "—" : `${(x * 100).toFixed(digits)}%`;
