import { useEffect, useState } from "react";
import { api, PALETTE } from "../api";
import { Modal, ms, PageHeader } from "../components/ui";
import CouncilTabs from "../components/CouncilTabs";
import AuditLog from "../components/AuditLog";
import { Tabs, useTab } from "../components/Tabs";

const OUTCOMES: Record<string, [string, string, string]> = {
  complete: ["Complete", "#5fb585", "The model finished normally."],
  cut_by_limit: ["Cut: token limit", "#d1a553", "Stopped by the max_tokens limit in the middle of the answer (finish_reason: length). Raise max_tokens."],
  cut_in_reasoning: ["Cut while thinking", "#d1a553", "The token limit ran out during reasoning, before any answer text (finish_reason: length). Raise max_tokens or lower the reasoning effort."],
  no_answer: ["Reasoning only", "#d1a553", "Finished, but produced reasoning and no answer text."],
  truncated: ["Stream cut", "#d9776f", "The upstream closed the stream without a finish_reason: it dropped the connection or crashed mid-response."],
  stream_error: ["Stream error", "#d9776f", "Reading the upstream stream failed mid-response."],
  client_aborted: ["Client left", "#8e8e9a", "The client closed the connection before the response ended."],
  filtered: ["Filtered", "#d1a553", "Stopped by the provider's content filter."],
  empty: ["Empty", "#d1a553", "The response contained no text at all."],
  blocked: ["Blocked", "#d9776f", "Stopped by a guardrail (PII, prompt injection or a content filter)."],
  invalid_output: ["Invalid output", "#d9776f", "The model's answer failed JSON validation, also after the configured retries."],
};

export default function Logs() {
  const [rows, setRows] = useState<any[]>([]);
  const [gws, setGws] = useState<any[]>([]);
  const [eps, setEps] = useState<any[]>([]);
  const [gid, setGid] = useState("");
  const [errs, setErrs] = useState(false);
  const [probs, setProbs] = useState(false);
  const [run, setRun] = useState<any>(null);
  const openRun = (id: number) => api(`/api/council-runs/${id}`).then(setRun).catch(() => setRun({ missing: true }));
  const [tab, setTab] = useTab(["requests", "audit"]);
  useEffect(() => { api("/api/gateways?all=true").then(setGws); api("/api/endpoints").then(setEps); }, []);
  useEffect(() => {
    const load = () => api(`/api/logs?limit=200${gid ? `&gateway_id=${gid}` : ""}${errs ? "&only_errors=true" : ""}${probs ? "&problems=true" : ""}`).then(setRows);
    load(); const t = setInterval(load, 3000); return () => clearInterval(t);
  }, [gid, errs, probs]);
  const epName = (id: number) => eps.find((e) => e.id === id)?.name || "—";
  const gwName = (id: number) => gws.find((g) => g.id === id)?.name || id;
  const tabs = <Tabs items={[["requests", "Requests"], ["audit", "Audit log"]]} value={tab} onChange={setTab} />;
  const exportQs = new URLSearchParams({ ...(gid ? { gateway_id: gid } : {}), ...(errs ? { only_errors: "true" } : {}) }).toString().replace(/^(?=.)/, "&");   // encoded, so a value can never alter the URL
  if (tab === "audit") return <div className="fadein"><PageHeader title="Audit log" sub="Changes made through the admin interface." />{tabs}<AuditLog /></div>;
  return (
    <div className="fadein">
      <PageHeader title="Request Log" sub="Most recent routed requests (auto-refreshing)."
        actions={<>
          <a className="btn" href={`/api/logs/export?format=csv${exportQs}`} download title="Download all matching requests (not only the 200 shown)">Export CSV</a>
          <a className="btn" href={`/api/logs/export?format=jsonl${exportQs}`} download>JSONL</a>
          <select className="input !w-auto" value={gid} onChange={(e) => setGid(e.target.value)}><option value="">All gateways</option>{gws.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select>
          <label className="btn cursor-pointer"><input type="checkbox" checked={errs} onChange={(e) => setErrs(e.target.checked)} /> Errors only</label>
          <label className="btn cursor-pointer" title="Responses that were cut off, ran out of tokens while thinking, or ended without an answer"><input type="checkbox" checked={probs} onChange={(e) => setProbs(e.target.checked)} /> Incomplete only</label></>} />
      {tabs}
      <div className="card overflow-x-auto p-2 scroll-thin">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]">
            {["Time", "Gateway", "Public model", "Routed to", "Needs", "Status", "Ended", "Queue", "TTFT", "Total", "Tokens", "Saved", "Guard", "Tries"].map((h) => <th key={h} className="px-3 py-3 font-medium">{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-white/5 hover:bg-white/[0.02]" title={r.error || ""}>
                <td className="px-3 py-2 whitespace-nowrap text-[#8e8e9a]">{new Date(r.ts * 1000).toLocaleTimeString()}</td>
                <td className="px-3 py-2">{gwName(r.gateway_id)}</td>
                <td className="mono px-3 py-2">{r.public_model}{r.requirements === "council" && (r.council_run_id ? <button className="chip ml-2" style={{ color: "#b79cf0", background: "#b79cf01f" }} onClick={() => openRun(r.council_run_id)} title="Open the stored council run">Council · {r.attempts} calls</button> : <span className="chip ml-2" style={{ color: "#b79cf0", background: "#b79cf01f" }} title="Run storage is off for this council">Council · {r.attempts} calls</span>)}</td>
                <td className="px-3 py-2">{r.upstream_model ? <><span className="mono">{r.upstream_model}</span> <span className="text-[#787884]">@ {epName(r.endpoint_id)}</span></> : "—"}</td>
                <td className="px-3 py-2 text-xs text-[#8e8e9a]">{r.requirements || "—"}{r.stream && " · stream"}</td>
                <td className="px-3 py-2"><span className="chip" style={{ color: r.ok ? "#5fb585" : "#d9776f", background: (r.ok ? "#5fb585" : "#d9776f") + "1f" }}>{r.status}</span></td>
                <td className="px-3 py-2 whitespace-nowrap">{OUTCOMES[r.outcome] ? <span className="chip" title={`${OUTCOMES[r.outcome][2]}${r.finish_reason ? ` finish_reason: ${r.finish_reason}` : ""}`} style={{ color: OUTCOMES[r.outcome][1], background: OUTCOMES[r.outcome][1] + "1f" }}>{OUTCOMES[r.outcome][0]}</span> : <span className="text-[#787884]">—</span>}</td>
                <td className="px-3 py-2">{r.queue_ms ? ms(r.queue_ms) : "—"}</td>
                <td className="px-3 py-2">{r.ttft_ms != null ? ms(r.ttft_ms) : "—"}</td>
                <td className="px-3 py-2">{ms(r.total_ms)}</td>
                <td className="px-3 py-2 text-[#8e8e9a]">{r.prompt_tokens}→{r.completion_tokens}</td>
                <td className="px-3 py-2 whitespace-nowrap" title={r.compress_mode ? `compression: ${r.compress_mode}` : ""}>{r.tokens_saved ? <span style={{ color: r.compress_mode === "shadow" ? "#8e8e9a" : "#5fb585" }}>{r.compress_mode === "shadow" ? "~" : "−"}{r.tokens_saved}</span> : "—"}</td>
                <td className="px-3 py-2 whitespace-nowrap" title={r.guardrail_findings ? Object.entries(r.guardrail_findings).map(([k, v]) => `${k}: ${v}`).join("\n") : ""}>{r.guardrail_action ? <span className="chip" style={{ color: ["blocked", "failed"].includes(r.guardrail_action) ? "#d9776f" : r.guardrail_action === "pass" || r.guardrail_action === "validated" ? "#5fb585" : "#d1a553", background: (["blocked", "failed"].includes(r.guardrail_action) ? "#d9776f" : r.guardrail_action === "pass" || r.guardrail_action === "validated" ? "#5fb585" : "#d1a553") + "1f" }}>{r.guardrail_action}{r.output_retries ? ` ×${r.output_retries}` : ""}</span> : "—"}</td>
                <td className="px-3 py-2">{r.attempts}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="p-6 text-center text-sm text-[#8e8e9a]">No requests yet.</div>}
      </div>
      {run && (
        <Modal title="Council run" onClose={() => setRun(null)} wide>
          {run.missing ? <p className="text-sm text-[#8e8e9a]">This run was deleted by the retention policy.</p> : (<>
            <div className="mb-2 text-xs text-[#787884]">{new Date(run.ts * 1000).toLocaleString()} · {ms(run.total_ms)} · {run.status}</div>
            <div className="mb-1 text-xs font-semibold text-[#8e8e9a]">Question</div>
            <div className="mb-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-black/30 p-2 text-xs scroll-thin">{run.question}</div>
            <div className="mb-1 text-xs font-semibold text-[#8e8e9a]">Final answer</div>
            <div className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-black/30 p-2 text-sm scroll-thin">{run.final || "—"}</div>
            {run.details?.stage1 && <CouncilTabs data={run.details} />}</>)}
        </Modal>)}
    </div>
  );
}
