import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Plus, Save, Trash2 } from "lucide-react";
import { api, STRATEGIES } from "../api";
import { CapChip, PageHeader } from "../components/ui";
import KeysPanel from "../components/KeysPanel";
import GuardrailsEditor from "../components/GuardrailsEditor";
import VoicePicker, { voiceName } from "../components/VoicePicker";

const blankModel = () => ({ public_name: "", enabled: true, aliases_text: "", voices_text: "", strategy: "", sticky: false, system_prompt: "", max_tokens_cap: "", default_params: "", cache_enabled: false, cache_ttl: "", cache_any_temperature: false, cache_streaming: true, cache_per_key: false, cache_replay_speed: "instant", compress_mode: "", guardrails: null as any, members: [] as any[] });

export default function GatewayEditor() {
  const { id } = useParams();
  const nav = useNavigate();
  const [eps, setEps] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [vBusy, setVBusy] = useState<Record<number, boolean>>({});
  const [vErr, setVErr] = useState<Record<number, string>>({});
  const [cacheBackend, setCacheBackend] = useState("memory");
  const [f, setF] = useState<any>({ name: "", slug: "", strategy: "least_busy", queue_timeout: 60, max_retries: 2, models: [blankModel()] });

  useEffect(() => {
    api("/api/endpoints").then(setEps);
    api("/api/cache/stats").then((c) => setCacheBackend(c.backend === "redis" ? (c.state === "error" ? "redis (down → memory)" : "redis") : "in-memory")).catch(() => {});
    if (id) api(`/api/gateways/${id}`).then((g) => setF({
      ...g, models: g.models.map((m: any) => ({ ...m, aliases_text: (m.aliases || []).join(", "), voices_text: (m.voices || []).join(", "), strategy: m.strategy || "", system_prompt: m.system_prompt || "", max_tokens_cap: m.max_tokens_cap ?? "", cache_ttl: m.cache_ttl ?? "", compress_mode: m.compress_mode || "", guardrails: m.guardrails || null,
        default_params: m.default_params && Object.keys(m.default_params).length ? JSON.stringify(m.default_params, null, 2) : "" })) }));
  }, [id]);

  const allModels = useMemo(() => eps.flatMap((e) => e.models.map((m: any) => ({ ...m, endpoint_name: e.name }))), [eps]);
  const byId = (mid: number) => allModels.find((m) => m.id === mid);
  const setModel = (i: number, patch: any) => setF({ ...f, models: f.models.map((m: any, j: number) => (j === i ? { ...m, ...patch } : m)) });
  const setMember = (i: number, k: number, patch: any) => setModel(i, { members: f.models[i].members.map((x: any, j: number) => (j === k ? { ...x, ...patch } : x)) });

  const refreshVoices = async (i: number, ttsIds: number[]) => {
    setVBusy((b) => ({ ...b, [i]: true })); setVErr((e) => ({ ...e, [i]: "" }));
    let last = "";
    for (const mid of ttsIds) { try { await api(`/api/models/${mid}/voices/refresh`, { method: "POST" }); last = ""; } catch (e: any) { last = e.message; } }
    await api("/api/endpoints").then(setEps);
    setVErr((e) => ({ ...e, [i]: last })); setVBusy((b) => ({ ...b, [i]: false }));
  };

  const save = async () => {
    setErr("");
    let models;
    try {
      models = f.models.map((m: any) => ({
        public_name: m.public_name, enabled: m.enabled !== false, aliases: String(m.aliases_text || "").split(/[,\n]/).map((x: string) => x.trim()).filter(Boolean), voices: String(m.voices_text || "").split(/[,\n]/).map((x: string) => x.trim()).filter(Boolean), strategy: m.strategy || null, sticky: !!m.sticky, system_prompt: m.system_prompt || null,
        max_tokens_cap: m.max_tokens_cap ? +m.max_tokens_cap : null,
        cache_enabled: !!m.cache_enabled, cache_streaming: !!m.cache_streaming, cache_per_key: !!m.cache_per_key, cache_replay_speed: m.cache_replay_speed || "instant", compress_mode: m.compress_mode || null, guardrails: m.guardrails || null, cache_ttl: m.cache_ttl ? +m.cache_ttl : null, cache_any_temperature: !!m.cache_any_temperature,
        default_params: m.default_params?.trim() ? JSON.parse(m.default_params) : {},
        members: m.members.map((x: any) => ({ model_id: x.model_id, weight: +x.weight || 1, priority: +x.priority || 0, slots_override: x.slots_override ? +x.slots_override : null,
          enabled: x.enabled ?? true, is_fallback: !!x.is_fallback, tier: +x.tier || 2 })),
      }));
    } catch (e: any) { setErr(`Default parameters must be valid JSON (${m_(e)})`); return; }
    const body = { name: f.name, slug: f.slug || null, strategy: f.strategy, queue_timeout: +f.queue_timeout, max_retries: +f.max_retries, models };
    try {
      const g = id ? await api(`/api/gateways/${id}`, { method: "PUT", json: body }) : await api("/api/gateways", { method: "POST", json: body });
      nav(`/gateways/${g.id}`);
    } catch (e: any) { setErr(e.message); }
  };
  const m_ = (e: any) => String(e.message || e).slice(0, 80);

  return (
    <div className="fadein">
      <PageHeader back="/gateways" crumb={["Gateways"]} title={id ? "Edit gateway" : "New smart gateway"} sub="Combine upstream models into one endpoint that speaks both the OpenAI and the Anthropic API."
        actions={<><button className="btn" onClick={() => nav(-1)}>Cancel</button><button className="btn primary" onClick={save}><Save size={16} /> Save gateway</button></>} />
      {err && <div className="mb-4 rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>}

      <div className="card card-pad mb-5 grid gap-4 md:grid-cols-2">
        <div><label className="lbl">Gateway name</label><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Production" /></div>
        <div><label className="lbl">URL slug</label><input className="input mono" value={f.slug} onChange={(e) => setF({ ...f, slug: e.target.value })} placeholder="auto from name → /gw/<slug>/v1" /></div>
        <div><label className="lbl">Default routing strategy</label>
          <select className="input" value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value })}>{Object.entries(STRATEGIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
        <div><label className="lbl">Queue timeout (s) when all slots are busy</label><input type="number" className="input" value={f.queue_timeout} onChange={(e) => setF({ ...f, queue_timeout: e.target.value })} /></div>
        <div><label className="lbl">Retries on upstream failure</label><input type="number" className="input" value={f.max_retries} onChange={(e) => setF({ ...f, max_retries: e.target.value })} /></div>
      </div>

      <div className="space-y-5">
        {f.models.map((m: any, i: number) => {
          const caps = new Set<string>(); m.members.forEach((x: any) => byId(x.model_id)?.capabilities.forEach((c: string) => caps.add(c)));
          const used = new Set(m.members.map((x: any) => x.model_id));
          const strat = m.strategy || f.strategy;
          return (
            <div key={i} className="card card-pad" style={m.enabled === false ? { opacity: 0.6 } : undefined}>
              <div className="flex flex-wrap items-end gap-4">
                <div className="min-w-[220px] flex-1"><label className="lbl">Public model name (visible via API)</label>
                  <input className="input mono" value={m.public_name} onChange={(e) => setModel(i, { public_name: e.target.value })} placeholder="smart-1" /></div>
                <div className="min-w-[220px] flex-1"><label className="lbl">Also exposed as (optional, comma separated)</label>
                  <input className="input mono" value={m.aliases_text || ""} onChange={(e) => setModel(i, { aliases_text: e.target.value })} placeholder="gpt-4o, claude-sonnet-4-5" title="Extra names clients can use for this same model. Each one is listed in /v1/models and answers with the name that was asked for." /></div>
                <div className="min-w-[220px]"><label className="lbl">Strategy override</label>
                  <select className="input" value={m.strategy} onChange={(e) => setModel(i, { strategy: e.target.value })}><option value="">Gateway default</option>{Object.entries(STRATEGIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
                <label className="btn cursor-pointer" title="Keep a conversation on the same upstream so its prompt cache is reused"><input type="checkbox" checked={m.sticky} onChange={(e) => setModel(i, { sticky: e.target.checked })} /> Sticky sessions</label>
                <label className="btn cursor-pointer" title="Turn the model off without deleting it: requests for it get 404 and it is hidden from /v1/models. Its settings and upstreams are kept."><input type="checkbox" checked={m.enabled !== false} onChange={(e) => setModel(i, { enabled: e.target.checked })} /> Enabled</label>
                <button className="btn danger" onClick={() => setF({ ...f, models: f.models.filter((_: any, j: number) => j !== i) })}><Trash2 size={15} /></button>
              </div>
              {caps.has("tts") && (() => {
                const ttsIds: number[] = m.members.map((x: any) => x.model_id).filter((mid: number) => byId(mid)?.capabilities.includes("tts"));
                const options = [...new Set<string>(ttsIds.flatMap((mid: number) => (byId(mid)?.voices || []).map(voiceName)).filter(Boolean))];
                return <VoicePicker options={options} text={m.voices_text || ""} onChange={(t) => setModel(i, { voices_text: t })}
                  onRefresh={() => refreshVoices(i, ttsIds)} busy={!!vBusy[i]} err={vErr[i] || ""} />;
              })()}
              <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-[#8e8e9a]">Combined capabilities: {[...caps].map((c) => <CapChip key={c} cap={c} />)}{caps.size === 0 && "—"}</div>

              <table className="mt-4 w-full text-sm">
                <thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]"><th className="pb-2 font-medium">Upstream model</th><th className="pb-2 font-medium">Weight</th><th className="pb-2 font-medium">Priority</th>
                  {strat === "tiered" && <th className="pb-2 font-medium">Tier</th>}<th className="pb-2 font-medium">Slot cap</th><th className="pb-2 font-medium" title="Used only when primaries are down or full for a few seconds">Fallback</th><th /></tr></thead>
                <tbody>
                  {m.members.map((x: any, k: number) => {
                    const um = byId(x.model_id);
                    return (
                      <tr key={k} className="border-t border-white/5">
                        <td className="py-2 pr-3"><div className="mono">{um?.upstream_id} <span className="text-[#787884]">@ {um?.endpoint_name}</span></div>
                          <div className="mt-1 flex flex-wrap gap-1">{um?.capabilities.map((c: string) => <CapChip key={c} cap={c} />)}<span className="chip text-[#8e8e9a]">{um?.max_slots} slots</span>{um?.context_length && <span className="chip text-[#8e8e9a]">ctx {um.context_length}</span>}</div></td>
                        <td className="py-2 pr-3"><input type="number" min={1} className="input !w-20 !py-1.5" value={x.weight} onChange={(e) => setMember(i, k, { weight: e.target.value })} /></td>
                        <td className="py-2 pr-3"><input type="number" className="input !w-20 !py-1.5" value={x.priority} onChange={(e) => setMember(i, k, { priority: e.target.value })} /></td>
                        {strat === "tiered" && <td className="py-2 pr-3"><select className="input !w-28 !py-1.5" value={x.tier ?? 2} onChange={(e) => setMember(i, k, { tier: +e.target.value })}><option value={1}>1 · small</option><option value={2}>2 · medium</option><option value={3}>3 · large</option></select></td>}
                        <td className="py-2 pr-3"><input type="number" min={1} className="input !w-24 !py-1.5" placeholder="model" value={x.slots_override ?? ""} onChange={(e) => setMember(i, k, { slots_override: e.target.value })} /></td>
                        <td className="py-2 pr-3"><input type="checkbox" checked={!!x.is_fallback} onChange={(e) => setMember(i, k, { is_fallback: e.target.checked })} /></td>
                        <td className="py-2 text-right"><button className="btn danger !p-1.5" onClick={() => setModel(i, { members: m.members.filter((_: any, j: number) => j !== k) })}><Trash2 size={14} /></button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="mt-3">
                <select className="input !w-auto" value="" onChange={(e) => e.target.value && setModel(i, { members: [...m.members, { model_id: +e.target.value, weight: 1, priority: 0, enabled: true, tier: 2 }] })}>
                  <option value="">+ Add upstream model…</option>
                  {allModels.filter((um) => !used.has(um.id)).map((um) => <option key={um.id} value={um.id}>{um.upstream_id} @ {um.endpoint_name} [{um.capabilities.join(", ")}]</option>)}
                </select>
              </div>

              <div className="mt-4 rounded-md border border-white/[0.08] p-3">
                <label className="flex cursor-pointer items-center gap-3 text-sm font-medium"><input type="checkbox" checked={!!m.cache_enabled} onChange={(e) => setModel(i, { cache_enabled: e.target.checked })} /> Cache responses for this model
                  <span className="chip text-[#8e8e9a]" style={{ background: "#ffffff0d" }}>backend: {cacheBackend}</span></label>
                {m.cache_enabled && (
                  <div className="mt-3 grid gap-4 md:grid-cols-2">
                    <div><label className="lbl">TTL (seconds)</label><input type="number" min={1} className="input" placeholder="default (see Settings)" value={m.cache_ttl} onChange={(e) => setModel(i, { cache_ttl: e.target.value })} /></div>
                    <label className="flex cursor-pointer items-start gap-3 self-end text-sm"><input type="checkbox" className="mt-1" checked={!!m.cache_any_temperature} onChange={(e) => setModel(i, { cache_any_temperature: e.target.checked })} />
                      <span>Also cache non-deterministic requests<span className="block text-xs text-[#787884]">Off: only requests with temperature 0 (or header X-SG-Cache: 1). X-SG-Cache: 0 bypasses.</span></span></label>
                    <label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={!!m.cache_streaming} onChange={(e) => setModel(i, { cache_streaming: e.target.checked })} />
                      <span>Also cache streaming requests<span className="block text-xs text-[#787884]">A stream is stored once it completes and later replayed to streaming <i>and</i> non-streaming clients. Interrupted streams are never stored.</span></span></label>
                    <label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={!!m.cache_per_key} onChange={(e) => setModel(i, { cache_per_key: e.target.checked })} />
                      <span>Separate cache per API key<span className="block text-xs text-[#787884]">Off: all keys of this gateway share cached answers to identical requests. On: each key only sees answers cached for itself (fewer hits).</span></span></label>
                    {m.cache_streaming && <div><label className="lbl">Replay speed for streaming hits</label>
                      <select className="input" value={m.cache_replay_speed || "instant"} onChange={(e) => setModel(i, { cache_replay_speed: e.target.value })}>
                        <option value="instant">Instant (fastest)</option><option value="realistic">Realistic (same duration as the original answer)</option></select></div>}
                  </div>)}
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-4 rounded-md border border-white/[0.08] p-3">
                <div className="text-sm font-medium">Context compression</div>
                <select className="input !w-auto" value={m.compress_mode || ""} onChange={(e) => setModel(i, { compress_mode: e.target.value })}>
                  <option value="">Use global setting</option><option value="off">Off</option><option value="shadow">Shadow (measure only)</option><option value="on">On</option></select>
                <span className="text-xs text-[#787884]">Shrinks older tool results before they are sent upstream. Global default is set in Settings.</span>
              </div>

              <GuardrailsEditor value={m.guardrails} onChange={(v) => setModel(i, { guardrails: v })} />

              <details className="mt-4 rounded-md border border-white/[0.08] p-3">
                <summary className="cursor-pointer text-sm text-[#8e8e9a]">Request overrides (optional) — pass-through unless set</summary>
                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div className="md:col-span-2"><label className="lbl">System prompt (prepended to every chat request)</label><textarea className="input" rows={2} value={m.system_prompt} onChange={(e) => setModel(i, { system_prompt: e.target.value })} /></div>
                  <div><label className="lbl">Max tokens ceiling</label><input type="number" min={1} className="input" placeholder="no ceiling" value={m.max_tokens_cap} onChange={(e) => setModel(i, { max_tokens_cap: e.target.value })} />
                    <p className="mt-1 text-xs text-[#787884]">Clamps max_tokens / max_completion_tokens; applied as the limit when the client sends none.</p></div>
                  <div><label className="lbl">Default parameters (JSON, only when the client omits them)</label><textarea className="input mono" rows={3} placeholder='{"temperature": 0.2}' value={m.default_params} onChange={(e) => setModel(i, { default_params: e.target.value })} /></div>
                </div>
              </details>
            </div>
          );
        })}
      </div>
      <button className="btn mt-5" onClick={() => setF({ ...f, models: [...f.models, blankModel()] })}><Plus size={16} /> Add public model</button>

      <div className="mt-8">
        {id ? <KeysPanel gwId={+id} slug={f.slug} models={f.models.map((m: any) => m.public_name).filter(Boolean)} />
          : <div className="card card-pad text-sm text-[#8e8e9a]">Save the gateway first, then add API keys with rate limits and quotas here.</div>}
      </div>
    </div>
  );
}
