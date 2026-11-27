import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Database, FlaskConical, ImagePlus, Loader2, Send, Square, Trash2, XCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { adminGwUrl, api, unifiedUrl } from "../api";
import { chatCompletion } from "../lib/chatStream";
import { CapChip, ms, PageHeader } from "../components/ui";
import FlowGraph from "../components/FlowGraph";
import CouncilTabs from "../components/CouncilTabs";
import { loadCouncilFlow } from "../lib/councilFlow";
import AudioPlayground from "../components/AudioPlayground";

type Msg = { role: "user" | "assistant"; text: string; image?: string; reasoning?: string; meta?: any; error?: string; council?: any };

export default function Playground() {
  const [gws, setGws] = useState<any[]>([]);
  const [gid, setGid] = useState<number | null>(null);
  const [model, setModel] = useState("");
  const [system, setSystem] = useState("");
  const [temp, setTemp] = useState(0.7);
  const [maxTok, setMaxTok] = useState(1024);
  const [stream, setStream] = useState(true);
  const [pmode, setPmode] = useState("");   // "" = automatic; chat | tts | stt once chosen
  const [useHistory, setUseHistory] = useState(true);  // false: every message is sent as a separate single-turn request
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [image, setImage] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const [cacheMode, setCacheMode] = useState<"default" | "force" | "bypass">("default");
  const [cstats, setCstats] = useState<any>(null);
  const [ctest, setCtest] = useState<any>(null);
  const [ctesting, setCtesting] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  const [token, setToken] = useState("");
  useEffect(() => {
    Promise.all([api("/api/gateways"), api("/api/unified").catch(() => null), api("/api/gateways?all=true").catch(() => [])]).then(([g, uni, all]) => {
      let list = g;
      if (uni?.enabled) {
        // flow-graph shape of the unified endpoint: the models of the exposed gateways plus the hidden internal one, tagged with their gateway
        const exposed: any[] = (uni.gateways || []).filter((r: any) => r.exposed);
        const picked = all.filter((x: any) => x.slug === "__unified__" ? (uni.models || []).length > 0 : exposed.some((r) => r.id === x.id));
        const flow = {
          id: "unified", slug: "unified", name: "Unified endpoint", strategy: "least_busy", gwIds: picked.map((x: any) => x.id),
          models: picked.flatMap((x: any) => x.models.filter((m: any) => m.enabled !== false).map((m: any) => ({ ...m, gw_id: x.id, label: x.slug === "__unified__" ? m.public_name : picked.length > 1 ? `${x.name}: ${m.public_name}` : m.public_name, strategy: m.strategy || x.strategy }))),
        };
        list = [...g, { id: -1, unified: true, flow, name: "Unified endpoint", models: (uni.model_names || []).map((n: string) => ({ id: n, public_name: n, capabilities: uni.model_abilities?.[n]?.capabilities || [], abilities: uni.model_abilities?.[n], members: [] })) }];
      }
      setGws(list);
      if (list[0]) { setGid(list[0].id); setModel((list[0].models.find((m: any) => m.enabled !== false) || list[0].models[0])?.public_name || ""); }
    });
  }, []);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs]);
  const gw = gws.find((g) => g.id === gid);
  const loadCache = () => api("/api/cache/stats").then(setCstats).catch(() => {});
  useEffect(() => { loadCache(); const t = setInterval(loadCache, 4000); return () => clearInterval(t); }, []);
  const baseUrl = () => (gw?.unified ? unifiedUrl() : adminGwUrl(gw.slug));
  const authHeaders = (): Record<string, string> => (gw?.unified && token ? { Authorization: `Bearer ${token}` } : {});
  const cacheHeaders = (): Record<string, string> => ({ ...authHeaders(), ...(gw?.kind === "council" ? { "X-SG-Council-Details": "1" } : {}), ...(cacheMode === "force" ? { "X-SG-Cache": "1" } : cacheMode === "bypass" ? { "X-SG-Cache": "0" } : {}) });
  const [cflow, setCflow] = useState<any>(null);
  useEffect(() => { setCflow(null); if (gw?.kind === "council") loadCouncilFlow(gw).then(setCflow).catch(() => {}); }, [gw]);
  const gm = gw?.models.find((m: any) => m.public_name === model);
  const caps: string[] = gm?.capabilities || [];
  // what the selected model can do decides the modes: chat, text to speech, speech to text (a model that does none of them stays on chat)
  const ab = gm?.abilities || { chat: caps.includes("chat") || caps.length === 0, tts: caps.includes("tts"), stt: caps.includes("audio") && !caps.includes("tts") };
  const modes: [string, string][] = [...(ab.chat ? [["chat", "Chat"] as [string, string]] : []), ...(ab.tts ? [["tts", "Text to speech"] as [string, string]] : []), ...(ab.stt ? [["stt", "Speech to text"] as [string, string]] : [])];
  if (!modes.length) modes.push(["chat", "Chat"]);
  // opens on speech when the model does speech (a transcription model is also tagged "chat" by name-based detection); a choice made with the pills is kept
  const cmode = modes.some((m) => m[0] === pmode) ? pmode : (modes.find((m) => m[0] !== "chat") || modes[0])[0];

  const pickImage = (file?: File) => { if (!file) return; const r = new FileReader(); r.onload = () => setImage(r.result as string); r.readAsDataURL(file); };

  const send = async () => {
    if (!gw || !model || (!input.trim() && !image) || busy) return;
    const user: Msg = { role: "user", text: input, image };
    const history = [...msgs, user];
    setMsgs([...history, { role: "assistant", text: "" }]);
    setInput(""); setImage(undefined); setBusy(true);
    const apiMsgs: any[] = [];
    if (system.trim()) apiMsgs.push({ role: "system", content: system });
    (useHistory ? history : [user]).forEach((m) => apiMsgs.push({ role: m.role, content: m.image ? [{ type: "text", text: m.text || "Describe this image." }, { type: "image_url", image_url: { url: m.image } }] : m.text }));
    const ac = new AbortController(); abort.current = ac;
    const patch = (p: Partial<Msg>) => setMsgs((ms_) => ms_.map((m, i) => (i === ms_.length - 1 ? { ...m, ...p } : m)));
    const t0 = performance.now();
    const { error } = await chatCompletion(
      { url: `${baseUrl()}/chat/completions`, signal: ac.signal, headers: cacheHeaders(), body: { model, messages: apiMsgs, temperature: temp, max_tokens: maxTok, stream } },
      (meta) => patch({ meta: { ...meta } }),
      (u) => patch({ text: u.text ?? "", reasoning: u.reasoning, ...(u.council ? { council: u.council } : {}) }),
    );
    setMsgs((ms_) => ms_.map((m, i) => (i === ms_.length - 1 ? { ...m, error, meta: { ...(m.meta || {}), total: performance.now() - t0 } } : m)));
    setBusy(false);
    loadCache();
  };

  /** Sends the same request twice (in the selected mode) plus once in the opposite mode, and explains what the cache did. */
  const testCache = async () => {
    if (!gw || !model) return;
    setCtesting(true); setCtest(null);
    const prompt = input.trim() || "Reply with a one-line greeting.";
    const msgs: any[] = [];
    if (system.trim()) msgs.push({ role: "system", content: system });
    msgs.push({ role: "user", content: prompt });
    const useTemp = gm?.cache_any_temperature ? temp : 0;  // the default rule only caches temperature 0
    const rows: any[] = [];
    for (const asStream of [stream, stream, !stream]) {
      const t0 = performance.now();
      let meta: any = {}, text = "", ttft: number | undefined;
      const { error } = await chatCompletion(
        { url: `${baseUrl()}/chat/completions`, headers: cacheHeaders(), body: { model, messages: msgs, temperature: useTemp, max_tokens: Math.min(maxTok, 128), stream: asStream } },
        (m) => (meta = m), (u) => { text = u.text || ""; ttft = u.ttft; },
      );
      rows.push({ mode: asStream ? "stream" : "json", cache: meta.cache as string | undefined, replay: meta.replay, ms: performance.now() - t0, ttft: asStream ? ttft : undefined, upstream: meta.upstream, endpoint: meta.endpoint, text, error });
    }
    setCtest({ rows, prompt, temp: useTemp, backend: cstats?.backend, mode: cacheMode, stream });
    setCtesting(false);
    loadCache();
  };
  const clearCache = async () => { await api("/api/cache/clear", { method: "POST" }); setCtest(null); loadCache(); };

  const chip = (c?: string) => {
    const t: Record<string, [string, string, string]> = {
      hit: ["cache hit", "#5fb585", "Served from the response cache; the upstream was not called"],
      miss: ["cache miss · stored", "#d1a553", "Not in the cache; the upstream answered and the response was stored"],
      skip: ["not cached · non-deterministic", "#8e8e9a", "This model only caches temperature 0 requests"],
      bypass: ["cache bypassed", "#8e8e9a", "Skipped by the X-SG-Cache: 0 request header"],
    };
    const x = t[c || ""];
    return x ? <span className="chip" title={x[2]} style={{ color: x[1], background: x[1] + "22" }}>{x[0]}</span> : null;
  };
  const verdict = (t: any) => {
    const [a, b, x] = t.rows;
    const mode = t.stream ? "streaming" : "non-streaming", other = t.stream ? "non-streaming" : "streaming";
    if (a.error || b.error) return { ok: false, text: a.error || b.error };
    if (b.cache === "hit") {
      const cross = x?.cache === "hit" ? ` The same entry also served a ${other} request.` : x?.cache ? ` (The ${other} request did not hit it.)` : "";
      const speed = t.stream && a.cache !== "hit" ? ` first token after ${ms(b.ttft ?? b.ms)} instead of ${ms(a.ttft ?? a.ms)}, complete in ${ms(b.ms)} vs ${ms(a.ms)}.` : ` ${ms(b.ms)} instead of ${ms(a.ms)} (${(a.ms / Math.max(1, b.ms)).toFixed(1)}× faster).`;
      return { ok: true, text: a.cache === "hit" ? `Cache works. The first request was already cached from earlier; clear the cache for a cold run.${cross}` : `Cache works: the second ${mode} request was served from the ${t.backend === "redis" ? "Redis" : "in-memory"} cache —${speed}${cross}` };
    }
    if (t.stream && gm?.cache_enabled && !gm?.cache_streaming) return { ok: false, text: "Streaming responses are not cached for this model. Enable “Also cache streaming requests” in the gateway editor, or turn Stream off to test the non-streaming cache." };
    if (a.cache === "skip") return { ok: false, text: "The request was not cacheable: this model only caches temperature 0 requests. Enable “Also cache non-deterministic requests” in the gateway editor, or use “Force cache” above." };
    if (a.cache === "bypass") return { ok: false, text: "Cache bypassed by the request header (Bypass selected above)." };
    if (a.cache === "miss") return { ok: false, text: "The response was stored but the second request did not hit it. Check the cache backend status in Settings (Redis unreachable?) or a very short TTL." };
    return { ok: false, text: "Caching is switched off for this model. Enable “Cache responses for this model” in the gateway editor." };
  };
  return (
    <div className="fadein">
      <PageHeader title="Playground" sub="Chat, speak and transcribe through a gateway with the full routing pipeline."
        actions={<button className="btn" onClick={() => setMsgs([])}><Trash2 size={15} /> Clear</button>} />
      <div className="grid gap-5 lg:grid-cols-[280px_1fr]">
        <div className="card card-pad space-y-4 self-start">
          <div><label className="lbl">Gateway</label>
            <select className="input" value={gid ?? ""} onChange={(e) => { const g = gws.find((x) => x.id === +e.target.value); setGid(g.id); setModel((g.models.find((m: any) => m.enabled !== false) || g.models[0])?.public_name || ""); }}>{gws.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
          {gw?.unified && <div><label className="lbl">Unified API key (only if one is configured)</label><input className="input mono" value={token} onChange={(e) => setToken(e.target.value)} placeholder="sk-..." /></div>}
          <div><label className="lbl">Model</label>
            <select className="input mono" value={model} onChange={(e) => setModel(e.target.value)}>{gw?.models.filter((m: any) => m.enabled !== false).map((m: any) => <option key={m.id}>{m.public_name}</option>)}</select>
            <div className="mt-2 flex flex-wrap gap-1">{caps.map((c) => <CapChip key={c} cap={c} />)}</div></div>
          {cmode === "chat" && (<>
          <div><label className="lbl">System prompt</label><textarea className="input" rows={3} value={system} onChange={(e) => setSystem(e.target.value)} /></div>
          <div><label className="lbl">Temperature · {temp}</label><input type="range" min={0} max={2} step={0.1} value={temp} onChange={(e) => setTemp(+e.target.value)} className="w-full" /></div>
          <div><label className="lbl">Max tokens</label><input type="number" min={1} className="input" value={maxTok} onChange={(e) => setMaxTok(+e.target.value || 1)} /></div>
          <label className="flex items-center gap-2 text-sm text-[#c8c8d0]"><input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} /> Stream response</label>
          <label className="-mt-2 flex items-start gap-2 text-sm text-[#c8c8d0]"><input type="checkbox" className="mt-1" checked={useHistory} onChange={(e) => setUseHistory(e.target.checked)} />
            <span>Send chat history<span className="block text-xs text-[#787884]">Off: each message is a separate single-turn request, handy for testing the cache.</span></span></label>
          {stream && gm?.cache_enabled && !gm?.cache_streaming && <p className="-mt-2 text-xs text-[#d1a553]">Streaming responses are not cached for this model. Enable “Also cache streaming requests” in its gateway settings, or turn streaming off.</p>}
          <div className="border-t border-white/[0.08] pt-4">
            <div className="mb-2 flex items-center justify-between"><span className="flex items-center gap-2 text-sm font-semibold"><Database size={14} /> Response cache</span>
              {cstats && <span className="chip" style={{ color: cstats.backend === "redis" ? (cstats.state === "error" ? "#d9776f" : "#5fb585") : "#8e8e9a", background: "#ffffff0d" }}>{cstats.backend === "redis" ? (cstats.state === "error" ? "Redis down → memory" : "Redis") : "in-memory"}</span>}</div>
            {gm && !gw?.unified ? (
              <div className="space-y-1.5 text-xs text-[#8e8e9a]">
                <div>This model: {gm.cache_enabled ? <span className="text-[#7fd1a3]">caching on</span> : <span>caching off</span>}{gm.cache_enabled && <> · TTL {gm.cache_ttl ? `${gm.cache_ttl}s` : `${cstats?.default_ttl ?? 600}s (default)`} · {gm.cache_any_temperature ? "any temperature" : "temperature 0 only"} · {gm.cache_streaming ? `streams cached (${gm.cache_replay_speed || "instant"} replay)` : "streams not cached"}</>}</div>
                {!gm.cache_enabled && <Link to={`/gateways/${gw.id}/edit`} className="text-[#a79cff] hover:underline">Enable in gateway settings →</Link>}
              </div>) : null}
            {gm?.cache_enabled && useHistory && msgs.length > 0 && <p className="mt-2 rounded-md border border-[#d1a553]/30 bg-[#d1a553]/10 p-2 text-xs text-[#e6c27a]">The whole conversation is part of the cache key. Your next message is sent together with {msgs.length} earlier message{msgs.length > 1 ? "s" : ""}, so it only hits the cache if the entire history was sent before. Clear the chat, or turn off “Send chat history”, to test the cache with a repeated message.</p>}
            <label className="lbl mt-3">Cache behaviour for requests</label>
            <select className="input" value={cacheMode} onChange={(e) => setCacheMode(e.target.value as any)}>
              <option value="default">Model default</option><option value="force">Force cache (X-SG-Cache: 1)</option><option value="bypass">Bypass cache (X-SG-Cache: 0)</option></select>
            <button className="btn primary mt-3 w-full justify-center" disabled={ctesting || !model} onClick={testCache} title="Sends the same request twice and shows what the cache did">
              {ctesting ? <Loader2 size={15} className="animate-spin" /> : <FlaskConical size={15} />} Test cache</button>
            {cstats && <div className="mt-3 flex items-center justify-between text-xs text-[#787884]"><span>{cstats.hits} hits · {cstats.misses} misses · {cstats.entries ?? 0} entries</span>
              <button className="text-[#a79cff] hover:underline" onClick={clearCache}>Clear</button></div>}
          </div>
          </>)}
        </div>

        <div className="min-w-0 space-y-5">
          {modes.length > 1 && (
            <div className="flex flex-wrap gap-2" role="tablist" aria-label="Playground mode">
              {modes.map(([k, label]) => <button key={k} role="tab" aria-selected={cmode === k} data-testid={`pmode-${k}`} className={`pill ${cmode === k ? "on" : ""}`} onClick={() => setPmode(k)}>{label}</button>)}
            </div>)}
          {gw && (gw.unified ? gw.flow.models.length > 0 && <FlowGraph key="unified" gw={gw.flow} /> : gw.kind === "council" ? (cflow && cflow.models.length > 0 && <FlowGraph key={cflow.id} gw={cflow} />) : gw.models.length > 0 && <FlowGraph key={gw.id} gw={gw} />)}
          {cmode === "chat" && ctest && (() => { const v = verdict(ctest); return (
            <div className="card card-pad">
              <div className="mb-3 flex items-center justify-between"><h3 className="flex items-center gap-2 text-sm font-semibold"><FlaskConical size={14} /> Cache test</h3><button className="text-xs text-[#8e8e9a] hover:text-white" onClick={() => setCtest(null)}>dismiss</button></div>
              <div className={`mb-3 flex items-start gap-2 rounded-md border p-2.5 text-sm ${v.ok ? "border-[#5fb585]/30 bg-[#5fb585]/10 text-[#7fd1a3]" : "border-[#d1a553]/30 bg-[#d1a553]/10 text-[#e6c27a]"}`}>{v.ok ? <CheckCircle2 size={16} className="mt-0.5 shrink-0" /> : <XCircle size={16} className="mt-0.5 shrink-0" />}<span>{v.text}</span></div>
              <table className="w-full text-sm"><thead><tr className="text-left text-xs uppercase tracking-wider text-[#787884]">{["#", "Mode", "Cache", "Time", "Served by", "Response"].map((h) => <th key={h} className="pb-2 font-medium">{h}</th>)}</tr></thead>
                <tbody>{ctest.rows.map((r: any, i: number) => (
                  <tr key={i} className="border-t border-white/5"><td className="py-2 pr-3">{i + 1}</td><td className="pr-3 text-xs text-[#8e8e9a]">{r.mode}</td><td className="pr-3">{chip(r.cache) || <span className="text-[#787884]">—</span>}</td><td className="pr-3 whitespace-nowrap">{ms(r.ms)}{r.ttft ? <span className="text-xs text-[#787884]"> · first token {ms(r.ttft)}</span> : null}</td>
                    <td className="pr-3 text-xs">{r.cache === "hit" ? <span className="text-[#7fd1a3]">cache</span> : r.upstream ? <><span className="mono">{r.upstream}</span> <span className="text-[#787884]">@ {r.endpoint}</span></> : "—"}</td>
                    <td className="max-w-[260px] truncate text-xs text-[#8e8e9a]" title={r.text}>{r.error || r.text}</td></tr>))}</tbody></table>
              <p className="mt-2 text-xs text-[#787884]">Same request sent {ctest.stream ? "twice streaming, then once non-streaming" : "twice non-streaming, then once streaming"} · temperature {ctest.temp}{ctest.mode !== "default" ? ` · ${ctest.mode === "force" ? "forced" : "bypass"} header` : ""} · prompt “{ctest.prompt.slice(0, 60)}”</p>
            </div>); })()}
        {cmode !== "chat" ? <AudioPlayground key={cmode + model} mode={cmode as "tts" | "stt"} base={baseUrl()} model={model} headers={authHeaders()} /> : (
        <div className="card flex h-[520px] flex-col">
          <div className="scroll-thin flex-1 space-y-4 overflow-y-auto p-5">
            {msgs.length === 0 && <div className="pt-16 text-center text-sm text-[#787884]">Send a message to start. Routing details appear under each reply.</div>}
            {msgs.map((m, i) => (
              <div key={i} className={m.role === "user" ? "flex justify-end" : ""}>
                <div className={`max-w-[85%] rounded-lg px-4 py-3 text-sm ${m.role === "user" ? "bg-[#6c5ce7]/25 border border-[#6c5ce7]/40" : "border border-white/[0.08] bg-[#17171a]"}`}>
                  {m.image && <img src={m.image} className="mb-2 max-h-40 rounded-md" />}
                  {m.reasoning && <details className="mb-2 text-[#8e8e9a]"><summary className="cursor-pointer text-xs">Reasoning ({m.reasoning.length} chars)</summary><div className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-xs italic scroll-thin">{m.reasoning}</div></details>}
                  <div className="whitespace-pre-wrap">{m.text || (m.role === "assistant" && busy && i === msgs.length - 1 && !m.error ? <span className="text-[#787884]">…</span> : "")}</div>
                  {m.error && <div className="mt-1 text-red-300">{m.error}</div>}
                  {m.council && <CouncilTabs data={m.council} />}
                  {m.role === "assistant" && m.meta && (
                    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[#787884]">
                      {m.meta.upstream && <span>→ <span className="mono">{m.meta.upstream}</span> @ {m.meta.endpoint}</span>}
                      {!!m.meta.queue && <span>queued {ms(m.meta.queue)}</span>}
                      {chip(m.meta.cache)}
                      {m.meta.total && <span>{ms(m.meta.total)}</span>}
                    </div>)}
                </div>
              </div>
            ))}
            <div ref={bottom} />
          </div>
          <div className="border-t border-white/[0.08] p-3">
            {image && <div className="mb-2 flex items-center gap-2 text-xs text-[#8e8e9a]"><img src={image} className="h-10 rounded" /> image attached <button className="btn !p-1" onClick={() => setImage(undefined)}>remove</button></div>}
            <div className="flex items-end gap-2">
              <label className={`btn cursor-pointer ${caps.includes("vision") ? "" : "opacity-40"}`} title={caps.includes("vision") ? "Attach image" : "Selected model has no vision capability"}>
                <ImagePlus size={16} /><input type="file" accept="image/*" hidden disabled={!caps.includes("vision")} onChange={(e) => pickImage(e.target.files?.[0])} /></label>
              <textarea className="input flex-1" rows={2} placeholder="Message… (Enter to send, Shift+Enter for newline)" value={input} onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
              {busy ? <button className="btn danger" onClick={() => abort.current?.abort()}><Square size={15} /> Stop</button>
                : <button className="btn primary" onClick={send} disabled={!model || (!input.trim() && !image)}><Send size={15} /> Send</button>}
            </div>
          </div>
        </div>
        )}
        </div>
      </div>
    </div>
  );
}
