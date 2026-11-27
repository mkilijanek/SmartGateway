import { useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, ShieldAlert } from "lucide-react";
import { api } from "../api";
import { Modal } from "./ui";

export type AccountKind = "codex" | "claude";
type Start = { session_id: string; user_code?: string; verification_url?: string; authorize_url?: string; interval?: number };

const NOTICE: Record<AccountKind, { title: string; warn: string; how: string }> = {
  codex: {
    title: "ChatGPT / Codex account",
    warn: "This uses your ChatGPT subscription through the same sign-in as the official Codex CLI. Use it for your own work only. OpenAI does not offer this as a public API, so it may stop working or be limited at any time, and your plan's own usage limits apply. Your tokens stay on this server, encrypted.",
    how: "You will get a short code and approve it at OpenAI in your browser. Nothing needs to be installed.",
  },
  claude: {
    title: "Claude account",
    warn: "Anthropic's terms do not allow using a Claude subscription login in third-party tools, and its API may refuse these requests or restrict the account. By continuing you accept that risk for your own account. Use it for your own work only. Your tokens stay on this server, encrypted.",
    how: "You will open a link, log in at claude.ai, and paste the code that page shows back here.",
  },
};

/** Sign in with a subscription account. ChatGPT: show a code and wait until it is approved. Claude: open a link, paste the code it shows.
 *  The content only (no frame), so it can sit inside the Add endpoint dialog; OAuthAccountDialog wraps it for "Sign in again". */
export function OAuthAccountFlow({ kind, endpointId, onClose, onDone }: { kind: AccountKind; endpointId?: number; onClose: () => void; onDone: () => void }) {
  const n = NOTICE[kind];
  const [step, setStep] = useState<"notice" | "code" | "done">("notice");
  const [start, setStart] = useState<Start | null>(null);
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const finished = () => { setStep("done"); onDone(); setTimeout(onClose, 1200); };

  const begin = async () => {
    setBusy(true); setErr("");
    try {
      const s: Start = await api(`/api/accounts/${kind}/start`, { method: "POST", json: { endpoint_id: endpointId ?? null } });
      setStart(s); setStep("code");
      if (kind === "codex") poll(s);
    } catch (e: any) { setErr(e.message); }
    setBusy(false);
  };

  const poll = (s: Start) => {
    const every = (s.interval || 5) * 1000;
    const tick = async () => {
      if (!alive.current) return;
      try {
        const r = await api("/api/accounts/codex/poll", { method: "POST", json: { session_id: s.session_id } });
        if (!alive.current) return;
        if (r.status === "done") { finished(); return; }
      } catch (e: any) { if (alive.current) { setErr(e.message); setStep("notice"); } return; }
      setTimeout(tick, every);
    };
    setTimeout(tick, every);
  };

  const submitCode = async () => {
    if (!start) return;
    setBusy(true); setErr("");
    try { await api("/api/accounts/claude/finish", { method: "POST", json: { session_id: start.session_id, code: pasted } }); if (alive.current) finished(); }
    catch (e: any) { setErr(e.message); }
    setBusy(false);
  };

  return (
      <div className="space-y-4">
        {step === "notice" && <>
          <div className="flex gap-3 rounded-md border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-200">
            <ShieldAlert size={18} className="mt-0.5 shrink-0" />
            <div>{n.warn}</div>
          </div>
          <p className="text-sm text-[#8e8e9a]">{n.how}</p>
        </>}
        {step === "code" && start && kind === "codex" && <>
          <p className="text-sm text-[#c8c8d0]">1. Open the OpenAI sign-in page and enter this code:</p>
          <div className="rounded-md border border-white/10 bg-white/[0.03] p-4 text-center">
            <div className="mono text-3xl font-semibold tracking-widest" data-testid="user-code">{start.user_code}</div>
          </div>
          <a className="btn w-full justify-center" href={start.verification_url} target="_blank" rel="noreferrer noopener"><ExternalLink size={15} /> Open {start.verification_url?.replace("https://", "")}</a>
          <p className="flex items-center gap-2 text-sm text-[#8e8e9a]"><Loader2 size={15} className="animate-spin" /> 2. Waiting for you to approve it… this window updates by itself.</p>
        </>}
        {step === "code" && start && kind === "claude" && <>
          <p className="text-sm text-[#c8c8d0]">1. Open the Claude sign-in page, log in and approve:</p>
          <a className="btn w-full justify-center" href={start.authorize_url} target="_blank" rel="noreferrer noopener" data-testid="authorize-link"><ExternalLink size={15} /> Open claude.ai sign-in</a>
          <div>
            <label className="lbl">2. Paste the code it shows</label>
            <input className="input mono" placeholder="code#state" value={pasted} onChange={(e) => setPasted(e.target.value)} autoComplete="off" autoFocus data-testid="pasted-code" />
          </div>
        </>}
        {step === "done" && <div className="rounded-md border border-emerald-400/30 bg-emerald-400/10 p-3 text-sm text-emerald-200">Signed in. The account is ready to use.</div>}
        {err && <div className="rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{err}</div>}
        <div className="flex justify-end gap-2">
          <button className="btn" onClick={onClose}>{step === "done" ? "Close" : "Cancel"}</button>
          {step === "notice" && <button className="btn primary" disabled={busy} onClick={begin}>{busy ? <><Loader2 size={16} className="animate-spin" /> Starting…</> : "I understand, start sign-in"}</button>}
          {step === "code" && kind === "claude" && <button className="btn primary" disabled={busy || !pasted.trim()} onClick={submitCode}>{busy ? <><Loader2 size={16} className="animate-spin" /> Checking…</> : "Finish sign-in"}</button>}
        </div>
      </div>
  );
}

export default function OAuthAccountDialog({ kind, endpointId, onClose, onDone }: { kind: AccountKind; endpointId?: number; onClose: () => void; onDone: () => void }) {
  return (
    <Modal title={`Sign in again · ${NOTICE[kind].title}`} onClose={onClose}>
      <OAuthAccountFlow kind={kind} endpointId={endpointId} onClose={onClose} onDone={onDone} />
    </Modal>
  );
}
