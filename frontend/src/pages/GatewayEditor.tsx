import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Plus, Save } from "lucide-react";
import { api, gwUrl, STRATEGIES } from "../api";
import { PageHeader } from "../components/ui";
import { Tabs, useTab } from "../components/Tabs";
import KeysPanel from "../components/KeysPanel";
import GatewayModelPanel from "../components/GatewayModelPanel";
import { applySteps, buildSteps } from "../components/FallbackChain";

const blankModel = () => ({ public_name: "", enabled: true, aliases_text: "", voices_text: "", audio_defaults: null as any, strategy: "", sticky: false, system_prompt: "", max_tokens_cap: "", default_params: "", cache_enabled: false, cache_ttl: "", cache_any_temperature: false, cache_streaming: true, cache_per_key: false, cache_replay_speed: "instant", compress_mode: "", guardrails: null as any, members: [] as any[] });

export default function GatewayEditor() {
  const { id } = useParams();
  const nav = useNavigate();
  const [eps, setEps] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [vBusy, setVBusy] = useState<Record<number, boolean>>({});
  const [vErr, setVErr] = useState<Record<number, string>>({});
  const [cacheBackend, setCacheBackend] = useState("memory");
  const [f, setF] = useState<any>({ name: "", slug: "", strategy: "least_busy", queue_timeout: 60, max_retries: 2, models: [blankModel()] });
  const [tab, setTab] = useTab(["general", "models"], id ? "models" : "general");   // page sections, kept in the URL (?tab=...)
  const [sel, setSel] = useState(0);            // which public model is open
  const [mtab, setMtab] = useState("routing");  // which section of it

  useEffect(() => {
    api("/api/endpoints").then(setEps);
    api("/api/cache/stats").then((c) => setCacheBackend(c.backend === "redis" ? (c.state === "error" ? "redis (down → memory)" : "redis") : "in-memory")).catch(() => {});
    if (id) api(`/api/gateways/${id}`).then((g) => setF({
      ...g, models: g.models.map((m: any) => ({ ...m, aliases_text: (m.aliases || []).join(", "), voices_text: (m.voices || []).join(", "), strategy: m.strategy || "", system_prompt: m.system_prompt || "", max_tokens_cap: m.max_tokens_cap ?? "", cache_ttl: m.cache_ttl ?? "", compress_mode: m.compress_mode || "", guardrails: m.guardrails || null,
        default_params: m.default_params && Object.keys(m.default_params).length ? JSON.stringify(m.default_params, null, 2) : "" })) }));
  }, [id]);

  const allModels = useMemo(() => eps.flatMap((e) => e.models.map((m: any) => ({ ...m, endpoint_name: e.name, endpoint_status: e.status }))), [eps]);
  const byId = (mid: number) => allModels.find((m) => m.id === mid);
  const setModel = (i: number, patch: any) => setF({ ...f, models: f.models.map((m: any, j: number) => (j === i ? { ...m, ...patch } : m)) });
  const setMember = (i: number, k: number, patch: any) => setModel(i, { members: f.models[i].members.map((x: any, j: number) => (j === k ? { ...x, ...patch } : x)) });

  /** Add a model to step `step` of a gateway model's chain (or as a new last step); the chain keeps priorities and fallback flags consistent. */
  const addMember = (i: number, modelId: number, step: number | "new") => {
    const ms = f.models[i].members, idx = ms.length;
    const members = [...ms, { model_id: modelId, weight: 1, priority: 0, is_fallback: false, enabled: true, tier: 2 }];
    const steps = buildSteps(ms).map((s) => ({ ...s, idxs: [...s.idxs] }));
    if (!steps.length) steps.push({ hold: false, idxs: [idx] });
    else if (step === "new") steps.push({ hold: true, idxs: [idx] });
    else steps[step].idxs.push(idx);
    setModel(i, { members: applySteps(members, steps) });
  };

  const addModel = () => { setF({ ...f, models: [...f.models, blankModel()] }); setSel(f.models.length); setMtab("routing"); setTab("models"); };
  const removeModel = (i: number) => {
    const m = f.models[i];
    if (m.members.length && !confirm(`Remove the public model "${m.public_name || "unnamed"}" and its ${m.members.length} upstream model${m.members.length === 1 ? "" : "s"}? It is only removed when you save.`)) return;
    setF({ ...f, models: f.models.filter((_: any, j: number) => j !== i) });
    setSel(Math.max(0, Math.min(sel, f.models.length - 2)));
  };

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
        audio_defaults: m.audio_defaults || null,
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

  const si = Math.min(sel, f.models.length - 1);
  const model = f.models[si];
  const nameMissing = !f.name.trim();
  return (
    <div className="fadein">
      <PageHeader back="/gateways" crumb={["Gateways"]} title={id ? "Edit gateway" : "New smart gateway"} sub="Combine upstream models into one endpoint that speaks both the OpenAI and the Anthropic API."
        actions={<><button className="btn" onClick={() => nav(-1)}>Cancel</button><button className="btn primary" onClick={save}><Save size={16} /> Save gateway</button></>} />
      {err && <div className="mb-4 rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>}

      <Tabs items={[["general", `Gateway${nameMissing ? " ⚠" : ""}`], ["models", `Models (${f.models.length})`]]} value={tab} onChange={setTab} />

      {tab === "general" && (
        <>
        <div className="card card-pad grid gap-4 md:grid-cols-2">
          <div><label className="lbl">Gateway name</label><input className="input" aria-invalid={nameMissing} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Production" /></div>
          <div><label className="lbl">URL slug</label><input className="input mono" value={f.slug} onChange={(e) => setF({ ...f, slug: e.target.value })} placeholder="auto from name → /gw/<slug>/v1" />
            {f.slug && <div className="mt-1 text-xs text-[#787884]">Clients use <span className="mono">{gwUrl(f.slug)}</span></div>}</div>
          <div><label className="lbl">Default routing strategy</label>
            <select className="input" value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value })}>{Object.entries(STRATEGIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
            <div className="mt-1 text-xs text-[#787884]">Each public model can override it under Models → Routing & fallbacks.</div></div>
          <div><label className="lbl">Queue timeout (s) when all slots are busy</label><input type="number" className="input" value={f.queue_timeout} onChange={(e) => setF({ ...f, queue_timeout: e.target.value })} /></div>
          <div><label className="lbl">Retries on upstream failure</label><input type="number" className="input" value={f.max_retries} onChange={(e) => setF({ ...f, max_retries: e.target.value })} /></div>
        </div>

        <div className="mt-6">
          {id ? <KeysPanel gwId={+id} slug={f.slug} models={f.models.map((m: any) => m.public_name).filter(Boolean)} />
            : <div className="card card-pad text-sm text-[#8e8e9a]">Save the gateway first, then add API keys with rate limits and quotas here.</div>}
        </div>
        </>
      )}

      {tab === "models" && (
        <div>
          <div className="mb-4 flex flex-wrap items-center gap-2" role="tablist" aria-label="Public models">
            {f.models.map((m: any, i: number) => {
              const warn = !m.public_name.trim() || !m.members.length;
              return (
                <button key={i} role="tab" aria-selected={i === si} data-testid={`model-pill-${i}`} onClick={() => setSel(i)} className={`pill ${i === si ? "on" : ""}`} style={m.enabled === false ? { opacity: 0.6 } : undefined}
                  title={warn ? (!m.public_name.trim() ? "This model has no name yet" : "This model has no upstream models yet") : undefined}>
                  <span className="mono">{m.public_name || "unnamed"}</span>
                  <span className="ml-1.5 rounded-full px-1.5 text-[11px]" style={{ background: warn ? "#e0a45833" : "#ffffff14", color: warn ? "#e0a458" : "#a9a9b4" }}>{m.members.length}</span>
                </button>);
            })}
            <button className="btn !py-1.5" onClick={addModel} data-testid="add-model"><Plus size={15} /> Add public model</button>
          </div>
          {model
            ? <GatewayModelPanel key={si} m={model} tab={mtab} onTab={setMtab} gwStrategy={f.strategy} maxRetries={+f.max_retries || 0} byId={byId} allModels={allModels} cacheBackend={cacheBackend}
                voices={{ busy: !!vBusy[si], err: vErr[si] || "", refresh: (ids) => refreshVoices(si, ids) }}
                onChange={(patch) => setModel(si, patch)} setMember={(k, patch) => setMember(si, k, patch)} onAdd={(mid, step) => addMember(si, mid, step)} onDelete={() => removeModel(si)} />
            : <div className="card card-pad text-center text-sm text-[#8e8e9a]">No public models yet. Add one to expose upstream models under your own name.</div>}
        </div>
      )}
    </div>
  );
}
