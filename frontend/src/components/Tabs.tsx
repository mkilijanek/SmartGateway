import { NavLink, useSearchParams } from "react-router-dom";

const cls = (on: boolean) => `pill ${on ? "on" : ""}`;

/** The selected tab lives in the URL (?tab=...), so it can be linked to and survives a refresh. */
export function useTab(keys: string[], fallback: string = keys[0]): [string, (k: string) => void] {
  const [sp, setSp] = useSearchParams();
  const v = sp.get("tab") || "";
  return [keys.includes(v) ? v : fallback, (k: string) => setSp((p) => { const n = new URLSearchParams(p); k === fallback ? n.delete("tab") : n.set("tab", k); return n; }, { replace: true })];
}

export function Tabs({ items, value, onChange }: { items: [string, string, React.ReactNode?][]; value: string; onChange: (k: string) => void }) {
  return (
    <div className="mb-5 flex flex-wrap gap-2" role="tablist">
      {items.map(([k, label, badge]) => <button key={k} role="tab" aria-selected={value === k} data-testid={`tab-${k}`} onClick={() => onChange(k)} className={cls(value === k)}>{label}{badge}</button>)}
    </div>
  );
}

/** Same look, but each tab is its own page (route). */
export function LinkTabs({ items }: { items: [string, string][] }) {
  return (
    <div className="mb-5 flex flex-wrap gap-2">
      {items.map(([to, label]) => <NavLink key={to} to={to} end className={({ isActive }) => cls(isActive)}>{label}</NavLink>)}
    </div>
  );
}

export const GATEWAY_TABS: [string, string][] = [["/gateways", "Smart"], ["/councils", "Council"], ["/unified", "Unified"]];
