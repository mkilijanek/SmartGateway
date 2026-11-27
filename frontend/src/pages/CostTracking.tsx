import { useCallback, useEffect, useMemo, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, ChevronRight, CircleDollarSign, Gauge, Layers, Loader2, RefreshCw, Wallet } from "lucide-react";
import { api, PALETTE } from "../api";
import { Empty, fmt, PageHeader, Stat } from "../components/ui";
import { Tabs, useTab } from "../components/Tabs";
import { barColor, PROVIDER, until } from "../components/PlanLimits";

const RANGES: [string, number][] = [["24 hours", 1], ["7 days", 7], ["30 days", 30], ["90 days", 90]];
const tip = { contentStyle: { background: "#202024", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, fontSize: 12 }, cursor: { fill: "rgba(255,255,255,0.04)" } };

/** Token counts: 1.2k, 3.4M, 2.1B. */
const big = (n: number) => (n >= 1e9 ? (n / 1e9).toFixed(2) + "B" : fmt(n));
export const usd = (n: number | null | undefined) => {
  if (n == null) return "—";
  if (n === 0) return "$0";
  if (Math.abs(n) < 0.01) return "$" + n.toFixed(4);
  return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const BILLING: Record<string, { label: string; color: string; hint: string }> = {
  payg: { label: "Pay-as-you-go", color: "#e0a458", hint: "Billed per token" },
  subscription: { label: "Subscription", color: "#7c6cf0", hint: "A flat fee: the amount shown is what the same tokens would cost at API prices" },
  free: { label: "Free", color: "#4cc38a", hint: "Priced at zero" },
  unpriced: { label: "No price", color: "#787884", hint: "No price known: local models, or set one in Prices" },
};
const Billing = ({ kind }: { kind: string }) => { const b = BILLING[kind] || BILLING.unpriced; return <span className="chip" style={{ color: b.color, background: b.color + "22" }} title={b.hint}>{b.label}</span>; };

export default function CostTracking() {
  const [tab, setTab] = useTab(["usage", "limits", "prices"]);
  const [days, setDays] = useState(7);
  return (
    <div className="fadein">
      <PageHeader title="Cost Tracking" sub="What your traffic costs, how full your plan limits are, and the prices behind both."
        actions={tab === "usage" ? <div className="flex gap-1.5">{RANGES.map(([l, d]) => <button key={d} className={`pill ${days === d ? "on" : ""}`} onClick={() => setDays(d)}>{l}</button>)}</div> : undefined} />
      <Tabs items={[["usage", "Usage & cost"], ["limits", "Plan limits"], ["prices", "Prices"]]} value={tab} onChange={setTab} />
      {tab === "usage" && <Usage days={days} goPrices={() => setTab("prices")} />}
      {tab === "limits" && <Limits />}
      {tab === "prices" && <Prices />}
    </div>
  );
}

// ------------------------------------------------------------------ usage & cost
function Usage({ days, goPrices }: { days: number; goPrices: () => void }) {
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState("");
  useEffect(() => { setD(null); api(`/api/cost/summary?days=${days}`).then(setD).catch((e) => setErr(e.message)); }, [days]);
  const names = useMemo(() => [...new Set<string>((d?.daily || []).flatMap((x: any) => Object.keys(x.by_endpoint)))], [d]);
  if (err) return <div className="rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>;
  if (!d) return <div className="flex items-center gap-2 text-sm text-[#8e8e9a]"><Loader2 size={16} className="animate-spin" /> Loading…</div>;
  const t = d.totals;
  const chart = d.daily.map((x: any) => ({ label: x.day.slice(5), ...x.by_endpoint }));
  const since = d.history_from ? new Date(d.history_from * 1000).toISOString().slice(0, 10) : null;
  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <Stat icon={Wallet} label="Pay-as-you-go cost" value={usd(t.payg_cost)} sub="billed per token" tone={t.payg_cost > 0 ? "warn" : undefined} />
        <Stat icon={Layers} label="Subscription usage" value={usd(t.subscription_value)} sub="API-equivalent, not billed per token" />
        <Stat icon={CircleDollarSign} label="Tokens" value={big(t.prompt_tokens)} unit="in" sub={`${big(t.completion_tokens)} out · ${big(t.cached_tokens)} cached`} />
        <Stat icon={Gauge} label="Requests" value={fmt(t.requests)} sub={`${fmt(t.gateway_cache_hits)} answered from the gateway cache (free)`} />
        <Stat icon={AlertTriangle} label="Without a price" value={big(t.unpriced_tokens)} unit="tokens" sub={t.unpriced_requests ? <button className="underline" onClick={goPrices}>{fmt(t.unpriced_requests)} requests · set prices</button> : "everything is priced"} tone={t.unpriced_requests ? "warn" : "ok"} />
      </div>

      <div className="card card-pad">
        <h3 className="text-base font-semibold">Cost per day <span className="text-xs font-normal text-[#787884]">· UTC, by endpoint</span></h3>
        {names.length === 0 ? <div className="py-10 text-center text-sm text-[#8e8e9a]">No priced traffic in this range.</div> : (
          <div className="mt-3 h-[280px]">
            <ResponsiveContainer><BarChart data={chart} barSize={days > 30 ? 8 : 18}>
              <XAxis dataKey="label" tick={{ fill: "#787884", fontSize: 11 }} axisLine={false} tickLine={false} interval={Math.max(0, Math.floor(chart.length / 10) - 1)} />
              <YAxis tick={{ fill: "#787884", fontSize: 11 }} axisLine={false} tickLine={false} width={48} tickFormatter={(v) => "$" + v} />
              <Tooltip {...tip} formatter={(v: any, n: any) => [usd(Number(v)), n]} />
              {names.map((n, i) => <Bar key={n} dataKey={(row: any) => row[n] ?? 0} stackId="a" fill={PALETTE[i % PALETTE.length]} name={n} isAnimationActive={false} />)}
            </BarChart></ResponsiveContainer>
          </div>)}
        {names.length > 0 && <div className="mt-2 flex flex-wrap gap-3 text-xs text-[#8e8e9a]">{names.map((n, i) => <span key={n} className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: PALETTE[i % PALETTE.length] }} />{n}</span>)}</div>}
      </div>

      <div className="card card-pad">
        <h3 className="mb-3 text-base font-semibold">By model</h3>
        {d.by_model.length === 0 ? <div className="text-sm text-[#8e8e9a]">No traffic in this range.</div> : (
          <div className="overflow-x-auto scroll-thin"><table className="w-full text-sm">
            <thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]">
              <th className="pb-2 font-medium">Endpoint · model</th><th className="pb-2 font-medium">Billing</th><th className="pb-2 pl-4 text-right font-medium">Requests</th><th className="pb-2 pl-4 text-right font-medium">Input</th>
              <th className="pb-2 pl-4 text-right font-medium">Cached</th><th className="pb-2 pl-4 text-right font-medium">Output</th><th className="pb-2 pl-4 text-right font-medium whitespace-nowrap">Price / 1M in · out</th><th className="pb-2 pl-4 text-right font-medium">Cost</th></tr></thead>
            <tbody>{d.by_model.map((r: any) => (
              <tr key={r.endpoint_id + r.model} className="border-t border-white/5">
                <td className="py-2.5 pr-3"><div className="mono">{r.model}</div><div className="text-xs text-[#787884]">@ {r.endpoint}{r.provider && !r.endpoint.includes(PROVIDER[r.provider] || "\u0000") ? ` · ${PROVIDER[r.provider] || r.provider}` : ""}</div></td>
                <td className="py-2.5 pr-3"><Billing kind={r.billing} /></td>
                <td className="py-2.5 pl-4 text-right tabular-nums">{r.requests.toLocaleString()}</td>
                <td className="py-2.5 pl-4 text-right tabular-nums">{big(r.prompt_tokens)}</td>
                <td className="py-2.5 pl-4 text-right tabular-nums text-[#8e8e9a]">{r.cached_tokens ? big(r.cached_tokens) : "—"}</td>
                <td className="py-2.5 pl-4 text-right tabular-nums">{big(r.completion_tokens)}</td>
                <td className="py-2.5 pl-4 text-right tabular-nums whitespace-nowrap text-[#8e8e9a]">{r.price ? `${usd(r.price.in)} · ${usd(r.price.out)}${r.price.has_rules ? " *" : ""}` : <button className="underline" onClick={goPrices}>set</button>}</td>
                <td className="py-2.5 pl-4 text-right font-medium tabular-nums">{usd(r.cost)}</td>
              </tr>))}</tbody>
          </table></div>)}
      </div>

      <div className="card card-pad">
        <h3 className="mb-3 text-base font-semibold">By gateway</h3>
        <div className="overflow-x-auto scroll-thin"><table className="w-full text-sm">
          <thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]"><th className="pb-2 font-medium">Gateway</th><th className="pb-2 pl-4 text-right font-medium">Requests</th><th className="pb-2 pl-4 text-right font-medium">Tokens</th><th className="pb-2 pl-4 text-right font-medium">Pay-as-you-go</th><th className="pb-2 pl-4 text-right font-medium">Subscription value</th></tr></thead>
          <tbody>{d.by_gateway.map((g: any) => (
            <tr key={g.gateway_id} className="border-t border-white/5"><td className="py-2.5">{g.name}</td><td className="py-2.5 pl-4 text-right tabular-nums">{g.requests.toLocaleString()}</td><td className="py-2.5 pl-4 text-right tabular-nums">{big(g.tokens)}</td>
              <td className="py-2.5 pl-4 text-right tabular-nums">{usd(g.payg_cost)}</td><td className="py-2.5 pl-4 text-right tabular-nums text-[#8e8e9a]">{usd(g.subscription_value)}</td></tr>))}</tbody>
        </table></div>
      </div>

      <p className="text-xs leading-relaxed text-[#787884]">
        Cost = uncached input × input price + cached input × cache price + output × output price, for successful upstream requests; answers served from the gateway's own cache cost nothing.
        {" "}Prices with an asterisk include the catalog's peak-hour surcharge, applied by the UTC time of each request. Requests logged before cached tokens were tracked are priced without a cache discount, so older figures are an upper estimate.
        {" "}Subscriptions (ChatGPT / Codex, Claude, OpenCode Go) are a flat fee: their figure is what the same tokens would cost at API prices, shown only where a price is known.
        {since && <> History starts {since}; how long it is kept is set under Settings → Data retention.</>}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ plan limits
function Limits() {
  const [rows, setRows] = useState<any[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const load = useCallback((refresh = false) => {
    setBusy(true); setErr("");
    api(`/api/cost/limits${refresh ? "?refresh=true" : ""}`).then(setRows).catch((e) => setErr(e.message)).finally(() => setBusy(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  if (err) return <div className="rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>;
  if (!rows) return <div className="flex items-center gap-2 text-sm text-[#8e8e9a]"><Loader2 size={16} className="animate-spin" /> Asking the vendors…</div>;
  const newest = rows.length ? Math.max(...rows.map((r) => r.fetched_at)) : 0;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-[#8e8e9a]">
        <span>The gateway checks each vendor in the background every few minutes (reading usage costs no quota) and when you press Refresh; the same numbers appear on the Dashboard and the Endpoints page.{newest ? ` Updated ${new Date(newest * 1000).toLocaleTimeString()}.` : ""}</span>
        <button className="btn" disabled={busy} onClick={() => load(true)}><RefreshCw size={15} className={busy ? "animate-spin" : ""} /> Refresh</button>
      </div>
      {rows.length === 0 && <Empty>No subscriptions yet. Add an OpenCode, ChatGPT / Codex or Claude endpoint to see its plan limits here.</Empty>}
      <div className="grid gap-4 lg:grid-cols-2">
        {rows.map((r) => (
          <div key={r.endpoint_id} className="card card-pad" data-testid={`limits-${r.endpoint_id}`}>
            <div className="flex flex-wrap items-center gap-2">
              <div className="text-base font-semibold">{r.name}</div>
              <span className="chip" style={{ color: "#a79cff", background: "#6c5ce722" }}>{PROVIDER[r.provider] || r.provider}</span>
              {r.extra?.plan && <span className="chip text-[#c8c8d0]" style={{ background: "#ffffff0d" }}>{r.extra.plan} plan</span>}
              <Billing kind={r.billing} />
            </div>
            {r.error && <div className="mt-3 flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-400/10 p-2.5 text-xs text-amber-200"><AlertTriangle size={14} className="mt-0.5 shrink-0" /><span>Could not read the limits: {r.error}</span></div>}
            {r.note && !r.error && <p className="mt-3 text-xs text-[#8e8e9a]">{r.note}</p>}
            <div className="mt-4 space-y-4">
              {r.windows.map((w: any) => (
                <div key={w.key}>
                  <div className="flex items-baseline justify-between text-sm"><span>{w.label}</span><span className="tabular-nums font-medium" style={{ color: barColor(w.percent) }}>{w.percent >= 100 ? "limit reached" : `${w.percent}% used`}</span></div>
                  <div className="mt-1.5 h-2.5 overflow-hidden rounded-full bg-white/5" role="progressbar" aria-valuenow={w.percent} aria-valuemin={0} aria-valuemax={100} aria-label={`${r.name} ${w.label}`}>
                    <div className="h-full rounded-full transition-all" style={{ width: `${Math.max(2, w.percent)}%`, background: barColor(w.percent) }} /></div>
                  <div className="mt-1 flex flex-wrap justify-between gap-2 text-xs text-[#787884]">
                    <span title={w.resets_at || ""}>{until(w.resets_at) || "reset time unknown"}</span>
                    <span>via this gateway: {fmt(w.via_gateway.tokens)} tokens · {w.via_gateway.requests.toLocaleString()} requests</span>
                  </div>
                </div>))}
              {r.extra?.credits && <div className="text-xs text-[#8e8e9a]">Credits: {r.extra.credits.unlimited ? "unlimited" : r.extra.credits.has_credits ? `balance ${r.extra.credits.balance ?? "?"}` : "none"}</div>}
            </div>
            <div className="mt-3 border-t border-white/5 pt-2 text-xs text-[#787884]">Last 30 days via this gateway: {fmt(r.via_gateway_30d.tokens)} tokens · {r.via_gateway_30d.requests.toLocaleString()} requests</div>
          </div>))}
      </div>
      <p className="text-xs leading-relaxed text-[#787884]">The percentages come from each vendor's own usage endpoint for the whole account, so they also count use outside this gateway (other apps, the vendor's own tools). These endpoints are not public APIs and can change; when one stops answering you will see why above.</p>
    </div>
  );
}

// ------------------------------------------------------------------ prices
function PriceRow({ m, onSaved }: { m: any; onSaved: () => void }) {
  const p = m.price;
  const [v, setV] = useState({ i: p ? String(p.in) : "", o: p ? String(p.out) : "", c: p && p.source === "manual" && p.cache_read !== p.in ? String(p.cache_read) : "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const dirty = v.i !== (p ? String(p.in) : "") || v.o !== (p ? String(p.out) : "") || v.c !== (p && p.source === "manual" && p.cache_read !== p.in ? String(p.cache_read) : "");
  const save = async () => {
    setBusy(true); setErr("");
    try { await api(`/api/cost/prices/${m.model_id}`, { method: "PUT", json: { price_in: +v.i || 0, price_out: +v.o || 0, price_cache_read: v.c === "" ? null : +v.c } }); onSaved(); }
    catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  const reset = async () => { setBusy(true); try { await api(`/api/cost/prices/${m.model_id}`, { method: "DELETE" }); onSaved(); } catch (e: any) { setErr(e.message); } setBusy(false); };
  const num = "input !w-24 !py-1.5 text-right tabular-nums";
  return (
    <tr className="border-t border-white/5">
      <td className="py-2.5 pr-3"><div className="mono">{m.model}</div>{!m.enabled && <div className="text-xs text-[#787884]">disabled</div>}</td>
      <td className="py-2.5 pr-3"><Billing kind={m.billing} /></td>
      <td className="py-2.5 pr-2"><input type="number" min={0} step="any" className={num} value={v.i} placeholder="input" aria-label={`${m.model} input price`} onChange={(e) => setV({ ...v, i: e.target.value })} /></td>
      <td className="py-2.5 pr-2"><input type="number" min={0} step="any" className={num} value={v.o} placeholder="output" aria-label={`${m.model} output price`} onChange={(e) => setV({ ...v, o: e.target.value })} /></td>
      <td className="py-2.5 pr-3"><input type="number" min={0} step="any" className={num} value={v.c} placeholder="= input" aria-label={`${m.model} cached price`} onChange={(e) => setV({ ...v, c: e.target.value })} /></td>
      <td className="py-2.5 pr-3 text-xs text-[#787884]">{p ? (p.source === "manual" ? "manual" : `catalog${p.has_rules ? " + peak hours" : ""}`) : "none"}</td>
      <td className="py-2.5 text-right"><div className="flex justify-end gap-1.5">
        <button className="btn primary !py-1" disabled={busy || !dirty || (v.i === "" && v.o === "")} onClick={save}>Save</button>
        {p?.source === "manual" && <button className="btn !py-1" disabled={busy} onClick={reset} title={m.has_catalog_price ? "Use the catalog price again" : "Remove the price"}>Reset</button>}
      </div>{err && <div className="mt-1 text-xs text-red-300">{err}</div>}</td>
    </tr>
  );
}

function Prices() {
  const [eps, setEps] = useState<any[] | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const load = useCallback(() => api("/api/cost/prices").then(setEps), []);
  useEffect(() => { load(); }, [load]);
  const refresh = async () => {
    setBusy(true); setMsg("");
    try { const r = await api("/api/cost/prices/refresh", { method: "POST" }); setMsg(`Read ${r.refreshed} catalog${r.refreshed === 1 ? "" : "s"}.${r.failed.length ? " Failed: " + r.failed.join("; ") : ""}`); await load(); }
    catch (e: any) { setMsg(e.message); }
    setBusy(false);
  };
  if (!eps) return <div className="flex items-center gap-2 text-sm text-[#8e8e9a]"><Loader2 size={16} className="animate-spin" /> Loading…</div>;
  const hosted = eps.some((e) => e.hosted_catalog);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-3xl text-xs leading-relaxed text-[#8e8e9a]">Prices are US dollars per 1 million tokens. OpenCode's catalog states its own prices (including peak-hour surcharges). For everything else, enter a price yourself: a pay-as-you-go API such as DeepSeek, or an
          "API-equivalent" price for a subscription (ChatGPT / Codex, Claude) to see what its use would cost. Leave local models without a price.</p>
        {hosted && <button className="btn" disabled={busy} onClick={refresh}><RefreshCw size={15} className={busy ? "animate-spin" : ""} /> Refresh OpenCode prices</button>}
      </div>
      {msg && <div className="text-xs text-[#8e8e9a]">{msg}</div>}
      {eps.length === 0 && <Empty>No endpoints yet.</Empty>}
      {eps.map((e) => {
        const priced = e.models.filter((m: any) => m.price).length;
        return (
          <div key={e.endpoint_id} className="card card-pad">
            <button className="flex w-full items-center gap-3 text-left" onClick={() => setOpen((s) => { const n = new Set(s); n.has(e.endpoint_id) ? n.delete(e.endpoint_id) : n.add(e.endpoint_id); return n; })} aria-expanded={open.has(e.endpoint_id)}>
              <ChevronRight size={16} className={`shrink-0 text-[#787884] transition-transform ${open.has(e.endpoint_id) ? "rotate-90" : ""}`} />
              <span className="text-base font-semibold">{e.name}</span>
              {e.provider && <span className="chip" style={{ color: "#a79cff", background: "#6c5ce722" }}>{PROVIDER[e.provider] || e.provider}</span>}
              <span className="text-xs text-[#787884]">{priced} of {e.models.length} model{e.models.length === 1 ? "" : "s"} priced</span>
            </button>
            {open.has(e.endpoint_id) && (
              <div className="mt-4 overflow-x-auto scroll-thin"><table className="w-full text-sm">
                <thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]"><th className="pb-2 font-medium">Model</th><th className="pb-2 font-medium">Billing</th><th className="pb-2 font-medium">Input</th><th className="pb-2 font-medium">Output</th><th className="pb-2 font-medium" title="Price of input tokens the upstream served from its prompt cache; empty = same as input">Cached input</th><th className="pb-2 font-medium">Source</th><th /></tr></thead>
                <tbody>{e.models.map((m: any) => <PriceRow key={m.model_id + ":" + (m.price ? m.price.source + m.price.in + m.price.out : "-")} m={m} onSaved={load} />)}</tbody>
              </table></div>)}
          </div>);
      })}
    </div>
  );
}
