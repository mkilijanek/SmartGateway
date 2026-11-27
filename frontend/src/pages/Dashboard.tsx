import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Activity, ArrowUpRight, Check, CheckCircle2, Clock, Minimize2, Zap } from "lucide-react";
import { api, PALETTE } from "../api";
import { useLive } from "../live";
import { fmt, ms, PageHeader, Stat, StatusDot } from "../components/ui";
import McpPanel from "../components/McpPanel";
import { PlanLimitsPanel } from "../components/PlanLimits";
import { CachePanel, ModelSpeedTable, PerformancePanel, SpeedChart } from "../components/PerfPanels";

const RANGES: [string, number][] = [["15 min", 15], ["1 hour", 60], ["6 hours", 360], ["24 hours", 1440]];
const tip = { contentStyle: { background: "#202024", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 12, fontSize: 12 }, cursor: { fill: "rgba(255,255,255,0.04)" } };

export default function Dashboard() {
  const [minutes, setMinutes] = useState(60);
  const [s, setS] = useState<any>(null);
  const [eps, setEps] = useState<any[]>([]);
  const [gws, setGws] = useState<any[]>([]);
  const [comp, setComp] = useState<any>(null);
  const [perf, setPerf] = useState<any>(null);
  const [mcp, setMcp] = useState<any>(null);
  const { snapshot } = useLive();
  useEffect(() => {
    const load = () => { api(`/api/stats?minutes=${minutes}`).then(setS); api("/api/endpoints").then(setEps); api("/api/gateways").then(setGws); api(`/api/insights/performance?minutes=${minutes}`).then(setPerf).catch(() => {}); api(`/api/insights/mcp?minutes=${minutes}`).then(setMcp).catch(() => {}); api(`/api/insights/compression?range=${minutes <= 60 ? "1h" : minutes <= 1440 ? "24h" : "7d"}`).then(setComp).catch(() => {}); };
    load(); const t = setInterval(load, 4000); return () => clearInterval(t);
  }, [minutes]);
  if (!s) return null;

  const donut = s.by_endpoint.map((e: any, i: number) => ({ ...e, fill: PALETTE[i % PALETTE.length] }));
  const top = s.by_model[0];
  const bars = s.timeline.map((b: any) => ({ ...b, label: new Date(b.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), ok: b.requests - b.errors }));
  const totalSlots = eps.reduce((a, e) => a + e.models.reduce((x: number, m: any) => x + m.max_slots, 0), 0);
  const busy = snapshot ? Object.values<any>(snapshot.models).reduce((a, m) => a + m.in_flight, 0) : 0;

  return (
    <div className="fadein">
      <PageHeader title="Dashboard" sub="Traffic, health and capacity across all gateways."
        actions={<div className="flex gap-1 rounded-lg border border-white/5 bg-white/[0.03] p-1">
          {RANGES.map(([l, v]) => <button key={v} onClick={() => setMinutes(v)} className={`rounded-md px-3.5 py-1.5 text-sm ${minutes === v ? "bg-white/10 text-white" : "text-[#8e8e9a]"}`}>{l}</button>)}</div>} />

      {(eps.length === 0 || gws.length === 0) && (
        <div className="card card-pad mb-5">
          <h3 className="text-base font-semibold">Get started</h3>
          <p className="mt-1 text-sm text-[#8e8e9a]">Three steps to your first request.</p>
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            {([
              ["1", "Add an endpoint", "Point SmartGateway at an OpenAI-compatible server (llama.cpp, vLLM, Ollama, a hosted API).", "/endpoints", eps.length > 0],
              ["2", "Create a gateway", "Expose its models under your own names and pick a routing strategy.", "/gateways/new", gws.length > 0],
              ["3", "Connect a client", "Use the gateway URL from the Gateways page in your app, Claude Code or Codex.", "/gateways", false],
            ] as [string, string, string, string, boolean][]).map(([n, t, d, to, done]) => (
              <Link key={n} to={to} className="rounded-lg border border-white/5 bg-white/[0.02] p-4 hover:bg-white/[0.05]">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <span className="grid h-6 w-6 place-items-center rounded-full text-xs" style={{ background: done ? "#5fb58533" : "#6c5ce733", color: done ? "#7fd1a3" : "#a79cff" }}>{done ? <Check size={13} /> : n}</span>{t}
                </div>
                <div className="mt-2 text-xs text-[#8e8e9a]">{d}</div>
              </Link>
            ))}
          </div>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <Stat icon={Activity} label="Requests" value={fmt(s.total)} unit="total" sub={`${s.rps} req/s`} />
        <Stat icon={CheckCircle2} label="Success rate" value={(s.success_rate * 100).toFixed(1) + "%"} sub={`${s.errors} errors`} tone={s.errors ? "warn" : "ok"} />
        <Stat icon={Clock} label="Latency p50 / p95" value={ms(s.p50_ms)} sub={`p95 ${ms(s.p95_ms)}`} />
        <Stat icon={Zap} label="Tokens generated" value={fmt(s.completion_tokens)} unit="tok" sub={`${fmt(s.prompt_tokens)} prompt`} />
        <Stat icon={Minimize2} label="Saved by compression" value={comp ? fmt(comp.totals.tokensNotSent) : "–"} unit="tok" tone="ok"
          sub={comp && comp.totals.potentialTokensNotSent ? `${fmt(comp.totals.potentialTokensNotSent)} more in shadow mode` : "prompt tokens not sent"} />
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-[1fr_1fr_1.4fr]">
        <div className="card card-pad">
          <h3 className="text-base font-semibold">Traffic by endpoint</h3>
          <div className="relative mx-auto mt-2 h-[190px]">
            {donut.length ? <ResponsiveContainer><PieChart>
              <Pie data={donut} dataKey="requests" nameKey="name" innerRadius={60} outerRadius={80} paddingAngle={1} cornerRadius={0} stroke="none">
                {donut.map((d: any, i: number) => <Cell key={i} fill={d.fill} />)}</Pie><Tooltip {...tip} /></PieChart></ResponsiveContainer>
              : <div className="flex h-full items-center justify-center text-sm text-[#787884]">No traffic yet</div>}
            {donut.length > 0 && <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center"><div className="text-2xl font-semibold">{fmt(s.total)}</div><div className="text-xs text-[#8e8e9a]">requests</div></div>}
          </div>
          <div className="mt-3 space-y-2.5">{donut.map((d: any) => (
            <div key={d.endpoint_id} className="flex items-center justify-between text-sm"><span className="flex items-center gap-2"><i className="h-2 w-2 rounded-full" style={{ background: d.fill }} />{d.name}</span><span>{fmt(d.requests)}</span></div>))}</div>
        </div>

        <div className="card card-pad">
          <h3 className="text-base font-semibold">Endpoint health</h3>
          <div className="mt-4 space-y-4">
            {eps.map((e) => {
              const st = snapshot?.endpoints?.[e.id]?.status || e.status;
              const slots = e.models.reduce((a: number, m: any) => a + m.max_slots, 0);
              const used = e.models.reduce((a: number, m: any) => a + (snapshot?.models?.[m.id]?.in_flight || 0), 0);
              return (<div key={e.id}>
                <div className="flex items-center justify-between text-sm"><span className="flex items-center gap-2"><StatusDot status={st} />{e.name}</span><span className="text-[#8e8e9a]">{used}/{slots}</span></div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-white/5"><div className="h-full rounded-full" style={{ width: `${slots ? (used / slots) * 100 : 0}%`, background: "#6c5ce7" }} /></div>
              </div>);
            })}
            {eps.length === 0 && <div className="text-sm text-[#787884]">No endpoints configured.</div>}
          </div>
          <div className="mt-6 border-t border-white/5 pt-4 text-xs text-[#8e8e9a]">Slot utilization: {busy}/{totalSlots} slots busy</div>
        </div>

        <div className="card card-pad">
          <h3 className="text-base font-semibold">Requests over time</h3>
          <div className="mt-3 h-[260px]">
            <ResponsiveContainer><BarChart data={bars} barSize={14}>
              <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#4cc38a" /><stop offset="100%" stopColor="#2f8f5f" /></linearGradient></defs>
              <XAxis dataKey="label" tick={{ fill: "#787884", fontSize: 11 }} axisLine={false} tickLine={false} interval={Math.floor(bars.length / 6)} />
              <YAxis tick={{ fill: "#787884", fontSize: 11 }} axisLine={false} tickLine={false} width={30} allowDecimals={false} />
              <Tooltip {...tip} />
              <Bar dataKey="ok" stackId="a" fill="url(#bg)" name="ok" radius={[4, 4, 0, 0]} />
              <Bar dataKey="errors" stackId="a" fill="#ef6a6a" name="errors" radius={[4, 4, 0, 0]} />
            </BarChart></ResponsiveContainer>
          </div>
        </div>
      </div>

      {perf && (<>
        <div className="mt-5 grid gap-5 xl:grid-cols-[1fr_2fr]"><CachePanel perf={perf} /><PerformancePanel perf={perf} /></div>
        <div className="mt-5 grid gap-5 xl:grid-cols-2"><SpeedChart perf={perf} /><ModelSpeedTable perf={perf} /></div>
      </>)}

      <div className="mt-5 empty:hidden"><PlanLimitsPanel /></div>

      {mcp && (mcp.totals.endpoints > 0 || mcp.totals.calls > 0) && <McpPanel d={mcp} />}

      <div className="mt-5 grid gap-5 xl:grid-cols-[1.3fr_1fr]">
        <div className="relative min-h-[220px] overflow-hidden rounded-lg border border-white/[0.08] p-6"
          style={{ background: "#202024" }}>
          <span className="chip border border-white/10 text-[#8e8e9a]">Insights</span>
          <p className="mt-10 max-w-lg text-lg leading-snug">
            {s.total === 0 ? "Send traffic to a gateway to see routing insights here."
              : `${top.model} handled ${Math.round((top.requests / s.total) * 100)}% of requests with ${(s.success_rate * 100).toFixed(0)}% success; median TTFT ${ms(s.ttft_p50_ms)}.`}
          </p>
          <Link to="/gateways" className="absolute bottom-5 right-5 rounded-lg bg-white/[0.06] p-2.5 hover:bg-white/10"><ArrowUpRight size={18} /></Link>
        </div>
        <div className="card card-pad">
          <h3 className="text-base font-semibold">Gateways</h3>
          <div className="mt-3 space-y-2">
            {gws.map((g) => {
              const q = snapshot?.queue?.[g.id] || 0;
              return <Link key={g.id} to={`/gateways/${g.id}`} className="flex items-center justify-between rounded-lg border border-white/5 bg-white/[0.02] px-4 py-3 hover:bg-white/[0.05]">
                <div><div className="font-medium">{g.name}</div><div className="mono text-xs text-[#8e8e9a]">{g.models.map((m: any) => m.public_name).join(", ") || "no models"}</div></div>
                {q > 0 ? <span className="chip text-[#d1a553]" style={{ background: "#d1a55322" }}><Clock size={12} /> {q} queued</span> : <ArrowUpRight size={16} className="text-[#787884]" />}
              </Link>;
            })}
            {gws.length === 0 && <div className="text-sm text-[#787884]">No gateways yet.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
