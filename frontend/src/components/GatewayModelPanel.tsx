import { useEffect, useRef, useState } from "react";
import { Trash2, X } from "lucide-react";
import { STRATEGIES } from "../api";
import { CapChip, StatusDot } from "./ui";
import GuardrailsEditor, { guardrailsActive } from "./GuardrailsEditor";
import VoicePicker, { voiceName } from "./VoicePicker";
import FallbackChain, { buildSteps } from "./FallbackChain";
import AudioSettings, { hasAudioDefaults } from "./AudioSettings";

type TabDef = [string, string];

/** The sections a public model offers depend on what it contains: audio settings only appear with an audio model, fallbacks with two or more
 *  upstream models, chat-only settings (safety, overrides, compression) with a chat model. */
export function visibleTabs(caps: Set<string>, members: number, audio: { tts: boolean; stt: boolean }): TabDef[] {
  const chat = caps.has("chat"), embedding = caps.has("embedding");
  const t: TabDef[] = [["upstreams", "Upstream models"]];
  if (members >= 2) t.push(["fallbacks", "Routing & fallbacks"]);
  if (audio.tts || audio.stt) t.push(["audio", "Audio"]);
  if (chat || embedding) t.push(["cache", chat ? "Cache & compression" : "Cache"]);
  if (chat) t.push(["safety", "Safety"], ["overrides", "Overrides"]);
  return t;
}


function badge(k: string, m: any): { text: string; tone?: "warn" } | null {
  const n = m.members.length;
  if (k === "upstreams") return n ? { text: String(n) } : { text: "none", tone: "warn" };
  if (k === "fallbacks") return { text: `${buildSteps(m.members).length} steps` };
  if (k === "audio") return hasAudioDefaults(m.audio_defaults) || String(m.voices_text || "").trim() ? { text: "set" } : null;
  if (k === "cache") return m.cache_enabled || ["on", "shadow"].includes(m.compress_mode || "") ? { text: "on" } : null;
  if (k === "safety") return guardrailsActive(m.guardrails) ? { text: "on" } : null;
  if (k === "overrides") return m.system_prompt?.trim() || m.max_tokens_cap || m.default_params?.trim() ? { text: "set" } : null;
  return null;
}

export default function GatewayModelPanel({ m, tab, onTab, gwStrategy, maxRetries, byId, allModels, cacheBackend, voices, onChange, setMember, onAdd, onDelete }: {
  m: any; tab: string; onTab: (t: string) => void; gwStrategy: string; maxRetries: number; byId: (id: number) => any; allModels: any[]; cacheBackend: string;
  voices: { busy: boolean; err: string; refresh: (ids: number[]) => void };
  onChange: (patch: any) => void; setMember: (k: number, patch: any) => void; onAdd: (modelId: number, step: number | "new") => void; onDelete: () => void;
}) {
  const caps = new Set<string>(); m.members.forEach((x: any) => byId(x.model_id)?.capabilities.forEach((c: string) => caps.add(c)));
  const used = new Set(m.members.map((x: any) => x.model_id));
  const strat = m.strategy || gwStrategy;
  // decided per upstream model: a TTS model is also tagged "audio" by name, so only an audio model that is not a TTS model counts as speech-to-text
  const memberCaps = m.members.map((x: any) => new Set<string>(byId(x.model_id)?.capabilities || []));
  const audio = { tts: memberCaps.some((c: Set<string>) => c.has("tts")), stt: memberCaps.some((c: Set<string>) => c.has("audio") && !c.has("tts")) };
  const tabs = visibleTabs(caps, m.members.length, audio);
  const keys = tabs.map((t) => t[0]).join(",");
  const current = tabs.some((t) => t[0] === tab) ? tab : tabs[0][0];

  // a tab that appears because of a change made here (a second upstream model, an audio model ...) is marked "new" until it is opened
  const before = useRef<string | null>(null);
  const [fresh, setFresh] = useState<string[]>([]);
  useEffect(() => {
    if (before.current !== null) { const added = keys.split(",").filter((k) => !before.current!.split(",").includes(k)); if (added.length) setFresh((f) => [...new Set([...f, ...added])]); }
    before.current = keys;
  }, [keys]);
  useEffect(() => { setFresh((f) => (f.includes(current) ? f.filter((k) => k !== current) : f)); }, [current]);

  const available = allModels.filter((um) => !used.has(um.id));
  const ttsIds: number[] = m.members.map((x: any) => x.model_id).filter((mid: number) => byId(mid)?.capabilities.includes("tts"));
  const discovered = [...new Set<string>(ttsIds.flatMap((mid: number) => (byId(mid)?.voices || []).map(voiceName)).filter(Boolean))];
  const chosen = String(m.voices_text || "").split(/[,\n]/).map((x: string) => x.trim()).filter(Boolean);
  const voiceChoices = chosen.length ? chosen : discovered;

  return (
    <div className="card card-pad">
      <div className="flex flex-wrap items-end gap-4">
                <div className="min-w-[240px] flex-1"><label className="lbl">Public model name (visible via API)</label>
                  <input className="input mono" value={m.public_name} onChange={(e) => onChange({ public_name: e.target.value })} placeholder="smart-1" /></div>
                <div className="min-w-[240px] flex-1"><label className="lbl">Also exposed as (optional, comma separated)</label>
                  <input className="input mono" value={m.aliases_text || ""} onChange={(e) => onChange({ aliases_text: e.target.value })} placeholder="gpt-4o, claude-sonnet-4-5" title="Extra names clients can use for this same model. Each one is listed in /v1/models and answers with the name that was asked for." /></div>
        <label className="btn cursor-pointer" title="Turn the model off without deleting it: requests for it get 404 and it is hidden from /v1/models. Its settings and upstreams are kept."><input type="checkbox" checked={m.enabled !== false} onChange={(e) => onChange({ enabled: e.target.checked })} /> Enabled</label>
        <button className="btn danger" title="Remove this public model" onClick={onDelete}><Trash2 size={15} /> Remove</button>
      </div>

      <div className="mt-4 flex flex-wrap gap-2" role="tablist" aria-label="Model sections">
        {tabs.map(([k, label]) => {
          const b = badge(k, m);
          return (
            <button key={k} role="tab" aria-selected={current === k} data-testid={`mtab-${k}`} onClick={() => onTab(k)} className={`pill ${current === k ? "on" : ""}`}>
              {label}
              {fresh.includes(k) && current !== k && <span className="ml-1.5 rounded-full px-1.5 text-[11px]" style={{ background: "#4cc38a33", color: "#7fd1a3" }} data-testid={`new-${k}`}>new</span>}
              {b && <span className="ml-1.5 rounded-full px-1.5 text-[11px]" style={{ background: b.tone === "warn" ? "#e0a45833" : "#6c5ce733", color: b.tone === "warn" ? "#e0a458" : "#c9c2ff" }}>{b.text}</span>}
            </button>);
        })}
      </div>

      <div className="mt-5" style={m.enabled === false ? { opacity: 0.6 } : undefined}>
        {current === "upstreams" && (
          <div>
            <p className="mb-3 text-xs text-[#787884]">The upstream models that answer requests for <span className="mono">{m.public_name || "this model"}</span>. {m.members.length < 2 ? "Add a second one to unlock fallbacks and load sharing." : "Arrange their order under Routing & fallbacks."}</p>
            <div className="space-y-2">
              {m.members.map((x: any, k: number) => {
                const um = byId(x.model_id);
                return (
                  <div key={k} className="flex items-center gap-3 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2.5" data-testid={`upstream-${um?.upstream_id}`}>
                    <StatusDot status={um?.endpoint_status || "unknown"} />
                    <div className="min-w-0 flex-1">
                      <div className="mono truncate text-sm">{um?.upstream_id || `model #${x.model_id}`} <span className="text-[#787884]">@ {um?.endpoint_name}</span></div>
                      <div className="mt-1 flex flex-wrap gap-1">{um?.capabilities?.map((c: string) => <CapChip key={c} cap={c} />)}{um?.max_slots && <span className="chip text-[#8e8e9a]">{um.max_slots} slots</span>}{um?.enabled === false && <span className="chip" style={{ color: "#e0a458", background: "#e0a45822" }}>model disabled</span>}</div>
                    </div>
                    <button className="btn danger !p-1.5" title="Remove from this model" onClick={() => onChange({ members: m.members.filter((_: any, j: number) => j !== k) })}><X size={14} /></button>
                  </div>);
              })}
            </div>
            {m.members.length === 0 && <div className="rounded-lg border border-dashed border-white/15 p-4 text-center text-sm text-[#8e8e9a]">No upstream models yet. Add one below to unlock the other settings of this model.</div>}
            {available.length > 0 && (
              <select className="input mt-3 !w-auto" value="" data-testid="add-upstream" onChange={(e) => e.target.value && onAdd(+e.target.value, m.members.length ? "new" : 0)}>
                <option value="">+ Add upstream model…</option>
                {available.map((um) => <option key={um.id} value={um.id}>{um.upstream_id} @ {um.endpoint_name} [{um.capabilities.join(", ")}]</option>)}
              </select>)}
              <div className="mt-4 flex flex-wrap items-center gap-1.5 text-xs text-[#8e8e9a]">Combined capabilities: {[...caps].map((c) => <CapChip key={c} cap={c} />)}{caps.size === 0 && "—"}</div>
          </div>
        )}

        {current === "fallbacks" && (<>
          <div className="flex flex-wrap items-end gap-4">
                <div className="min-w-[240px]"><label className="lbl">Strategy override</label>
                  <select className="input" value={m.strategy} onChange={(e) => onChange({ strategy: e.target.value })}><option value="">Gateway default</option>{Object.entries(STRATEGIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
            <label className="btn cursor-pointer" title="Keep a conversation on the same upstream so its prompt cache is reused"><input type="checkbox" checked={m.sticky} onChange={(e) => onChange({ sticky: e.target.checked })} /> Sticky sessions</label>
          </div>

              <FallbackChain members={m.members} byId={byId} strategy={strat} maxRetries={maxRetries}
                available={allModels.filter((um) => !used.has(um.id))}
                onChange={(members) => onChange({ members })} onUseFailover={() => onChange({ strategy: "priority_failover" })}
                onAdd={(mid, step) => onAdd(mid, step)} />

              {m.members.length > 0 && (
                <details className="mt-3 text-sm">
                  <summary className="cursor-pointer text-xs text-[#8e8e9a] hover:text-[#c8c8d0]">Advanced per-model settings (weight, slot cap{strat === "tiered" ? ", tier" : ""})</summary>
                  <table className="mt-2 w-full text-sm">
                    <thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]"><th className="pb-2 font-medium">Upstream model</th><th className="pb-2 font-medium" title="Share of traffic among models of the same step (weighted round-robin)">Weight</th>
                      {strat === "tiered" && <th className="pb-2 font-medium">Tier</th>}<th className="pb-2 font-medium">Slot cap</th></tr></thead>
                    <tbody>
                      {m.members.map((x: any, k: number) => {
                        const um = byId(x.model_id);
                        return (
                          <tr key={k} className="border-t border-white/5">
                            <td className="py-2 pr-3"><div className="mono">{um?.upstream_id} <span className="text-[#787884]">@ {um?.endpoint_name}</span></div>
                              <div className="mt-1 flex flex-wrap gap-1">{um?.capabilities.map((c: string) => <CapChip key={c} cap={c} />)}<span className="chip text-[#8e8e9a]">{um?.max_slots} slots</span>{um?.context_length && <span className="chip text-[#8e8e9a]">ctx {um.context_length}</span>}</div></td>
                            <td className="py-2 pr-3"><input type="number" min={1} className="input !w-20 !py-1.5" value={x.weight} onChange={(e) => setMember(k, { weight: e.target.value })} /></td>
                            {strat === "tiered" && <td className="py-2 pr-3"><select className="input !w-28 !py-1.5" value={x.tier ?? 2} onChange={(e) => setMember(k, { tier: +e.target.value })}><option value={1}>1 · small</option><option value={2}>2 · medium</option><option value={3}>3 · large</option></select></td>}
                            <td className="py-2 pr-3"><input type="number" min={1} className="input !w-24 !py-1.5" placeholder="model" value={x.slots_override ?? ""} onChange={(e) => setMember(k, { slots_override: e.target.value })} /></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </details>
              )}
        </>)}

        {current === "audio" && (
          <AudioSettings tts={audio.tts} stt={audio.stt} defaults={m.audio_defaults} onChange={(v) => onChange({ audio_defaults: v })} voiceChoices={voiceChoices}
            voicePicker={<VoicePicker options={discovered} text={m.voices_text || ""} onChange={(t) => onChange({ voices_text: t })}
              onRefresh={() => voices.refresh(ttsIds)} busy={voices.busy} err={voices.err} />} />
        )}

        {current === "cache" && (<>
              <div className="rounded-md border border-white/[0.08] p-3">
                <label className="flex cursor-pointer items-center gap-3 text-sm font-medium"><input type="checkbox" checked={!!m.cache_enabled} onChange={(e) => onChange({ cache_enabled: e.target.checked })} /> Cache responses for this model
                  <span className="chip text-[#8e8e9a]" style={{ background: "#ffffff0d" }}>backend: {cacheBackend}</span></label>
                {m.cache_enabled && (
                  <div className="mt-3 grid gap-4 md:grid-cols-2">
                    <div><label className="lbl">TTL (seconds)</label><input type="number" min={1} className="input" placeholder="default (see Settings)" value={m.cache_ttl} onChange={(e) => onChange({ cache_ttl: e.target.value })} /></div>
                    <label className="flex cursor-pointer items-start gap-3 self-end text-sm"><input type="checkbox" className="mt-1" checked={!!m.cache_any_temperature} onChange={(e) => onChange({ cache_any_temperature: e.target.checked })} />
                      <span>Also cache non-deterministic requests<span className="block text-xs text-[#787884]">Off: only requests with temperature 0 (or header X-SG-Cache: 1). X-SG-Cache: 0 bypasses.</span></span></label>
                    <label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={!!m.cache_streaming} onChange={(e) => onChange({ cache_streaming: e.target.checked })} />
                      <span>Also cache streaming requests<span className="block text-xs text-[#787884]">A stream is stored once it completes and later replayed to streaming <i>and</i> non-streaming clients. Interrupted streams are never stored.</span></span></label>
                    <label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={!!m.cache_per_key} onChange={(e) => onChange({ cache_per_key: e.target.checked })} />
                      <span>Separate cache per API key<span className="block text-xs text-[#787884]">Off: all keys of this gateway share cached answers to identical requests. On: each key only sees answers cached for itself (fewer hits).</span></span></label>
                    {m.cache_streaming && <div><label className="lbl">Replay speed for streaming hits</label>
                      <select className="input" value={m.cache_replay_speed || "instant"} onChange={(e) => onChange({ cache_replay_speed: e.target.value })}>
                        <option value="instant">Instant (fastest)</option><option value="realistic">Realistic (same duration as the original answer)</option></select></div>}
                  </div>)}
              </div>

          {caps.has("chat") && (
              <div className="mt-4 flex flex-wrap items-center gap-4 rounded-md border border-white/[0.08] p-3">
                <div className="text-sm font-medium">Context compression</div>
                <select className="input !w-auto" value={m.compress_mode || ""} onChange={(e) => onChange({ compress_mode: e.target.value })}>
                  <option value="">Use global setting</option><option value="off">Off</option><option value="shadow">Shadow (measure only)</option><option value="on">On</option></select>
                <span className="text-xs text-[#787884]">Shrinks older tool results before they are sent upstream. Global default is set in Settings.</span>
              </div>
          )}
        </>)}

        {current === "safety" && (
              <GuardrailsEditor alwaysOpen value={m.guardrails} onChange={(v) => onChange({ guardrails: v })} />
        )}

        {current === "overrides" && (
          <>
            <p className="mb-3 text-xs text-[#787884]">Chat requests only; pass-through unless set.</p>
                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div className="md:col-span-2"><label className="lbl">System prompt (prepended to every chat request)</label><textarea className="input" rows={2} value={m.system_prompt} onChange={(e) => onChange({ system_prompt: e.target.value })} /></div>
                  <div><label className="lbl">Max tokens ceiling</label><input type="number" min={1} className="input" placeholder="no ceiling" value={m.max_tokens_cap} onChange={(e) => onChange({ max_tokens_cap: e.target.value })} />
                    <p className="mt-1 text-xs text-[#787884]">Clamps max_tokens / max_completion_tokens; applied as the limit when the client sends none.</p></div>
                  <div><label className="lbl">Default parameters (JSON, only when the client omits them)</label><textarea className="input mono" rows={3} placeholder='{"temperature": 0.2}' value={m.default_params} onChange={(e) => onChange({ default_params: e.target.value })} /></div>
                </div>
          </>
        )}
      </div>
    </div>
  );
}
