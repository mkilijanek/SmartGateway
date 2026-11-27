import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import Logo from "./components/Logo";
import CommandPalette, { PaletteItem } from "./components/CommandPalette";
import { Activity, Users, LayoutGrid, LogOut, Plus, Search, MessageSquare, Network, Plug, Plug2, Boxes, Minimize2, ListFilter, ScrollText, Settings as SettingsIcon, Wallet } from "lucide-react";
import Settings from "./pages/Settings";
import Compression from "./pages/Compression";
import CostTracking from "./pages/CostTracking";
import Filters from "./pages/Filters";
import { api } from "./api";
import Login from "./pages/Login";
import Setup from "./pages/Setup";
import Playground from "./pages/Playground";
import { useLive } from "./live";
import Dashboard from "./pages/Dashboard";
import Endpoints from "./pages/Endpoints";
import Gateways from "./pages/Gateways";
import GatewayEditor from "./pages/GatewayEditor";
import CouncilEditor, { EditRoute } from "./pages/CouncilEditor";
import GatewayLive from "./pages/GatewayLive";
import UnifiedLive from "./pages/UnifiedLive";
import LiveAll from "./pages/LiveAll";
import Logs from "./pages/Logs";
import McpEndpoints from "./pages/McpEndpoints";
import McpGateways from "./pages/McpGateways";
import McpGatewayEditor from "./pages/McpGatewayEditor";
import Unified from "./pages/Unified";

type NavItem = { to: string; label: string; icon: any; end?: boolean; also?: string[] };
const groups: { title: string; items: NavItem[] }[] = [
  { title: "Overview", items: [
    { to: "/", label: "Dashboard", icon: LayoutGrid, end: true },
    { to: "/live", label: "Live", icon: Activity }] },
  { title: "LLM", items: [
    { to: "/endpoints", label: "Endpoints", icon: Plug },
    { to: "/gateways", label: "Gateways", icon: Network, also: ["/councils", "/unified"] },
    { to: "/playground", label: "Playground", icon: MessageSquare }] },
  { title: "MCP", items: [
    { to: "/mcp-endpoints", label: "MCP Servers", icon: Plug2 },
    { to: "/mcp-gateways", label: "MCP Gateways", icon: Boxes }] },
  { title: "Insights", items: [
    { to: "/logs", label: "Request Log", icon: ScrollText },
    { to: "/cost", label: "Cost Tracking", icon: Wallet },
    { to: "/compression", label: "Compression", icon: Minimize2 }] },
  { title: "Safety", items: [{ to: "/filters", label: "Content Filters", icon: ListFilter }] },
  { title: "System", items: [{ to: "/settings", label: "Settings", icon: SettingsIcon }] },
];
const nav = groups.flatMap((g) => g.items);
/** An item is active on its own path (and sub-paths) and on the extra paths it owns, e.g. Gateways also owns /councils and /unified. */
const isOn = (n: NavItem, path: string) => [n.to, ...(n.also || [])].some((p) => (p === "/" ? path === "/" : path === p || path.startsWith(p + "/")));

export default function App() {
  const [auth, setAuth] = useState<{ auth_required: boolean; authenticated: boolean; setup_required?: boolean } | null>(null);
  const refresh = () => api("/api/session").then(setAuth).catch(() => setAuth({ auth_required: false, authenticated: true }));
  useEffect(() => { refresh(); const h = () => setAuth({ auth_required: true, authenticated: false }); window.addEventListener("sg-unauth", h); return () => window.removeEventListener("sg-unauth", h); }, []);
  if (!auth) return null;
  if (auth.setup_required) return <Setup onDone={refresh} />;
  if (auth.auth_required && !auth.authenticated) return <Login onDone={refresh} />;
  return <Shell auth={auth} onLogout={async () => { await api("/api/logout", { method: "POST" }); refresh(); }} />;
}

/** Status dot shown next to a nav item: green = everything online, amber = something is offline. */
function useNavDots(): Record<string, string> {
  const [d, setD] = useState<Record<string, string>>({});
  useEffect(() => {
    const dot = (rows: any[]) => (rows.length === 0 ? "" : rows.every((r) => r.status === "online") ? "#4ade80" : "#e0a458");
    const load = () => Promise.all([api("/api/endpoints").catch(() => []), api("/api/mcp-endpoints").catch(() => [])])
      .then(([e, m]) => setD({ "/endpoints": dot(e), "/mcp-endpoints": dot(m) }));
    load(); const t = setInterval(load, 15000); return () => clearInterval(t);
  }, []);
  return d;
}

const quick: PaletteItem[] = [
  { label: "New gateway", to: "/gateways/new", hint: "action", icon: Plus },
  { label: "New council", to: "/councils/new", hint: "action", icon: Plus },
  { label: "New MCP gateway", to: "/mcp-gateways/new", hint: "action", icon: Plus },
  { label: "Unified endpoint", to: "/unified", hint: "Gateways", icon: Network },
  { label: "Audit log", to: "/logs?tab=audit", hint: "Request Log", icon: ScrollText },
  { label: "Cost tracking", to: "/cost", hint: "Insights", icon: Wallet },
  { label: "Plan limits (OpenCode, Codex, Claude)", to: "/cost?tab=limits", hint: "Cost Tracking", icon: Wallet },
  { label: "Model prices", to: "/cost?tab=prices", hint: "Cost Tracking", icon: Wallet },
  { label: "Change admin password", to: "/settings?tab=security", hint: "Settings", icon: SettingsIcon },
  { label: "Response cache and Redis", to: "/settings?tab=general", hint: "Settings", icon: SettingsIcon },
  { label: "Data retention", to: "/settings?tab=data", hint: "Settings", icon: SettingsIcon },
];

function Shell({ auth, onLogout }: { auth: { auth_required: boolean }; onLogout: () => void }) {
  const { connected, snapshot } = useLive();
  const path = useLocation().pathname;
  const dots = useNavDots();
  const [palette, setPalette] = useState(false);
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((v) => !v); } };
    window.addEventListener("keydown", h); return () => window.removeEventListener("keydown", h);
  }, []);
  const items: PaletteItem[] = [...nav.map((n) => ({ label: n.label, to: n.to, icon: n.icon, hint: "page" })), ...quick];
  const inflight = snapshot ? Object.values<any>(snapshot.models).reduce((a, m) => a + m.in_flight, 0) : 0;
  return (
    <div className="min-h-full p-3 lg:p-5">
     <CommandPalette items={items} open={palette} onClose={() => setPalette(false)} />
     <div className="app-window mx-auto flex min-h-[calc(100vh-40px)] max-w-[1800px]">
      <aside className="sticky top-5 hidden h-[calc(100vh-40px)] w-[260px] shrink-0 flex-col gap-3 rounded-l-[22px] border-r border-white/[0.06] bg-[#1f1f23] p-3 lg:flex">
        <div className="flex items-center gap-3 px-2 py-1.5">
          <Logo size={30} />
          <span className="text-[15px] font-semibold">SmartGateway</span>
        </div>
        <button onClick={() => setPalette(true)} className="flex items-center gap-2 rounded-xl border border-white/[0.08] bg-[#17171a] px-3 py-2 text-left text-[13px] text-[#787884] hover:border-white/20">
          <Search size={14} /><span className="flex-1">Jump to…</span><kbd className="rounded-md border border-white/10 px-1.5 py-0.5 text-[10px]">Ctrl K</kbd>
        </button>
        <nav className="scroll-thin flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
          {groups.map((g, gi) => (
            <div key={g.title} className={gi ? "mt-2" : ""}>
              <div className="px-3 pb-1 pt-1 text-[11px] text-[#787884]">{g.title}</div>
              {g.items.map((n) => (
                <NavLink key={n.to} to={n.to} end={n.end}
                  className={`flex items-center gap-3 rounded-lg px-3 py-[7px] text-[14px] transition ${isOn(n, path) ? "bg-white/[0.08] text-white" : "text-[#a2a2ac] hover:bg-white/[0.04]"}`}>
                  <n.icon size={17} /> <span className="flex-1">{n.label}</span>
                  {dots[n.to] && <span className="h-1.5 w-1.5 rounded-full" style={{ background: dots[n.to] }} />}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="border-t border-white/[0.06] pt-3">
          <div className="flex items-center gap-2 px-3 pb-2 text-xs text-[#8e8e9a]">
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: connected ? "#4ade80" : "#d9776f" }} />
            <span className="flex-1">{connected ? "Live stream connected" : "Reconnecting…"}</span>
            <span className="flex items-center gap-1 text-[#a79cff]"><Activity size={12} />{inflight}</span>
          </div>
          {auth.auth_required && (
            <button onClick={onLogout} className="flex w-full items-center gap-3 rounded-xl border border-white/[0.06] bg-[#17171a] px-3 py-2 text-left hover:border-white/20">
              <span className="grid h-8 w-8 place-items-center rounded-full bg-[#6c5ce7] text-xs font-semibold text-white">AD</span>
              <span className="flex-1 text-[13px]"><span className="block font-medium">Admin</span><span className="text-[11px] text-[#787884]">Sign out</span></span>
              <LogOut size={14} className="text-[#787884]" />
            </button>
          )}
        </div>
      </aside>
      <main className="min-w-0 flex-1 p-5 lg:p-7">
        <div className="mb-4 flex gap-2 overflow-x-auto lg:hidden">
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={`btn whitespace-nowrap ${isOn(n, path) ? "primary" : ""}`}>
              <n.icon size={16} /> {n.label}
            </NavLink>
          ))}
        </div>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/endpoints" element={<Endpoints />} />
          <Route path="/gateways" element={<Gateways />} />
          <Route path="/cost" element={<CostTracking />} />
          <Route path="/gateways/new" element={<GatewayEditor />} />
          <Route path="/councils" element={<Gateways kind="council" />} />
          <Route path="/councils/new" element={<CouncilEditor />} />
          <Route path="/gateways/:id/edit" element={<EditRoute />} />
          <Route path="/unified" element={<Unified />} />
          <Route path="/gateways/unified" element={<UnifiedLive />} />
          <Route path="/gateways/:id" element={<GatewayLive />} />
          <Route path="/mcp-endpoints" element={<McpEndpoints />} />
          <Route path="/mcp-gateways" element={<McpGateways />} />
          <Route path="/mcp-gateways/new" element={<McpGatewayEditor />} />
          <Route path="/mcp-gateways/:id/edit" element={<McpGatewayEditor />} />
          <Route path="/live" element={<LiveAll />} />
          <Route path="/playground" element={<Playground />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/compression" element={<Compression />} />
          <Route path="/filters" element={<Filters />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
     </div>
    </div>
  );
}
