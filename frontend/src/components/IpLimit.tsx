import { useEffect, useState } from "react";
import { Loader2, Save, Gauge } from "lucide-react";
import { api } from "../api";

export default function IpLimit() {
  const [cur, setCur] = useState<{ ip_rpm: number | null; ip_rpm_env: number } | null>(null);
  const [v, setV] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = () => api("/api/settings").then((s) => { setCur({ ip_rpm: s.ip_rpm, ip_rpm_env: s.ip_rpm_env }); setV(s.ip_rpm === null ? "" : String(s.ip_rpm)); });
  useEffect(() => { load(); }, []);
  const save = async () => {
    setBusy(true); setMsg(null);
    try { await api("/api/settings", { method: "PUT", json: { ip_rpm: v.trim() === "" ? -1 : Math.max(0, Math.round(+v || 0)) } }); setMsg({ ok: true, text: "Saved." }); load(); }
    catch (e: any) { setMsg({ ok: false, text: e.message }); }
    setBusy(false);
  };
  return (
    <div className="card card-pad mt-4">
      <h3 className="flex items-center gap-2 text-base font-semibold"><Gauge size={17} /> Per-IP request limit</h3>
      <p className="mb-3 mt-1 text-sm text-[#8e8e9a]">Requests per minute per client address on the public gateway URLs (/gw, /v1, /unified, /mcp). Protects gateways that have no API key. 0 turns it off; leave empty to use the SG_IP_RPM environment variable{cur ? ` (now ${cur.ip_rpm_env || "off"})` : ""}.</p>
      <div className="flex flex-wrap items-end gap-3">
        <div><label className="lbl">Requests per minute</label><input type="number" min={0} className="input" placeholder="from environment" value={v} onChange={(e) => setV(e.target.value)} /></div>
        <button className="btn primary" onClick={save} disabled={busy}>{busy ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} Save</button>
        {msg && <span className={"text-sm " + (msg.ok ? "text-[#7fd1a3]" : "text-[#ff8a8a]")}>{msg.text}</span>}
      </div>
    </div>
  );
}
