import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Plus, Save, X } from "lucide-react";
import { api } from "../api";
import { PageHeader } from "../components/ui";
import McpKeys from "../components/McpKeys";

const KINDS: [string, string][] = [["tool", "Tools"], ["resource", "Resources"], ["template", "Resource templates"], ["prompt", "Prompts"]];
const exposed = (prefix: string, kind: string, name: string) => (kind === "resource" || kind === "template" ? `mcp://${prefix}/${name}` : `${prefix}__${name}`);

export default function McpGatewayEditor() {
  const { id } = useParams();
  const nav = useNavigate();
  const [f, setF] = useState<any>({ name: "", slug: "", description: "", session_scope: "shared", members: [] as any[] });
  const [eps, setEps] = useState<any[]>([]);
  const [items, setItems] = useState<Record<number, any[]>>({});
  const [q, setQ] = useState<Record<number, string>>({});
  const [pick, setPick] = useState("");
  const [err, setErr] = useState("");
  const loadItems = (epId: number) => api(`/api/mcp-endpoints/${epId}/items`).then((r) => { setItems((x) => ({ ...x, [epId]: r })); return r; });
  useEffect(() => {
    api("/api/mcp-endpoints").then(setEps);
    if (id) api(`/api/mcp-gateways/${id}`).then((g) => { setF({ ...g, description: g.description || "" }); g.members.forEach((m: any) => loadItems(m.endpoint_id)); });
  }, [id]);
  const setM = (i: number, p: any) => setF({ ...f, members: f.members.map((m: any, j: number) => (j === i ? { ...m, ...p } : m)) });
  const add = async () => {
    const ep = eps.find((e) => String(e.id) === pick);
    if (!ep) return;
    const list = await loadItems(ep.id);
    const prefix = ep.name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "mcp";
    setF({ ...f, members: [...f.members, { endpoint_id: ep.id, endpoint_name: ep.name, prefix, enabled: true, enabled_items: list.map((i: any) => `${i.kind}:${i.name}`) }] });
    setPick("");
  };
  const toggle = (i: number, key: string, on: boolean) => { const cur: string[] = f.members[i].enabled_items; setM(i, { enabled_items: on ? [...new Set([...cur, key])] : cur.filter((x) => x !== key) }); };
  const save = async () => {
    setErr("");
    const body = { name: f.name, slug: f.slug || null, description: f.description || null, session_scope: f.session_scope || "shared", members: f.members.map((m: any) => ({ endpoint_id: m.endpoint_id, prefix: m.prefix, enabled: m.enabled, enabled_items: m.enabled_items })) };
    try {
      const r = id ? await api(`/api/mcp-gateways/${id}`, { method: "PUT", json: body }) : await api("/api/mcp-gateways", { method: "POST", json: body });
      nav(id ? "/mcp-gateways" : `/mcp-gateways/${r.id}/edit`);
    } catch (e: any) { setErr(e.message); }
  };
  const free = eps.filter((e) => !f.members.some((m: any) => m.endpoint_id === e.id));
  const toolNames: string[] = f.members.filter((m: any) => m.enabled).flatMap((m: any) => (items[m.endpoint_id] || []).filter((i) => i.kind === "tool" && m.enabled_items.includes(`tool:${i.name}`)).map((i) => exposed(m.prefix, "tool", i.name)));
  return (
    <div className="fadein">
      <PageHeader back="/mcp-gateways" crumb={["MCP Gateways"]} title={id ? "Edit MCP gateway" : "New MCP gateway"} sub="Combine tools, resources and prompts of several MCP servers behind one MCP server URL."
        actions={<><button className="btn" onClick={() => nav("/mcp-gateways")}>Cancel</button><button className="btn primary" onClick={save}><Save size={16} /> Save gateway</button></>} />
      {err && <div className="mb-4 rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>}
      <div className="card card-pad mb-5 grid gap-4 md:grid-cols-2">
        <div><label className="lbl">Gateway name</label><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Dev tools" /></div>
        <div><label className="lbl">URL slug</label><input className="input mono" value={f.slug} onChange={(e) => setF({ ...f, slug: e.target.value })} placeholder="auto from name → /mcp/<slug>" /></div>
        <div className="md:col-span-2"><label className="lbl">Instructions for clients (optional)</label><input className="input" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="Sent to MCP clients as server instructions" /></div>
        <label className="flex cursor-pointer items-start gap-3 text-sm md:col-span-2"><input type="checkbox" className="mt-1" checked={f.session_scope === "key"} onChange={(e) => setF({ ...f, session_scope: e.target.checked ? "key" : "shared" })} />
          <span>Separate upstream session per API key<span className="block text-xs text-[#787884]">Off: all clients share one session per upstream MCP server. On: each gateway key gets its own, so a stateful server never mixes the context of different clients.</span></span></label>
      </div>
      <div className="space-y-4">{f.members.map((m: any, i: number) => {
        const list = items[m.endpoint_id] || [];
        const term = (q[m.endpoint_id] || "").toLowerCase();
        return (
          <div key={m.endpoint_id} className="card card-pad">
            <div className="flex flex-wrap items-end gap-4">
              <div className="min-w-[160px]"><div className="text-lg font-semibold">{m.endpoint_name}</div><label className="flex items-center gap-2 text-xs text-[#8e8e9a]"><input type="checkbox" checked={m.enabled} onChange={(e) => setM(i, { enabled: e.target.checked })} /> enabled</label></div>
              <div><label className="lbl">Prefix</label><input className="input mono !w-40" value={m.prefix} onChange={(e) => setM(i, { prefix: e.target.value })} title="Names from this endpoint are exposed as prefix__name" /></div>
              <input className="input !w-52" placeholder="Filter…" value={q[m.endpoint_id] || ""} onChange={(e) => setQ({ ...q, [m.endpoint_id]: e.target.value })} />
              <div className="flex gap-2"><button className="btn !py-1" onClick={() => setM(i, { enabled_items: list.map((x: any) => `${x.kind}:${x.name}`) })}>All</button><button className="btn !py-1" onClick={() => setM(i, { enabled_items: [] })}>None</button></div>
              <button className="btn danger ml-auto" onClick={() => setF({ ...f, members: f.members.filter((_: any, j: number) => j !== i) })}><X size={15} /></button>
            </div>
            <div className="mt-4 space-y-4">{KINDS.map(([k, label]) => {
              const rows = list.filter((x) => x.kind === k && (!term || x.name.toLowerCase().includes(term) || (x.description || "").toLowerCase().includes(term)));
              return rows.length === 0 ? null : (
                <div key={k}><div className="mb-1 text-xs uppercase tracking-wider text-[#787884]">{label}</div>
                  <div className="grid gap-1 md:grid-cols-2">{rows.map((x) => {
                    const key = `${x.kind}:${x.name}`;
                    const name = exposed(m.prefix, x.kind, x.name);
                    return (
                      <label key={key} className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-white/[0.03]">
                        <input type="checkbox" className="mt-1" checked={m.enabled_items.includes(key)} onChange={(e) => toggle(i, key, e.target.checked)} />
                        <span className="min-w-0"><span className="mono break-all">{name}</span>{x.kind === "tool" && name.length > 64 && <span className="ml-1 text-xs text-[#d1a553]" title="Some clients limit tool names to 64 characters">long name</span>}
                          {x.description && <span className="line-clamp-1 block text-xs text-[#787884]">{x.description}</span>}</span>
                      </label>);
                  })}</div></div>);
            })}{list.length === 0 && <div className="text-sm text-[#8e8e9a]">This endpoint offers nothing (yet).</div>}</div>
          </div>);
      })}</div>
      <div className="card card-pad mt-4 flex flex-wrap items-center gap-3">
        <select className="input !w-auto min-w-[240px]" value={pick} onChange={(e) => setPick(e.target.value)}><option value="">Add an MCP server…</option>{free.map((e) => <option key={e.id} value={e.id}>{e.name} ({e.counts.tool} tools)</option>)}</select>
        <button className="btn" disabled={!pick} onClick={add}><Plus size={15} /> Add</button>
        {eps.length === 0 && <span className="text-sm text-[#8e8e9a]">Add an MCP server first.</span>}
        <span className="ml-auto text-xs text-[#787884]">New items an endpoint offers later stay off until you tick them here.</span>
      </div>
      {id && <McpKeys gwId={+id} tools={toolNames} slug={f.slug} />}
    </div>
  );
}
