import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Pencil, Plug2, Plus, RefreshCw, Trash2, Wifi, X } from "lucide-react";
import { api } from "../api";
import { Empty, Modal, PageHeader, StatusDot } from "../components/ui";

const KIND_LABEL: Record<string, string> = { tool: "Tools", resource: "Resources", template: "Resource templates", prompt: "Prompts" };
type Hdr = { k: string; v: string; saved?: boolean };
const isAuth = (k: string) => k.trim().toLowerCase() === "authorization";
/** What goes into the Authorization header for what the admin typed: a bare token becomes "Bearer <token>"; a full value ("Basic …", "Token …") is sent as it is. */
export const authValue = (v: string) => (/\s/.test(v.trim()) ? v.trim() : "Bearer " + v.trim());

function EndpointForm({ init, onClose, onDone }: { init?: any; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ name: init?.name || "", url: init?.url || "", transport: init?.transport || "auto" });
  const [hdrs, setHdrs] = useState<Hdr[]>((init?.header_names || []).filter((k: string) => !isAuth(k)).map((k: string) => ({ k, v: "", saved: true })));
  const [token, setToken] = useState("");                                                             // the optional Authorization field
  const [tokenSaved, setTokenSaved] = useState<boolean>((init?.header_names || []).some(isAuth));   // a token is already stored (its value is never sent back)
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [err, setErr] = useState("");
  const [probe, setProbe] = useState<any>(null);
  const headers = () => {
    const own = token.trim();
    // the Authorization field wins over a header row of the same name (header names are not case sensitive, so both would be sent)
    const rows = hdrs.filter((h) => h.k.trim() && !(own && isAuth(h.k)));
    const out: Record<string, string> = Object.fromEntries(rows.map((h) => [h.k.trim(), h.v]));
    if (own) out["Authorization"] = authValue(own);
    else if (tokenSaved) out["Authorization"] = "";   // empty = keep the saved value
    return out;
  };
  const test = async () => {
    setTesting(true); setProbe(null); setErr("");
    try { setProbe(await api("/api/mcp-endpoints/test", { method: "POST", json: { ...f, headers: headers(), endpoint_id: init?.id ?? null } })); } catch (e: any) { setErr(e.message); }
    setTesting(false);
  };
  const save = async () => {
    setBusy(true); setErr("");
    try {
      if (init) await api(`/api/mcp-endpoints/${init.id}`, { method: "PATCH", json: { ...f, headers: headers() } });
      else await api("/api/mcp-endpoints", { method: "POST", json: { ...f, headers: headers() } });
      onDone(); onClose();
    } catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  const setH = (i: number, p: Partial<Hdr>) => setHdrs(hdrs.map((h, j) => (j === i ? { ...h, ...p } : h)));
  return (
    <Modal title={init ? `Edit MCP server · ${init.name}` : "Add MCP server"} onClose={onClose}>
      <div className="space-y-4">
        <div><label className="lbl">Name</label><input className="input" placeholder="e.g. GitHub, Filesystem" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
        <div><label className="lbl">Server URL</label><input className="input mono" placeholder="https://host/mcp" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} /></div>
        <div><label className="lbl">Transport</label>
          <select className="input" value={f.transport} onChange={(e) => setF({ ...f, transport: e.target.value })}>
            <option value="auto">Auto-detect (Streamable HTTP, then legacy SSE)</option><option value="streamable_http">Streamable HTTP</option><option value="sse">Legacy SSE</option></select></div>
        <div>
          <label className="lbl">Authorization <span className="normal-case text-[#787884]">(optional · API token, stored encrypted)</span></label>
          <div className="flex gap-2">
            <input className="input mono" type="password" autoComplete="off" spellCheck={false} data-testid="auth-token" value={token} onChange={(e) => setToken(e.target.value)}
              placeholder={tokenSaved ? "saved (leave empty to keep)" : "paste the API token"} />
            {tokenSaved && !token && <button className="btn danger !px-2" title="Remove the saved token" onClick={() => setTokenSaved(false)} data-testid="auth-remove"><X size={14} /></button>}
          </div>
          <div className="mt-1 text-xs text-[#787884]">Sent as <span className="mono">Authorization: Bearer &lt;token&gt;</span>. To use another scheme, paste the whole value, e.g. <span className="mono">Basic dXNlcjpwYXNz</span>.</div>
        </div>
        <div>
          <label className="lbl">Other request headers <span className="normal-case text-[#787884]">(e.g. X-Api-Key; stored encrypted)</span></label>
          <div className="space-y-2">{hdrs.map((h, i) => (
            <div key={i} className="flex gap-2">
              <input className="input mono !w-44" placeholder="Header" value={h.k} readOnly={h.saved} onChange={(e) => setH(i, { k: e.target.value })} />
              <input className="input mono" type="password" autoComplete="off" placeholder={h.saved ? "saved (leave empty to keep)" : "value"} value={h.v} onChange={(e) => setH(i, { v: e.target.value })} />
              <button className="btn danger !px-2" onClick={() => setHdrs(hdrs.filter((_, j) => j !== i))}><X size={14} /></button>
            </div>))}
            <button className="btn !py-1" onClick={() => setHdrs([...hdrs, { k: "", v: "" }])}><Plus size={14} /> Add header</button></div>
        </div>
        {probe && (probe.ok
          ? <div className="rounded-md border border-[#5fb585]/30 bg-[#5fb585]/10 p-3 text-sm text-[#7fd1a3]">Connected{probe.server_info?.name ? ` to ${probe.server_info.name}` : ""} ({probe.mode}) · {probe.counts.tool} tools · {probe.counts.resource + probe.counts.template} resources · {probe.counts.prompt} prompts</div>
          : <div className="rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{probe.error}</div>)}
        {err && <div className="rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>}
        <div className="flex justify-between gap-2">
          <button className="btn" disabled={testing || !f.url} onClick={test}>{testing ? <Loader2 size={15} className="animate-spin" /> : <Wifi size={15} />} Test connection</button>
          <div className="flex gap-2"><button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || !f.name || !f.url} onClick={save}>{busy ? <><Loader2 size={16} className="animate-spin" /> Connecting…</> : init ? "Save" : "Add & discover"}</button></div>
        </div>
      </div>
    </Modal>
  );
}

function Items({ epId }: { epId: number }) {
  const [items, setItems] = useState<any[] | null>(null);
  useEffect(() => { api(`/api/mcp-endpoints/${epId}/items`).then(setItems); }, [epId]);
  if (!items) return <div className="py-3 text-sm text-[#8e8e9a]">Loading…</div>;
  if (items.length === 0) return <div className="py-3 text-sm text-[#8e8e9a]">This server offers nothing.</div>;
  return (
    <div className="mt-4 space-y-4">{Object.keys(KIND_LABEL).map((k) => {
      const rows = items.filter((i) => i.kind === k);
      return rows.length === 0 ? null : (
        <div key={k}><div className="mb-1 text-xs uppercase tracking-wider text-[#787884]">{KIND_LABEL[k]} ({rows.length})</div>
          <div className="divide-y divide-white/5 rounded-md border border-white/5">{rows.map((i) => (
            <div key={i.name} className="px-3 py-2 text-sm"><span className="mono">{i.name}</span>{i.description && <div className="mt-0.5 line-clamp-2 text-xs text-[#8e8e9a]">{i.description}</div>}</div>))}</div></div>);
    })}</div>
  );
}

export default function McpEndpoints() {
  const [eps, setEps] = useState<any[]>([]);
  const [form, setForm] = useState<any>(null);       // null | {} (new) | endpoint (edit)
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const load = () => api("/api/mcp-endpoints").then(setEps);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, []);
  const rescan = async (id: number) => { setBusy(id); try { await api(`/api/mcp-endpoints/${id}/rescan`, { method: "POST" }); await load(); } catch (e: any) { alert(e.message); } setBusy(null); };
  const del = async (ep: any) => {
    if (!confirm(`Delete MCP server "${ep.name}"?`)) return;
    try { await api(`/api/mcp-endpoints/${ep.id}`, { method: "DELETE" }); }
    catch (e: any) { if (!confirm(`${e.message}\n\nDelete it anyway? It is removed from those gateways.`)) return; await api(`/api/mcp-endpoints/${ep.id}?force=true`, { method: "DELETE" }); }
    load();
  };
  return (
    <div className="fadein">
      <PageHeader title="MCP Servers" sub="External MCP servers (Streamable HTTP or SSE) whose tools, resources and prompts you can combine into MCP gateways."
        actions={<button className="btn primary" onClick={() => setForm({})}><Plus size={16} /> Add MCP server</button>} />
      {eps.length === 0 && <Empty>No MCP servers yet. Add the URL of a remote MCP server to discover what it offers.</Empty>}
      <div className="space-y-5">{eps.map((ep) => (
        <div key={ep.id} className="card card-pad">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <StatusDot status={ep.status} />
              <div>
                <div className="flex items-center gap-2 text-lg font-semibold"><Plug2 size={16} className="text-[#8e8e9a]" />{ep.name}{ep.server_info?.name && ep.server_info.name !== ep.name && <span className="text-xs font-normal text-[#787884]">{ep.server_info.name} {ep.server_info.version}</span>}</div>
                <div className="mono text-xs text-[#8e8e9a]">{ep.url}{ep.header_names.length ? ` · ${ep.header_names.length} header(s)` : ""}</div>
              </div>
              {ep.status === "offline" && ep.last_error && <span className="chip" style={{ color: "#d9776f", background: "#d9776f22" }}>{ep.last_error}</span>}
            </div>
            <div className="flex gap-2">
              <button className="btn" disabled={busy === ep.id} onClick={() => rescan(ep.id)}><RefreshCw size={15} className={busy === ep.id ? "animate-spin" : ""} /> Re-scan</button>
              <button className="btn" onClick={() => setForm(ep)}><Pencil size={15} /></button>
              <button className="btn danger" onClick={() => del(ep)}><Trash2 size={15} /></button>
            </div>
          </div>
          <button className="mt-4 flex items-center gap-1.5 text-sm text-[#a79cff]" onClick={() => setOpen(open === ep.id ? null : ep.id)}>
            {open === ep.id ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            {ep.counts.tool} tools · {ep.counts.resource + ep.counts.template} resources · {ep.counts.prompt} prompts
            {ep.used_by.length > 0 && <span className="ml-2 text-xs text-[#787884]">used by {ep.used_by.join(", ")}</span>}
          </button>
          {open === ep.id && <Items epId={ep.id} />}
        </div>))}</div>
      {form && <EndpointForm init={form.id ? form : undefined} onClose={() => setForm(null)} onDone={load} />}
    </div>
  );
}
