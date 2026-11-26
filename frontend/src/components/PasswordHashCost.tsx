import { useEffect, useState } from "react";
import { KeyRound, Loader2, Save } from "lucide-react";
import { api } from "../api";

const COSTS: [number, string][] = [[14, "Standard (16 MB per check) - default"], [15, "Higher (32 MB per check)"], [16, "Highest (64 MB per check)"]];

export default function PasswordHashCost() {
  const [v, setV] = useState(14);
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => { api("/api/settings").then((s) => { setV(s.password_hash_log_n ?? 14); setSource(s.password_source || ""); }); }, []);
  const save = async () => {
    setBusy(true); setMsg(null);
    try { await api("/api/settings", { method: "PUT", json: { password_hash_log_n: v } }); setMsg({ ok: true, text: "Saved. Your password is re-hashed at the next login." }); }
    catch (e: any) { setMsg({ ok: false, text: e.message }); }
    setBusy(false);
  };
  return (
    <div className="card card-pad mt-4">
      <h3 className="flex items-center gap-2 text-base font-semibold"><KeyRound size={17} /> Admin password hash cost</h3>
      <p className="mb-3 mt-1 text-sm text-[#8e8e9a]">How expensive it is to check a password chosen in this interface (scrypt). Higher is harder to brute-force from a stolen data volume but uses more memory per login attempt. After a change the stored hash is upgraded at your next successful login, which signs other admin sessions out once.{source && source !== "ui" ? " It does not apply while the password comes from SG_ADMIN_PASSWORD or the generated file." : ""}</p>
      <div className="flex flex-wrap items-end gap-3">
        <div><label className="lbl">Cost</label>
          <select className="input" value={v} onChange={(e) => setV(+e.target.value)}>{COSTS.map(([n, l]) => <option key={n} value={n}>{l}</option>)}</select></div>
        <button className="btn primary" onClick={save} disabled={busy}>{busy ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} Save</button>
        {msg && <span className={"text-sm " + (msg.ok ? "text-[#7fd1a3]" : "text-[#ff8a8a]")}>{msg.text}</span>}
      </div>
    </div>
  );
}
