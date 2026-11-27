import { ReactNode } from "react";

export const SPEECH_FORMATS = ["mp3", "opus", "aac", "flac", "wav", "pcm"];
export const TRANSCRIPTION_FORMATS = ["json", "text", "srt", "verbose_json", "vtt"];
export const hasAudioDefaults = (a: any) => !!a && Object.values(a).some((sec: any) => sec && Object.values(sec).some((v) => v !== "" && v != null));

/** The audio settings of a model: defaults for speech and transcription requests (used only when a request leaves the setting out), plus whatever
 *  the caller puts in `voicePicker` (the voice checklist). Used by the gateway editor and the unified endpoint. */
export default function AudioSettings({ tts, stt, defaults, onChange, voiceChoices, voicePicker, bare }: {
  tts: boolean; stt: boolean; defaults: any; onChange: (next: any | null) => void; voiceChoices: string[]; voicePicker?: ReactNode; bare?: boolean;
}) {
  const ad = defaults || {};
  const setAudio = (sec: "speech" | "transcription", key: string, value: any) => {
    const next: any = { ...ad, [sec]: { ...(ad[sec] || {}) } };
    if (value === "" || value == null) delete next[sec][key]; else next[sec][key] = value;
    if (!Object.keys(next[sec]).length) delete next[sec];
    onChange(Object.keys(next).length ? next : null);
  };
  return (
          <div className="space-y-6">
            {!bare && <p className="text-xs text-[#787884]">Defaults are used only when a request leaves the setting out; a client's own value always wins.</p>}
            {tts && (
              <section>
                {!bare && <h4 className="mb-3 text-sm font-semibold">Text-to-speech</h4>}
                {voicePicker}
                <div className="mt-4 grid gap-4 md:grid-cols-3">
                  <div><label className="lbl">Default voice</label>
                    {voiceChoices.length
                      ? <select className="input" value={ad.speech?.voice || ""} onChange={(e) => setAudio("speech", "voice", e.target.value)} data-testid="audio-voice"><option value="">None: the client chooses</option>{voiceChoices.map((v) => <option key={v} value={v}>{v}</option>)}</select>
                      : <input className="input mono" value={ad.speech?.voice || ""} placeholder="e.g. alloy" onChange={(e) => setAudio("speech", "voice", e.target.value)} data-testid="audio-voice" />}
                    <p className="mt-1 text-xs text-[#787884]">{voiceChoices.length ? "From the voices above." : "No voice list yet: type a name."}</p></div>
                  <div><label className="lbl">Default format</label>
                    <select className="input" value={ad.speech?.response_format || ""} onChange={(e) => setAudio("speech", "response_format", e.target.value)} data-testid="audio-format"><option value="">Upstream default</option>{SPEECH_FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}</select></div>
                  <div><label className="lbl">Default speed</label>
                    <input type="number" min={0.25} max={4} step={0.05} className="input" placeholder="1.0" value={ad.speech?.speed ?? ""} onChange={(e) => setAudio("speech", "speed", e.target.value === "" ? "" : +e.target.value)} data-testid="audio-speed" />
                    <p className="mt-1 text-xs text-[#787884]">0.25 to 4</p></div>
                </div>
              </section>)}
            {stt && (
              <section>
                {!bare && <h4 className="mb-3 text-sm font-semibold">Speech-to-text</h4>}
                <div className="grid gap-4 md:grid-cols-3">
                  <div><label className="lbl">Default language</label>
                    <input className="input mono" placeholder="auto-detect (e.g. en, pl)" value={ad.transcription?.language || ""} onChange={(e) => setAudio("transcription", "language", e.target.value)} data-testid="audio-language" />
                    <p className="mt-1 text-xs text-[#787884]">Transcriptions only; translations are always into English.</p></div>
                  <div><label className="lbl">Default format</label>
                    <select className="input" value={ad.transcription?.response_format || ""} onChange={(e) => setAudio("transcription", "response_format", e.target.value)}><option value="">Upstream default</option>{TRANSCRIPTION_FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}</select></div>
                  <div><label className="lbl">Default temperature</label>
                    <input type="number" min={0} max={1} step={0.1} className="input" placeholder="0" value={ad.transcription?.temperature ?? ""} onChange={(e) => setAudio("transcription", "temperature", e.target.value === "" ? "" : +e.target.value)} />
                    <p className="mt-1 text-xs text-[#787884]">0 to 1</p></div>
                </div>
              </section>)}
          </div>
  );
}
