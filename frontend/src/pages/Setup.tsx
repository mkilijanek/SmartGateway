import Logo from "../components/Logo";
import { useState } from "react";
import { api } from "../api";

const MIN = 10;

/** First-run wizard: shown once, on a brand-new install, instead of the login page. */
export default function Setup({ onDone }: { onDone: () => void }) {
  const [code, setCode] = useState("");
  const [u, setU] = useState("admin");
  const [p, setP] = useState("");
  const [p2, setP2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const problem = p && p.length < MIN ? `At least ${MIN} characters (${p.length} so far)` : p2 && p !== p2 ? "The passwords do not match" : "";
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api("/api/setup", { method: "POST", json: { code, username: u, password: p } }); onDone(); }
    catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={submit} className="card card-pad w-full max-w-sm space-y-4">
        <div className="flex items-center gap-3">
          <Logo size={32} />
          <span className="text-base font-semibold">Welcome to SmartGateway</span>
        </div>
        <div className="text-sm text-[#8e8e9a]">Create the admin account. You will use it to sign in to this interface.</div>
        <div>
          <label className="lbl">Setup code</label>
          <input className="input font-mono" value={code} onChange={(e) => setCode(e.target.value)} placeholder="XXXX-XXXX-XXXX" autoComplete="off" autoFocus />
          <div className="mt-1 text-xs text-[#8e8e9a]">Printed in the container log: <code>docker logs &lt;container&gt;</code>. It proves you control this server.</div>
        </div>
        <div><label className="lbl">Username</label><input className="input" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" /></div>
        <div><label className="lbl">Password</label><input className="input" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="new-password" /></div>
        <div><label className="lbl">Repeat password</label><input className="input" type="password" value={p2} onChange={(e) => setP2(e.target.value)} autoComplete="new-password" /></div>
        {(problem || err) && <div className="rounded-md border border-red-400/30 bg-red-400/10 p-2.5 text-sm text-red-300">{err || problem}</div>}
        <button className="btn primary w-full justify-center" disabled={busy || !code || !u || p.length < MIN || p !== p2}>{busy ? "Creating…" : "Create admin account"}</button>
      </form>
    </div>
  );
}
