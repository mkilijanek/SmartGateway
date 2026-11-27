import { useEffect, useRef, useState } from "react";
import { Download, FileAudio, Loader2, Mic, Square, Volume2 } from "lucide-react";
import { voiceName } from "./VoicePicker";
import { ms } from "./ui";

const SPEECH_FORMATS = ["mp3", "wav", "opus", "flac", "aac"];   // pcm is raw and cannot be played in a browser
const TRANSCRIPTION_FORMATS = ["json", "text", "srt", "verbose_json", "vtt"];
const EXT: Record<string, string> = { "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/ogg": "ogg", "audio/opus": "opus", "audio/flac": "flac", "audio/aac": "aac", "audio/webm": "webm" };

type Result = { id: number; at: number; ms: number; error?: string; upstream?: string; endpoint?: string; text?: string; raw?: string;
  url?: string; bytes?: number; ctype?: string; label: string };

const size = (b: number) => (b < 1024 ? `${b} B` : `${(b / 1024).toFixed(1)} KB`);

/** What the gateway says about how it routed the request (only the admin routes report it). */
const routing = (r: Response) => ({ upstream: r.headers.get("X-SG-Upstream") || undefined, endpoint: r.headers.get("X-SG-Endpoint") || undefined });
const errorOf = async (r: Response) => { let m = `HTTP ${r.status}`; try { const j = await r.json(); m = j.error?.message || j.detail?.message || j.detail || m; } catch { /* keep the status */ } return typeof m === "string" ? m : JSON.stringify(m); };

/** Text-to-speech and speech-to-text through a gateway, with the same routing pipeline as chat. */
export default function AudioPlayground({ mode, base, model, headers }: { mode: "tts" | "stt"; base: string; model: string; headers: Record<string, string> }) {
  const [results, setResults] = useState<Result[]>([]);
  const [busy, setBusy] = useState(false);
  const urls = useRef<string[]>([]);
  const n = useRef(0);
  const hkey = JSON.stringify(headers);
  const add = (r: Omit<Result, "id" | "at">) => setResults((x) => [{ ...r, id: ++n.current, at: Date.now() }, ...x]);
  useEffect(() => () => { urls.current.forEach((u) => URL.revokeObjectURL(u)); }, []);
  useEffect(() => { setResults([]); }, [mode, model, base]);

  // ---------------------------------------------------------------- text to speech
  const [text, setText] = useState("Hello! This is a test of the text-to-speech model.");
  const [voice, setVoice] = useState("");
  const [voices, setVoices] = useState<string[] | null>(null);
  const [format, setFormat] = useState("");
  const [speed, setSpeed] = useState("");
  useEffect(() => {
    if (mode !== "tts") return;
    setVoices(null); setVoice("");
    fetch(`${base}/audio/voices?model=${encodeURIComponent(model)}`, { headers }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      const raw = Array.isArray(d) ? d : d?.voices;
      setVoices(Array.isArray(raw) ? raw.map(voiceName).filter(Boolean) : []);
    }).catch(() => setVoices([]));
  }, [mode, base, model, hkey]); // eslint-disable-line

  const speak = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    const t0 = performance.now();
    const body: any = { model, input: text };
    if (voice) body.voice = voice;
    if (format) body.response_format = format;
    if (speed) body.speed = +speed;
    const label = [voice || "default voice", format || "default format", speed ? `speed ${speed}` : ""].filter(Boolean).join(" · ");
    try {
      const r = await fetch(`${base}/audio/speech`, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
      const meta = routing(r);
      if (!r.ok) add({ error: await errorOf(r), ms: performance.now() - t0, label, ...meta });
      else {
        const blob = await r.blob();
        const url = URL.createObjectURL(blob); urls.current.push(url);
        add({ url, bytes: blob.size, ctype: blob.type || r.headers.get("content-type") || "", ms: performance.now() - t0, label, text, ...meta });
      }
    } catch (e: any) { add({ error: e.message, ms: performance.now() - t0, label }); }
    setBusy(false);
  };

  // ---------------------------------------------------------------- speech to text
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | undefined>();
  const [lang, setLang] = useState("");
  const [tfmt, setTfmt] = useState("json");
  const [task, setTask] = useState<"transcriptions" | "translations">("transcriptions");
  const [rec, setRec] = useState(false);
  const [secs, setSecs] = useState(0);
  const [recErr, setRecErr] = useState("");
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const canRecord = typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";
  useEffect(() => { if (!file) { setPreview(undefined); return; } const u = URL.createObjectURL(file); setPreview(u); return () => URL.revokeObjectURL(u); }, [file]);
  useEffect(() => () => { window.clearInterval(timer.current); recorder.current?.stream.getTracks().forEach((t) => t.stop()); }, []);

  const startRec = async () => {
    setRecErr("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream); const chunks: Blob[] = [];
      mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const type = mr.mimeType || "audio/webm";
        setFile(new File([new Blob(chunks, { type })], `recording.${type.includes("ogg") ? "ogg" : type.includes("mp4") ? "m4a" : "webm"}`, { type }));
      };
      recorder.current = mr; mr.start(); setRec(true); setSecs(0);
      timer.current = window.setInterval(() => setSecs((s) => s + 1), 1000);
    } catch (e: any) { setRecErr(e.message || "Could not open the microphone"); }
  };
  const stopRec = () => { window.clearInterval(timer.current); recorder.current?.stop(); setRec(false); };

  const transcribe = async () => {
    if (!file || busy) return;
    setBusy(true);
    const t0 = performance.now();
    const fd = new FormData();
    fd.append("model", model); fd.append("file", file);
    if (lang.trim() && task === "transcriptions") fd.append("language", lang.trim());
    if (tfmt !== "json") fd.append("response_format", tfmt);
    const label = [file.name, task === "translations" ? "to English" : lang.trim() || "auto language", tfmt].join(" · ");
    try {
      const r = await fetch(`${base}/audio/${task}`, { method: "POST", headers, body: fd });
      const meta = routing(r);
      const raw = await r.text();
      if (!r.ok) { let m = raw.slice(0, 300); try { const j = JSON.parse(raw); m = j.error?.message || j.detail?.message || j.detail || m; } catch { /* plain text */ } add({ error: typeof m === "string" ? m : JSON.stringify(m), ms: performance.now() - t0, label, ...meta }); }
      else { let t = raw; try { const j = JSON.parse(raw); if (typeof j?.text === "string") t = j.text; } catch { /* text, srt or vtt */ } add({ text: t, raw, ms: performance.now() - t0, label, ...meta }); }
    } catch (e: any) { add({ error: e.message, ms: performance.now() - t0, label }); }
    setBusy(false);
  };

  return (
    <div className="card flex flex-col" data-testid={`audio-playground-${mode}`}>
      <div className="space-y-4 border-b border-white/[0.08] p-5">
        {mode === "tts" ? (<>
          <div><label className="lbl">Text to speak</label>
            <textarea className="input" rows={3} value={text} onChange={(e) => setText(e.target.value)} data-testid="tts-text" /></div>
          <div className="grid gap-4 md:grid-cols-3">
            <div><label className="lbl">Voice</label>
              {voices && voices.length > 0
                ? <select className="input" value={voice} onChange={(e) => setVoice(e.target.value)} data-testid="tts-voice"><option value="">Default (gateway / upstream)</option>{voices.map((v) => <option key={v} value={v}>{v}</option>)}</select>
                : <input className="input mono" value={voice} onChange={(e) => setVoice(e.target.value)} placeholder={voices === null ? "loading voices…" : "default; or type a voice name"} data-testid="tts-voice" />}</div>
            <div><label className="lbl">Format</label>
              <select className="input" value={format} onChange={(e) => setFormat(e.target.value)} data-testid="tts-format"><option value="">Default</option>{SPEECH_FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}</select></div>
            <div><label className="lbl">Speed</label>
              <input type="number" min={0.25} max={4} step={0.05} className="input" placeholder="default" value={speed} onChange={(e) => setSpeed(e.target.value)} data-testid="tts-speed" /></div>
          </div>
          <div className="flex items-center gap-3">
            <button className="btn primary" onClick={speak} disabled={busy || !text.trim() || !model} data-testid="tts-speak">{busy ? <Loader2 size={15} className="animate-spin" /> : <Volume2 size={15} />} Speak</button>
            <span className="text-xs text-[#787884]">Settings you leave on default use the defaults of the gateway model, then the upstream's.</span>
          </div>
        </>) : (<>
          <div className="flex flex-wrap items-center gap-2">
            <label className="btn cursor-pointer"><FileAudio size={15} /> Choose audio file<input type="file" accept="audio/*,video/*" hidden data-testid="stt-file" onChange={(e) => { setFile(e.target.files?.[0] || null); e.target.value = ""; }} /></label>
            {canRecord
              ? (rec ? <button className="btn danger" onClick={stopRec} data-testid="stt-stop"><Square size={14} /> Stop · {secs}s</button>
                     : <button className="btn" onClick={startRec} disabled={busy} data-testid="stt-record"><Mic size={15} /> Record</button>)
              : <span className="text-xs text-[#787884]" title="Browsers only allow the microphone on https:// or localhost">Recording needs https or localhost; upload a file instead.</span>}
            {file && <span className="text-xs text-[#8e8e9a]">{file.name} · {size(file.size)}</span>}
          </div>
          {recErr && <div className="text-xs text-red-300">{recErr}</div>}
          {preview && <audio controls src={preview} className="w-full" data-testid="stt-preview" />}
          <div className="grid gap-4 md:grid-cols-3">
            <div><label className="lbl">Task</label>
              <select className="input" value={task} onChange={(e) => setTask(e.target.value as any)} data-testid="stt-task"><option value="transcriptions">Transcribe</option><option value="translations">Translate to English</option></select></div>
            <div><label className="lbl">Language</label>
              <input className="input mono" placeholder="auto-detect (e.g. en, pl)" value={lang} disabled={task === "translations"} onChange={(e) => setLang(e.target.value)} data-testid="stt-language" /></div>
            <div><label className="lbl">Format</label>
              <select className="input" value={tfmt} onChange={(e) => setTfmt(e.target.value)} data-testid="stt-format">{TRANSCRIPTION_FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}</select></div>
          </div>
          <div className="flex items-center gap-3">
            <button className="btn primary" onClick={transcribe} disabled={busy || !file || !model} data-testid="stt-go">{busy ? <Loader2 size={15} className="animate-spin" /> : <Mic size={15} />} {task === "translations" ? "Translate" : "Transcribe"}</button>
            <span className="text-xs text-[#787884]">Language and format left empty use the gateway model's defaults.</span>
          </div>
        </>)}
      </div>

      <div className="scroll-thin max-h-[420px] min-h-[160px] space-y-3 overflow-y-auto p-5" data-testid="audio-results">
        {results.length === 0 && <div className="py-8 text-center text-sm text-[#787884]">{mode === "tts" ? "Type some text and press Speak. The audio appears here." : "Choose or record some audio and press Transcribe. The text appears here."}</div>}
        {results.map((r) => (
          <div key={r.id} className="rounded-lg border border-white/[0.08] bg-[#17171a] p-4 text-sm" data-testid="audio-result">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[#787884]">
              <span className="text-[#c8c8d0]">{r.label}</span>
              {r.bytes != null && <span>{size(r.bytes)}{r.ctype ? ` · ${r.ctype}` : ""}</span>}
              {r.upstream && <span>→ <span className="mono">{r.upstream}</span> @ {r.endpoint}</span>}
              <span>{ms(r.ms)}</span>
            </div>
            {r.error && <div className="text-red-300" data-testid="audio-error">{r.error}</div>}
            {r.url && (<>
              <audio controls autoPlay={r.id === n.current} src={r.url} className="w-full" />
              <a className="mt-2 inline-flex items-center gap-1.5 text-xs text-[#a79cff] hover:underline" href={r.url} download={`speech.${EXT[(r.ctype || "").split(";")[0]] || "audio"}`}><Download size={13} /> Download</a>
            </>)}
            {r.text != null && !r.url && <div className="whitespace-pre-wrap" data-testid="stt-text">{r.text}</div>}
            {r.raw && r.raw !== r.text && <details className="mt-2 text-xs text-[#8e8e9a]"><summary className="cursor-pointer">Raw response</summary><pre className="mono scroll-thin mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-[11px]">{r.raw}</pre></details>}
          </div>))}
      </div>
    </div>
  );
}
