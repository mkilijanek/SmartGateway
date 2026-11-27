import { useEffect, useState } from "react";
import { CheckCircle2, Database, Loader2, Plug, Save, Trash2, XCircle } from "lucide-react";
import { api } from "../api";
import { fmt, PageHeader, Stat } from "../components/ui";
import CompressionSettings from "../components/CompressionSettings";
import Retention from "../components/Retention";
import IpLimit from "../components/IpLimit";
import PasswordHashCost from "../components/PasswordHashCost";
import AdminPassword from "../components/AdminPassword";
import { Link } from "react-router-dom";
import { Tabs, useTab } from "../components/Tabs";

export default function Settings() {
  const [tab, setTab] = useTab(["general", "compression", "data", "security"]);
  const [s, setS] = useState<any>(null);
  const [stats, setStats] = useState<any>(null);
  const [f, setF] = useState<any>({ redis_enabled: false, url: "", username: "", password: "", redis_key_prefix: "sg:", cache_default_ttl: 600, cache_max_entry_kb: 512, cache_fallback_memory: true });
  const [test, setTest] = useState<any>(null);
  const [busy, setBusy] = useState<string>("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = async () => {
    const v = await api("/api/settings");
    setS(v);
    setF((p: any) => ({ ...p, url: v.redis_url || "", username: v.redis_username || "", password: "", redis_enabled: v.redis_enabled, redis_key_prefix: v.redis_key_prefix, cache_default_ttl: v.cache_default_ttl, cache_max_entry_kb: v.cache_max_entry_kb, cache_fallback_memory: v.cache_fallback_memory }));
  };
  const loadStats = () => api("/api/cache/stats").then(setStats).catch(() => {});
  useEffect(() => { load(); loadStats(); const t = setInterval(loadStats, 4000); return () => clearInterval(t); }, []);

  const runTest = async () => {
    setBusy("test"); setTest(null);
    try { setTest(await api("/api/settings/redis/test", { method: "POST", json: { url: f.url || null, username: f.username, password: f.password || null } })); } catch (e: any) { setTest({ ok: false, error: e.message }); }
    setBusy("");
  };
  const save = async (extra: any = {}) => {
    setBusy("save"); setMsg(null);
    try {
      const body: any = { redis_enabled: f.redis_enabled, redis_key_prefix: f.redis_key_prefix, cache_default_ttl: +f.cache_default_ttl, cache_max_entry_kb: +f.cache_max_entry_kb, cache_fallback_memory: f.cache_fallback_memory, ...extra };
      body.redis_url = f.url.trim();
      body.redis_username = f.username;
      if (f.password) body.redis_password = f.password;  // empty = keep the saved password
      const r = await api("/api/settings", { method: "PUT", json: body });
      setS(r); setStats((x: any) => ({ ...x, ...r.cache })); setF((p: any) => ({ ...p, url: r.redis_url || "", username: r.redis_username || "", password: "" }));
      setMsg({ ok: true, text: "Saved and applied." }); loadStats();
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
    setBusy("");
  };
  const clear = async () => {
    if (!confirm("Remove all cached responses?")) return;
    setBusy("clear");
    const r = await api("/api/cache/clear", { method: "POST" });
    setMsg({ ok: true, text: `Cleared ${r.memory + r.redis} cached responses.` }); loadStats(); setBusy("");
  };
  if (!s) return null;

  const backend = stats?.backend === "redis"
    ? (stats.state === "error" ? { label: "Redis · unreachable (using memory)", color: "#d9776f" } : { label: "Redis · connected", color: "#5fb585" })
    : { label: "In-memory", color: "#8e8e9a" };

  return (
    <div className="fadein">
      <PageHeader title="Settings" sub="System-wide options." />
      <Tabs items={[["general", "General"], ["compression", "Compression"], ["data", "Data"], ["security", "Security"]]} value={tab} onChange={setTab} />
      {tab === "general" && <>
      <div className="card card-pad">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
          <h3 className="flex items-center gap-2 text-base font-semibold"><Database size={17} /> Response cache</h3>
          <span className="chip" style={{ color: backend.color, background: backend.color + "22" }}>{backend.label}</span>
        </div>
        <p className="mb-5 max-w-3xl text-sm text-[#8e8e9a]">
          Identical requests can be answered from a cache. By default entries live in this process's memory (lost on restart). Connect an optional Redis server to keep
          them across restarts and share them between instances. Which models use the cache is set per model in each gateway's editor.
        </p>
        {stats?.state === "error" && stats.last_error && <div className="mb-4 rounded-md border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">Redis error: {stats.last_error}. {s.cache_fallback_memory ? "Requests keep working; the in-memory cache is used until Redis is back." : "Caching is paused until Redis is back."}</div>}

        <label className="mb-4 flex cursor-pointer items-center gap-3 text-sm">
          <input type="checkbox" checked={f.redis_enabled} onChange={(e) => setF({ ...f, redis_enabled: e.target.checked })} />
          <span>Use Redis for the response cache <span className="text-[#787884]">(optional)</span></span>
        </label>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <label className="lbl">Redis URL</label>
            <div className="flex gap-2">
              <input className="input mono" autoComplete="off" spellCheck={false} value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })}
                placeholder="redis://host:6379/0   ·   rediss://host:6380/0 for TLS" />
              <button className="btn whitespace-nowrap" onClick={runTest} disabled={busy === "test" || !f.url}>{busy === "test" ? <Loader2 size={15} className="animate-spin" /> : <Plug size={15} />} Test connection</button>
              {s.has_url && <button className="btn danger" title="Remove the Redis configuration (URL, username and password)" onClick={() => { if (confirm("Remove the saved Redis configuration and disable Redis?")) { setF({ ...f, redis_enabled: false, url: "", username: "", password: "" }); save({ redis_url: "", redis_enabled: false }); } }}><Trash2 size={15} /></button>}
            </div>
            {s.redis_url_source === "env" && <p className="mt-1 text-xs text-[#787884]">Preconfigured from the SG_REDIS_URL environment variable; saving here overrides it.</p>}
          </div>
          <div><label className="lbl">Username <span className="normal-case text-[#787884]">(optional, Redis 6+ ACL)</span></label>
            <input className="input mono" autoComplete="off" spellCheck={false} value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} placeholder="default" /></div>
          <div><label className="lbl">Password <span className="normal-case text-[#787884]">(optional)</span></label>
            <input className="input mono" type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })}
              placeholder={s.has_password ? "•••••••• saved — type to replace" : "no password"} />
            <p className="mt-1 text-xs text-[#787884]">{s.has_password ? "The saved password is stored encrypted and never shown. " : "Stored encrypted and never shown again. "}
              {s.has_password && <button type="button" className="text-[#a79cff] hover:underline" onClick={() => { if (confirm("Remove the saved Redis password?")) save({ redis_password: "" }); }}>Remove saved password</button>}</p></div>
          <div className="md:col-span-2">
            {test && (
              <div className={`mt-2 flex items-center gap-2 rounded-md border p-2.5 text-sm ${test.ok ? "border-[#5fb585]/30 bg-[#5fb585]/10 text-[#7fd1a3]" : "border-red-400/30 bg-red-400/10 text-red-300"}`}>
                {test.ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
                {test.ok ? `Connected to ${test.target} · Redis ${test.version} · ${test.latency_ms} ms` : test.error}
              </div>
            )}
          </div>
          <div><label className="lbl">Key prefix</label><input className="input mono" value={f.redis_key_prefix} onChange={(e) => setF({ ...f, redis_key_prefix: e.target.value })} />
            <p className="mt-1 text-xs text-[#787884]">Lets several apps or environments share one Redis safely.</p></div>
          <div className="flex items-end"><label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={f.cache_fallback_memory} onChange={(e) => setF({ ...f, cache_fallback_memory: e.target.checked })} />
            <span>Fall back to in-memory cache if Redis is unreachable<span className="block text-xs text-[#787884]">Recommended. Requests never fail because of the cache either way.</span></span></label></div>
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <div><label className="lbl">Default TTL (seconds)</label><input type="number" min={1} className="input" value={f.cache_default_ttl} onChange={(e) => setF({ ...f, cache_default_ttl: e.target.value })} />
            <p className="mt-1 text-xs text-[#787884]">Used by models that don't set their own TTL.</p></div>
          <div><label className="lbl">Max entry size (KB)</label><input type="number" min={1} className="input" value={f.cache_max_entry_kb} onChange={(e) => setF({ ...f, cache_max_entry_kb: e.target.value })} />
            <p className="mt-1 text-xs text-[#787884]">Larger responses are not cached.</p></div>
        </div>

        {msg && <div className={`mt-4 rounded-md border p-2.5 text-sm ${msg.ok ? "border-[#5fb585]/30 bg-[#5fb585]/10 text-[#7fd1a3]" : "border-red-400/30 bg-red-400/10 text-red-300"}`}>{msg.text}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn" onClick={clear} disabled={busy === "clear"}><Trash2 size={15} /> Clear cache</button>
          <button className="btn primary" onClick={() => save()} disabled={busy === "save"}>{busy === "save" ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} Save settings</button>
        </div>
      </div>

      <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <Stat icon={Database} label="Hit rate (since start)" value={stats?.hit_rate == null ? "–" : `${Math.round(stats.hit_rate * 100)}%`} />
        <Stat icon={Database} label="Hits / misses" value={stats ? `${fmt(stats.hits)} / ${fmt(stats.misses)}` : "–"} />
        <Stat icon={Database} label="Cached entries" value={stats?.entries == null ? "–" : fmt(stats.entries)} />
        <Stat icon={Database} label="Cache hits (24 h)" value={stats ? fmt(stats.hits_24h) : "–"} />
        <Stat icon={Database} label="Tokens saved (24 h)" value={stats ? fmt(stats.tokens_saved_24h) : "–"} tone="ok" />
      </div>
      </>}
      {tab === "compression" && <CompressionSettings />}
      {tab === "data" && <Retention />}
      {tab === "security" && <>
        <AdminPassword />
        <IpLimit />
        <PasswordHashCost />
        <p className="mt-4 text-sm text-[#8e8e9a]">Who changed what in the admin interface: <Link to="/logs?tab=audit" className="text-[#a79cff] hover:underline">view the audit log →</Link></p>
      </>}
    </div>
  );
}
