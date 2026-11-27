import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowUpRight, Loader2, RefreshCw } from "lucide-react";
import { api } from "../api";

export const PROVIDER: Record<string, string> = { "opencode-go": "OpenCode Go", "opencode-zen": "OpenCode Zen", "codex-oauth": "ChatGPT / Codex", "claude-oauth": "Claude" };
export const barColor = (p: number) => (p >= 90 ? "#d9776f" : p >= 70 ? "#e0a458" : "#4cc38a");

/** "resets in 2 h 14 min" for a reset time. */
export function until(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (!Number.isFinite(s)) return "";
  if (s <= 0) return "resetting now";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return "resets in " + (d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${Math.max(1, m)} min`);
}
/** "12 min ago" for the time of the last successful reading. */
export function ago(ts: number | null | undefined): string {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
}

type Limits = { windows: { key: string; label: string; percent: number; resets_at: string | null }[]; error?: string | null; stale?: boolean; fetched_at?: number | null; last_ok?: number | null; note?: string; plan?: string | null } | null;

/** The usage windows of one account: how much of each is used and when it resets. `compact` is the version inside an endpoint card. */
export function LimitBars({ limits, compact }: { limits: Limits | undefined; compact?: boolean }) {
  if (!limits || !limits.fetched_at) return <div className="text-xs text-[#787884]">Plan usage not read yet: the gateway checks it in the background.</div>;
  const w = limits.windows || [];
  return (
    <div data-testid="limit-bars">
      {limits.error && (
        <div className="mb-2 flex items-start gap-1.5 text-xs text-amber-300"><AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>{w.length ? `Could not refresh: ${limits.error}. Showing the reading from ${ago(limits.last_ok)}.` : `Plan usage unavailable: ${limits.error}`}</span></div>)}
      {!limits.error && w.length === 0 && limits.note && <div className="text-xs text-[#787884]">{limits.note}</div>}
      <div className={compact ? "grid gap-x-6 gap-y-2 sm:grid-cols-2" : "space-y-3.5"}>
        {w.map((x) => (
          <div key={x.key} style={limits.stale ? { opacity: 0.65 } : undefined}>
            <div className="flex items-baseline justify-between text-xs"><span className="text-[#c8c8d0]">{x.label}</span>
              <span className="tabular-nums font-medium" style={{ color: barColor(x.percent) }}>{x.percent >= 100 ? "limit reached" : `${x.percent}% used`}</span></div>
            <div className="mt-1 h-2 overflow-hidden rounded-full bg-white/5" role="progressbar" aria-valuenow={x.percent} aria-valuemin={0} aria-valuemax={100} aria-label={x.label}>
              <div className="h-full rounded-full transition-all" style={{ width: `${Math.max(2, x.percent)}%`, background: barColor(x.percent) }} /></div>
            <div className="mt-0.5 text-[11px] text-[#787884]" title={x.resets_at || ""}>{until(x.resets_at) || "reset time unknown"}</div>
          </div>))}
      </div>
    </div>
  );
}

/** The latest plan usage of all subscription accounts (read by the background check; this only reads the gateway's copy). */
export function useLimits(every = 60000) {
  const [d, setD] = useState<{ interval: number; polling: boolean; accounts: any[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api("/api/limits").then(setD).catch(() => {}), []);
  useEffect(() => { load(); const t = setInterval(load, every); return () => clearInterval(t); }, [load, every]);
  const refresh = async () => { setBusy(true); try { setD(await api("/api/limits/refresh", { method: "POST" })); } catch { /* keep what we have */ } setBusy(false); };
  return { d, busy, refresh };
}

/** Dashboard card: 5-hour and weekly usage of every subscription account, with reset times. Hidden when there are none. */
export function PlanLimitsPanel() {
  const { d, busy, refresh } = useLimits();
  const rows = (d?.accounts || []);
  if (!d || rows.length === 0) return null;
  const newest = Math.max(0, ...rows.map((r) => r.fetched_at || 0));
  return (
    <div className="card card-pad" data-testid="plan-limits-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><h3 className="text-base font-semibold">Plan limits</h3>
          <p className="text-xs text-[#787884]">How much of each subscription is used and when it resets{newest ? ` · checked ${ago(newest)}` : ""}{d.polling ? ` · every ${Math.round(d.interval / 60)} min` : " · background check is off"}</p></div>
        <div className="flex gap-2">
          <button className="btn !py-1.5" disabled={busy} onClick={refresh} title="Ask the vendors now">{busy ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />} Check now</button>
          <Link to="/cost?tab=limits" className="btn !py-1.5">Details <ArrowUpRight size={14} /></Link>
        </div>
      </div>
      <div className="mt-4 grid gap-x-8 gap-y-5 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((r) => (
          <div key={r.endpoint_id}>
            <div className="mb-2 flex flex-wrap items-center gap-2 text-sm font-medium"><span className="truncate">{r.name}</span>
              <span className="chip" style={{ color: "#a79cff", background: "#6c5ce722" }}>{PROVIDER[r.provider] || r.provider}</span>
              {r.plan && <span className="chip text-[#c8c8d0]" style={{ background: "#ffffff0d" }}>{r.plan}</span>}</div>
            <LimitBars limits={r} />
          </div>))}
      </div>
    </div>
  );
}
