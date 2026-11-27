import VoicePicker, { splitVoices, voiceName } from "./VoicePicker";
import AudioSettings, { hasAudioDefaults } from "./AudioSettings";
import { Tabs, useTab } from "./Tabs";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Copy, KeyRound, Layers, Loader2, Lock, Plus, Save, Trash2 } from "lucide-react";
import { api } from "../api";
import { CapChip, StatusDot } from "./ui";

type Row = { id: number; name: string; slug: string; models: string[]; has_keys: boolean; exposed: boolean; model: string | null; names: string[]; aliases: string[]; aliases_text?: string };

const split = (t?: string) => String(t || "").split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

/** One OpenAI-compatible endpoint in front of several gateways; the model name is the gateway name. */
export default function UnifiedEndpoint({ onChange }: { onChange?: (v: any) => void } = {}) {
  const [enabled, setEnabled] = useState(false);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [dModels, setDModels] = useState<{ model_id: number; name: string; upstream_id: string; endpoint: string; aliases?: string[]; aliases_text?: string; disabled?: boolean; capabilities?: string[]; voices?: string[]; voices_text?: string; voice_options?: string[]; audio_defaults?: any }[]>([]);  // upstream models exposed directly
  const [allModels, setAllModels] = useState<any[]>([]);   // every model of every endpoint, for the picker
  const [pick, setPick] = useState("");
  const [vBusy, setVBusy] = useState<Record<number, boolean>>({});
  const [vErr, setVErr] = useState<Record<number, string>>({});
  const [keys, setKeys] = useState<any[]>([]);
  const [keyName, setKeyName] = useState("");
  const [fresh, setFresh] = useState<any>(null);  // a just-created key: its plaintext is shown once
  const [keyErr, setKeyErr] = useState("");
  const apply = (v: any) => { onChange?.(v); setEnabled(v.enabled); setRows(v.gateways.map((g: any) => ({ ...g, aliases_text: (g.aliases || []).join(", ") }))); setDModels((v.models || []).map((m: any) => ({ ...m, aliases_text: (m.aliases || []).join(", "), voices_text: (m.voices || []).join(", ") }))); setKeys(v.keys || []); setDirty(false); };
  // top-level sections; the sections of a directly exposed model (Upstream model, Audio) work like the ones of a public model in the gateway editor
  const [tab, setTab] = useTab(["endpoint", "gateways", "models", "audio"]);
  const audioKind = (m: { capabilities?: string[] }) => ({ tts: !!m.capabilities?.includes("tts"), stt: !!m.capabilities?.includes("audio") && !m.capabilities?.includes("tts") });
  const [sel, setSel] = useState<number | null>(null);      // the open model
  // all voice settings live on one Audio tab, like the Audio tab of a public model in the gateway editor: a Text-to-speech and a Speech-to-text
  // section. It only appears once a directly exposed model does speech, and is marked "new" when it does.
  const ttsModels = dModels.filter((m) => audioKind(m).tts);
  const sttModels = dModels.filter((m) => audioKind(m).stt);
  const hasAudio = ttsModels.length + sttModels.length > 0;
  const current = tab === "audio" && !hasAudio ? "endpoint" : tab;
  const sawAudio = useRef<boolean | null>(null);
  const [freshAudio, setFreshAudio] = useState(false);
  useEffect(() => { if (!rows) return; if (sawAudio.current === false && hasAudio) setFreshAudio(true); sawAudio.current = hasAudio; }, [hasAudio, rows === null]); // eslint-disable-line   (the first run is the initial load: nothing is "new" then)
  useEffect(() => { if (current === "audio") setFreshAudio(false); }, [current]);
  useEffect(() => {
    api("/api/unified").then(apply).catch(() => {});
    api("/api/endpoints").then((eps: any[]) => setAllModels(eps.flatMap((e) => (e.models || []).filter((m: any) => m.enabled !== false).map((m: any) => ({ model_id: m.id, upstream_id: m.upstream_id, endpoint: e.name, endpoint_status: e.status, capabilities: m.capabilities || [], voice_options: (m.voices || []).map(voiceName).filter(Boolean) }))))).catch(() => {});
  }, []);
  if (!rows) return null;

  const url = `${location.origin}/unified/v1`;
  const patch = (id: number, p: Partial<Row>) => { setRows(rows.map((r) => (r.id === id ? { ...r, ...p } : r))); setDirty(true); };
  const exposed = rows.filter((r) => r.exposed);
  const firstName = exposed[0]?.name || dModels[0]?.name || "My Gateway";
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const v = await api("/api/unified", { method: "PUT", json: { enabled, gateways: exposed.map((r) => ({ id: r.id, model: r.model, aliases: split(r.aliases_text) })), models: dModels.map((m) => ({ model_id: m.model_id, name: m.name, aliases: split(m.aliases_text) , disabled: !!m.disabled, voices: splitVoices(m.voices_text || ""), audio_defaults: m.audio_defaults || null })) } });
      apply(v); setMsg({ ok: true, text: "Saved. Changes apply within a few seconds." });
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
    setBusy(false);
  };
  const refreshVoices = async (modelId: number) => {
    setVBusy((b) => ({ ...b, [modelId]: true })); setVErr((e) => ({ ...e, [modelId]: "" }));
    try {
      const m = await api(`/api/models/${modelId}/voices/refresh`, { method: "POST" });
      setDModels((list) => list.map((x) => (x.model_id === modelId ? { ...x, voice_options: (m.voices || []).map((v: any) => (typeof v === "string" ? v : String(v?.id ?? v?.voice_id ?? v?.name ?? ""))).filter(Boolean) } : x)));
    } catch (e: any) { setVErr((x) => ({ ...x, [modelId]: e.message })); }
    setVBusy((b) => ({ ...b, [modelId]: false }));
  };
  const addKey = async () => {
    setKeyErr("");
    try {
      const k = await api("/api/unified/keys", { method: "POST", json: { name: keyName.trim() } });
      setFresh(k); setKeyName(""); setKeys((x) => [...x, k]);
    } catch (e: any) { setKeyErr(e.message); }
  };
  const delKey = async (k: any) => {
    if (!confirm(`Delete the key "${k.name}"? Clients using it stop working immediately.${keys.length === 1 ? " This is the last key, so the endpoint will accept calls without a unified key again." : ""}`)) return;
    await api(`/api/unified/keys/${k.id}`, { method: "DELETE" });
    setKeys((x) => x.filter((y) => y.id !== k.id)); if (fresh?.id === k.id) setFresh(null);
  };
  const addModel = () => {
    const m = allModels.find((x) => String(x.model_id) === pick);
    if (!m) return;
    let name = m.upstream_id, n = 2;
    while (dModels.some((x) => x.name.toLowerCase() === name.toLowerCase())) name = `${m.upstream_id}-${n++}`;
    setDModels([...dModels, { ...m, name }]); setSel(m.model_id); setPick(""); setDirty(true);
  };
  const copy = () => { navigator.clipboard?.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500); };

  return (
    <div className="card card-pad mb-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h3 className="flex items-center gap-2 text-base font-semibold"><Layers size={17} /> Unified endpoint</h3>
          <span className="chip" style={enabled ? { color: "#5fb585", background: "#5fb58522" } : { color: "#8e8e9a", background: "#8e8e9a22" }}>{enabled ? "Enabled" : "Disabled"}</span>
        </div>
        <div className="flex items-center gap-3">
          {dirty && <span className="chip" style={{ color: "#e0a458", background: "#e0a45822" }} data-testid="unsaved">Unsaved changes</span>}
          <button className="btn primary" onClick={save} disabled={busy || !dirty}>{busy ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} Save</button>
        </div>
      </div>
      {msg && <div className={`mb-4 rounded-md border p-2.5 text-sm ${msg.ok ? "border-[#5fb585]/30 bg-[#5fb585]/10 text-[#7fd1a3]" : "border-red-400/30 bg-red-400/10 text-red-300"}`}>{msg.text}</div>}

      <Tabs value={current} onChange={setTab} items={[
        ["endpoint", "Endpoint"],
        ["gateways", "Gateways", <span key="g" className="ml-1.5 rounded-full px-1.5 text-[11px]" style={{ background: "#ffffff14", color: "#a9a9b4" }}>{exposed.length}/{rows.length}</span>],
        ["models", "Models", <span key="m" className="ml-1.5 rounded-full px-1.5 text-[11px]" style={{ background: "#ffffff14", color: "#a9a9b4" }}>{dModels.length}</span>],
        ...(hasAudio ? [["audio", "Audio", freshAudio && current !== "audio" ? <span key="a" className="ml-1.5 rounded-full px-1.5 text-[11px]" style={{ background: "#4cc38a33", color: "#7fd1a3" }} data-testid="new-audio">new</span> : null] as [string, string, React.ReactNode]] : []),
      ]} />

      {current === "endpoint" && (
        <div>
      <p className="mb-5 max-w-3xl text-sm text-[#8e8e9a]">
        Offer several gateways through one OpenAI-compatible URL. Clients pick a gateway by its <b className="font-medium text-[#c8c8d0]">name</b> in the <span className="mono">model</span> field.
        Each gateway keeps its own routing, cache, limits and API keys: use the gateway's key as the Bearer token (open gateways need none).
      </p>

      <label className="mb-4 flex cursor-pointer items-center gap-3 text-sm">
        <input type="checkbox" checked={enabled} onChange={(e) => { setEnabled(e.target.checked); setDirty(true); }} />
        <span>Enable the unified endpoint</span>
      </label>

      <label className="lbl">Endpoint URL</label>
      <div className="mb-5 flex gap-2">
        <input readOnly className="input mono" value={url} onFocus={(e) => e.currentTarget.select()} />
        <button className="btn whitespace-nowrap" onClick={copy}>{copied ? <Check size={15} /> : <Copy size={15} />} {copied ? "Copied" : "Copy"}</button>
      </div>

      <label className="lbl mt-6 flex items-center gap-1.5"><KeyRound size={12} /> API keys</label>
      <p className="mb-3 max-w-3xl text-xs text-[#787884]">
        {keys.length === 0
          ? "No key set: callers authenticate against each gateway (open gateways need none, protected ones need their own key). Create a key to require authentication for the whole endpoint."
          : "A key is required. A unified key works for every exposed gateway, so clients don't need each gateway's own key. A gateway's own key still works for that gateway only."}
      </p>
      {fresh && (
        <div className="mb-3 rounded-md border border-[#5fb585]/30 bg-[#5fb585]/10 p-3 text-sm">
          <div className="mb-1.5 text-[#7fd1a3]">Key "{fresh.name}" created. Copy it now, it will not be shown again.</div>
          <div className="flex gap-2"><input readOnly className="input mono" value={fresh.key} onFocus={(e) => e.currentTarget.select()} />
            <button className="btn whitespace-nowrap" onClick={() => navigator.clipboard?.writeText(fresh.key)}><Copy size={15} /> Copy</button>
            <button className="btn" onClick={() => setFresh(null)}>Done</button></div>
        </div>
      )}
      {keys.length > 0 && (
        <div className="mb-3 divide-y divide-white/5 rounded-md border border-white/5">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center gap-4 px-3.5 py-2.5 text-sm">
              <span className="min-w-[140px] font-medium">{k.name}</span>
              <span className="mono text-xs text-[#8e8e9a]">{k.prefix}</span>
              <span className="flex-1 text-xs text-[#787884]">created {new Date(k.created_at * 1000).toLocaleDateString()}</span>
              <button className="btn danger !px-2 !py-1" title="Delete key" onClick={() => delKey(k)}><Trash2 size={14} /></button>
            </div>))}
        </div>
      )}
      <div className="flex gap-2">
        <input className="input max-w-xs" placeholder="Key name, e.g. team-a, ci" value={keyName} maxLength={60} onChange={(e) => setKeyName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && keyName.trim() && addKey()} />
        <button className="btn whitespace-nowrap" onClick={addKey} disabled={!keyName.trim()}><Plus size={15} /> Create key</button>
      </div>
      {keyErr && <div className="mt-2 text-sm text-red-300">{keyErr}</div>}

      {(exposed.length > 0 || dModels.length > 0) && (
        <pre className="mono scroll-thin mt-5 overflow-x-auto rounded-md bg-black/40 p-3 text-[11px] leading-relaxed text-[#a79cff]">{`curl ${url}/chat/completions \\
  -H "Authorization: Bearer <${keys.length ? "unified API key" : "gateway API key, if the gateway has one"}>" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${firstName}",
       "messages":[{"role":"user","content":"Hi"}]}'

# list what the caller may use:  GET ${url}/models`}</pre>
      )}
        </div>
      )}

      {current === "gateways" && (
        <div>
      <label className="lbl">Gateways to expose <span className="font-normal normal-case text-[#787884]">· untick a gateway to stop serving it here; models disabled inside a gateway are skipped automatically</span></label>
      {rows.length === 0 ? (
        <div className="rounded-md border border-white/5 p-4 text-sm text-[#8e8e9a]">No gateways yet. <Link to="/gateways/new" className="text-[#a79cff] hover:underline">Create one</Link> first.</div>
      ) : (
        <div className="divide-y divide-white/5 rounded-md border border-white/5">
          {rows.map((r) => (
            <div key={r.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3.5 py-3">
              <label className="flex min-w-[220px] flex-1 cursor-pointer items-center gap-3 text-sm">
                <input type="checkbox" checked={r.exposed} disabled={r.models.length === 0} onChange={(e) => patch(r.id, { exposed: e.target.checked })} />
                <span className="font-medium">{r.name}</span>
                <span className="mono text-xs text-[#787884]">{r.slug}</span>
                {r.has_keys
                  ? <span className="chip" style={{ color: "#c2a05a", background: "#c2a05a22" }}><Lock size={11} /> API key</span>
                  : <span className="chip" style={{ color: "#8e8e9a", background: "#8e8e9a22" }}>Open</span>}
              </label>
              {r.models.length === 0 && <span className="text-xs text-[#787884]">no enabled models</span>}
              {r.exposed && r.models.length > 1 && (
                <div className="flex items-center gap-2 text-xs text-[#8e8e9a]">
                  <span>Default model</span>
                  <select className="input !w-auto !py-1" value={r.model || ""} onChange={(e) => patch(r.id, { model: e.target.value })}>
                    {r.models.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </div>
              )}
              {r.exposed && (
                <div className="flex w-full flex-wrap items-center gap-1.5 pl-7 text-xs text-[#787884]">
                  model name{r.models.length > 1 ? "s" : ""}:
                  {(dirty ? [r.name, ...split(r.aliases_text), ...(r.models.length > 1 ? r.models.map((m) => `${r.name}/${m}`) : [])] : r.names).map((n) => <span key={n} className="chip mono" style={{ color: "#a79cff", background: "#6c5ce722" }}>{n}</span>)}
                  <input className="input mono !w-auto min-w-[220px] flex-1 !py-1 text-xs" value={r.aliases_text || ""} placeholder="also exposed as (optional): gpt-4o, claude-sonnet"
                    title="Extra names clients can use for this gateway's default model" onChange={(e) => patch(r.id, { aliases_text: e.target.value })} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <p className="mt-2 text-xs text-[#787884]">A gateway with several public models is reachable as <span className="mono">name</span> (its default model) and as <span className="mono">name/model</span>. If two gateways share a name, the slug is appended to the second. Voices offered by a text-to-speech model of a gateway are chosen in that gateway's editor.</p>
        </div>
      )}

      {current === "models" && (() => {
        const cur = dModels.find((m) => m.model_id === sel) || dModels[0];
        const patchM = (id: number, p: any) => { setDModels(dModels.map((x) => (x.model_id === id ? { ...x, ...p } : x))); setDirty(true); };
        const addable = allModels.filter((m) => !dModels.some((x) => x.model_id === m.model_id));
        const up = cur ? allModels.find((x) => x.model_id === cur.model_id) : null;
        return (
          <div>
            <p className="mb-3 max-w-3xl text-xs text-[#787884]">Expose a single model of one of your endpoints directly, without a gateway around it. Clients use the name you give it in the <span className="mono">model</span> field.</p>
            <div className="mb-4 flex flex-wrap items-center gap-2" role="tablist" aria-label="Directly exposed models">
              {dModels.map((m) => (
                <button key={m.model_id} role="tab" aria-selected={cur?.model_id === m.model_id} data-testid={`dmodel-pill-${m.model_id}`} onClick={() => setSel(m.model_id)}
                  className={`pill ${cur?.model_id === m.model_id ? "on" : ""}`} style={m.disabled ? { opacity: 0.6 } : undefined}>
                  <span className="mono">{m.name || "unnamed"}</span>
                </button>))}
              <select className="input !w-auto !py-1.5" value={pick} onChange={(e) => setPick(e.target.value)} data-testid="add-direct-select">
                <option value="">Add a model…</option>
                {addable.map((m) => <option key={m.model_id} value={m.model_id}>{m.upstream_id} @ {m.endpoint}</option>)}
              </select>
              <button className="btn !py-1.5" onClick={() => { addModel(); }} disabled={!pick} data-testid="add-direct"><Plus size={15} /> Add model</button>
            </div>

            {cur ? (
              <div className="card card-pad" key={cur.model_id} data-testid="direct-panel">
                <div className="flex flex-wrap items-end gap-4">
                  <div className="min-w-[240px] flex-1"><label className="lbl">Public model name (visible via API)</label>
                    <input className="input mono" value={cur.name} maxLength={120} onChange={(e) => patchM(cur.model_id, { name: e.target.value })} placeholder="my-model" /></div>
                  <div className="min-w-[240px] flex-1"><label className="lbl">Also exposed as (optional, comma separated)</label>
                    <input className="input mono" value={cur.aliases_text || ""} onChange={(e) => patchM(cur.model_id, { aliases_text: e.target.value })} placeholder="gpt-4o, claude-sonnet-4-5"
                      title="Extra names clients can use for this same model. Each one is listed in /v1/models and answers with the name that was asked for." /></div>
                  <label className="btn cursor-pointer" title="Turn the model off without removing it: requests for it get 404 and it is hidden from /models."><input type="checkbox" checked={!cur.disabled} onChange={(e) => patchM(cur.model_id, { disabled: !e.target.checked })} /> Enabled</label>
                  <button className="btn danger" title="Stop exposing this model" onClick={() => { setDModels(dModels.filter((x) => x.model_id !== cur.model_id)); setDirty(true); }}><Trash2 size={15} /> Remove</button>
                </div>

                <div className="mt-5" style={cur.disabled ? { opacity: 0.6 } : undefined}>
                  {(
                    <div>
                      <p className="mb-3 text-xs text-[#787884]">The upstream model behind <span className="mono">{cur.name || "this name"}</span>. It is served directly, so there are no fallbacks: to spread requests over several models, expose a gateway instead.</p>
                      <div className="flex items-center gap-3 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2.5" data-testid={`upstream-${cur.upstream_id}`}>
                        <StatusDot status={up?.endpoint_status || "unknown"} />
                        <div className="min-w-0 flex-1">
                          <div className="mono truncate text-sm">{cur.upstream_id} <span className="text-[#787884]">@ {cur.endpoint}</span></div>
                          <div className="mt-1 flex flex-wrap gap-1">{cur.capabilities?.map((c) => <CapChip key={c} cap={c} />)}</div>
                        </div>
                      </div>
                    </div>
                  )}
                  {(audioKind(cur).tts || audioKind(cur).stt) && <p className="mt-3 text-xs text-[#787884]">Its voices and audio defaults are on the Audio tab.</p>}
                </div>
              </div>
            ) : <div className="card card-pad text-center text-sm text-[#8e8e9a]">No directly exposed models yet. Pick one above to expose it under its own name.</div>}
          </div>
        );
      })()}

      {current === "audio" && (
        <div className="space-y-8" data-testid="audio-tab">
          <p className="text-xs text-[#787884]">Defaults are used only when a request leaves the setting out; a client's own value always wins.</p>
          {ttsModels.length > 0 && (
            <section data-testid="tts-section">
              <h4 className="mb-3 text-sm font-semibold">Text-to-speech</h4>
              <div className="space-y-4">
                {ttsModels.map((m) => {
                  const chosen = splitVoices(m.voices_text || "");
                  return (
                    <div key={m.model_id} className="rounded-lg border border-white/10 p-4" data-testid={`audio-card-${m.model_id}`}>
                      <div className="mb-4 flex flex-wrap items-baseline gap-2"><span className="mono text-sm font-semibold">{m.name}</span>
                        <span className="text-xs text-[#787884]"><span className="mono">{m.upstream_id}</span> @ {m.endpoint}</span>
                        {m.disabled && <span className="chip" style={{ color: "#e0a458", background: "#e0a45822" }}>disabled</span>}
                        {(hasAudioDefaults(m.audio_defaults) || chosen.length > 0) && <span className="chip" style={{ color: "#c9c2ff", background: "#6c5ce733" }}>set</span>}</div>
                      <AudioSettings bare tts stt={false} defaults={m.audio_defaults} voiceChoices={chosen.length ? chosen : (m.voice_options || [])}
                        onChange={(v) => { setDModels(dModels.map((x) => (x.model_id === m.model_id ? { ...x, audio_defaults: v } : x))); setDirty(true); }}
                        voicePicker={<VoicePicker options={m.voice_options || []} text={m.voices_text || ""} busy={!!vBusy[m.model_id]} err={vErr[m.model_id] || ""}
                          onChange={(t) => { setDModels(dModels.map((x) => (x.model_id === m.model_id ? { ...x, voices_text: t } : x))); setDirty(true); }} onRefresh={() => refreshVoices(m.model_id)} />} />
                    </div>);
                })}
              </div>
            </section>)}
          {sttModels.length > 0 && (
            <section data-testid="stt-section">
              <h4 className="mb-3 text-sm font-semibold">Speech-to-text</h4>
              <div className="space-y-4">
                {sttModels.map((m) => (
                  <div key={m.model_id} className="rounded-lg border border-white/10 p-4" data-testid={`audio-card-${m.model_id}`}>
                    <div className="mb-4 flex flex-wrap items-baseline gap-2"><span className="mono text-sm font-semibold">{m.name}</span>
                      <span className="text-xs text-[#787884]"><span className="mono">{m.upstream_id}</span> @ {m.endpoint}</span>
                      {m.disabled && <span className="chip" style={{ color: "#e0a458", background: "#e0a45822" }}>disabled</span>}
                      {hasAudioDefaults(m.audio_defaults) && <span className="chip" style={{ color: "#c9c2ff", background: "#6c5ce733" }}>set</span>}</div>
                    <AudioSettings bare tts={false} stt defaults={m.audio_defaults} voiceChoices={[]}
                      onChange={(v) => { setDModels(dModels.map((x) => (x.model_id === m.model_id ? { ...x, audio_defaults: v } : x))); setDirty(true); }} />
                  </div>))}
              </div>
            </section>)}
        </div>
      )}
    </div>
  );
}
