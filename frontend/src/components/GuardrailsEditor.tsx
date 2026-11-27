import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ShieldCheck } from "lucide-react";
import { api } from "../api";

const PII_TYPES: [string, string][] = [["email", "Email"], ["phone", "Phone"], ["card", "Credit card"], ["iban", "IBAN"], ["ssn", "US SSN"], ["pesel", "PL PESEL"], ["ip", "IP address"], ["secret", "Keys & secrets"]];
const DEFAULT = {
  pii: { mode: "off", types: PII_TYPES.map(([k]) => k) },
  injection: { mode: "off", sensitivity: "medium" },
  filters: { mode: "off", filter_ids: [] as number[], group_ids: [] as number[] },
  output: { mode: "off", json_schema: null as any, use_request_schema: true, max_retries: 2, feedback: true, on_fail: "error" },
};

export const guardrailsActive = (g: any) => !!g && ["pii", "injection", "filters", "output"].some((k) => g[k]?.mode && g[k].mode !== "off");

export default function GuardrailsEditor({ value, onChange, alwaysOpen }: { value: any; onChange: (v: any) => void; alwaysOpen?: boolean }) {
  const g = { ...DEFAULT, ...(value || {}), pii: { ...DEFAULT.pii, ...(value?.pii || {}) }, injection: { ...DEFAULT.injection, ...(value?.injection || {}) },
    filters: { ...DEFAULT.filters, ...(value?.filters || {}) }, output: { ...DEFAULT.output, ...(value?.output || {}) } };
  const [schemaText, setSchemaText] = useState(g.output.json_schema ? JSON.stringify(g.output.json_schema, null, 2) : "");
  const [schemaErr, setSchemaErr] = useState("");
  useEffect(() => { setSchemaText(value?.output?.json_schema ? JSON.stringify(value.output.json_schema, null, 2) : ""); }, [value === null]); // eslint-disable-line
  const set = (sec: string, patch: any) => onChange({ ...g, [sec]: { ...g[sec], ...patch } });
  const [lib, setLib] = useState<{ filters: any[]; groups: any[] }>({ filters: [], groups: [] });
  useEffect(() => { if (g.filters.mode === "on") api("/api/content-filters").then(setLib).catch(() => {}); }, [g.filters.mode]); // eslint-disable-line
  const toggle = (k: "filter_ids" | "group_ids", id: number, on: boolean) => set("filters", { [k]: on ? [...g.filters[k], id] : g.filters[k].filter((x: number) => x !== id) });
  const active = useMemo(() => {
    const ids = new Set<number>(g.filters.filter_ids);
    lib.groups.filter((x) => g.filters.group_ids.includes(x.id)).forEach((x) => x.filter_ids.forEach((i: number) => ids.add(i)));
    return lib.filters.filter((x) => ids.has(x.id) && x.enabled).length;
  }, [lib, g.filters.filter_ids, g.filters.group_ids]);
  const on = guardrailsActive(value);

  const editSchema = (text: string) => {
    setSchemaText(text);
    if (!text.trim()) { setSchemaErr(""); set("output", { json_schema: null }); return; }
    try { const s = JSON.parse(text); setSchemaErr(""); set("output", { json_schema: s }); } catch (e: any) { setSchemaErr("Not valid JSON yet — the last valid schema stays in effect"); }
  };

  return (
    <details className={`rounded-md border border-white/[0.08] p-3 ${alwaysOpen ? "" : "mt-4"}`} open={on || !!alwaysOpen}>
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium"><ShieldCheck size={15} /> Safety &amp; guardrails
        <span className="chip" style={{ color: on ? "#5fb585" : "#8e8e9a", background: (on ? "#5fb585" : "#8e8e9a") + "1f" }}>{on ? "active" : "off"}</span></summary>
      <p className="mt-2 text-xs text-[#787884]">All checks are off by default and cannot be bypassed by clients. Only counts and rule names are logged, never the matched text.</p>

      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div className="rounded-md border border-white/[0.06] p-3">
          <label className="lbl">Personal data (PII) in prompts</label>
          <select className="input" value={g.pii.mode} onChange={(e) => set("pii", { mode: e.target.value })}>
            <option value="off">Off</option><option value="log">Detect and log only</option><option value="redact">Redact before sending upstream</option><option value="block">Block the request</option></select>
          {g.pii.mode !== "off" && <div className="mt-2 grid grid-cols-2 gap-1">
            {PII_TYPES.map(([k, label]) => <label key={k} className="flex cursor-pointer items-center gap-2 text-xs"><input type="checkbox" checked={g.pii.types.includes(k)}
              onChange={(e) => set("pii", { types: e.target.checked ? [...g.pii.types, k] : g.pii.types.filter((x: string) => x !== k) })} /> {label}</label>)}</div>}
          {g.pii.mode === "redact" && <p className="mt-2 text-xs text-[#787884]">Values become placeholders such as [EMAIL_1]; the model never sees the original and the answer is not restored.</p>}
        </div>

        <div className="rounded-md border border-white/[0.06] p-3">
          <label className="lbl">Prompt injection</label>
          <select className="input" value={g.injection.mode} onChange={(e) => set("injection", { mode: e.target.value })}>
            <option value="off">Off</option><option value="log">Detect and log only</option><option value="block">Block the request</option></select>
          {g.injection.mode !== "off" && <>
            <label className="lbl mt-2">Sensitivity</label>
            <select className="input" value={g.injection.sensitivity} onChange={(e) => set("injection", { sensitivity: e.target.value })}>
              <option value="low">Low (only clear attacks)</option><option value="medium">Medium</option><option value="high">High (any suspicious phrase)</option></select>
            <p className="mt-2 text-xs text-[#787884]">Heuristic phrase matching on user messages and tool results. It catches common attacks, not all of them.</p></>}
        </div>

        <div className="rounded-md border border-white/[0.06] p-3 md:col-span-2">
          <label className="flex cursor-pointer items-center gap-3 text-sm font-medium"><input type="checkbox" checked={g.filters.mode === "on"} onChange={(e) => set("filters", { mode: e.target.checked ? "on" : "off" })} /> Content filtering
            <Link to="/filters" className="text-xs font-normal text-[#a79cff] hover:underline">Manage filters →</Link></label>
          {g.filters.mode === "on" && (lib.groups.length + lib.filters.length === 0
            ? <p className="mt-3 text-xs text-[#787884]">No content filters defined yet. <Link to="/filters" className="text-[#a79cff] hover:underline">Create some</Link>, then come back to pick them.</p>
            : <div className="mt-3 grid gap-4 md:grid-cols-2">
              <div><label className="lbl">Groups</label>
                <div className="max-h-44 space-y-1 overflow-y-auto rounded-md border border-white/[0.08] p-2">
                  {lib.groups.length === 0 && <span className="text-xs text-[#787884]">No groups</span>}
                  {lib.groups.map((x: any) => <label key={x.id} className="flex cursor-pointer items-center gap-2 text-sm"><input type="checkbox" checked={g.filters.group_ids.includes(x.id)} onChange={(e) => toggle("group_ids", x.id, e.target.checked)} /> {x.name} <span className="text-xs text-[#787884]">{x.filter_ids.length} filters</span></label>)}</div></div>
              <div><label className="lbl">Individual filters</label>
                <div className="max-h-44 space-y-1 overflow-y-auto rounded-md border border-white/[0.08] p-2">
                  {lib.filters.length === 0 && <span className="text-xs text-[#787884]">No filters</span>}
                  {lib.filters.map((x: any) => <label key={x.id} className="flex cursor-pointer items-center gap-2 text-sm" style={{ opacity: x.enabled ? 1 : 0.5 }}><input type="checkbox" checked={g.filters.filter_ids.includes(x.id)} onChange={(e) => toggle("filter_ids", x.id, e.target.checked)} /> {x.name}
                    <span className="text-xs text-[#787884]">{x.action === "block" ? "block" : "redact"} · {x.stage}{x.enabled ? "" : " · disabled"}</span></label>)}</div></div>
              <p className="text-xs text-[#787884] md:col-span-2">{active} active filter{active === 1 ? "" : "s"} apply to this model. Changes made on the Content Filters page take effect here immediately. Filters that apply to answers make the gateway collect the full reply before returning it.</p>
            </div>)}
        </div>

        <div className="rounded-md border border-white/[0.06] p-3 md:col-span-2">
          <label className="flex cursor-pointer items-center gap-3 text-sm font-medium"><input type="checkbox" checked={g.output.mode === "on"} onChange={(e) => set("output", { mode: e.target.checked ? "on" : "off" })} /> Output validation (JSON) with retry</label>
          {g.output.mode === "on" && <div className="mt-3 grid gap-4 md:grid-cols-2">
            <div className="md:col-span-2">
              <label className="lbl">JSON schema (optional)</label>
              <textarea className="input mono" rows={5} placeholder='{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}}}' value={schemaText} onChange={(e) => editSchema(e.target.value)} />
              {schemaErr && <p className="mt-1 text-xs text-[#d1a553]">{schemaErr}</p>}
              <p className="mt-1 text-xs text-[#787884]">Empty: the reply only has to be valid JSON — or, when the request carries a response_format with a schema, it is checked against that schema.</p>
            </div>
            <label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={!!g.output.use_request_schema} onChange={(e) => set("output", { use_request_schema: e.target.checked })} />
              <span>Use the request's response_format when no schema is set here</span></label>
            <label className="flex cursor-pointer items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={!!g.output.feedback} onChange={(e) => set("output", { feedback: e.target.checked })} />
              <span>Tell the model what was wrong when retrying</span></label>
            <div><label className="lbl">Retries when invalid</label><input type="number" min={0} max={5} className="input" value={g.output.max_retries} onChange={(e) => set("output", { max_retries: Math.max(0, Math.min(5, +e.target.value || 0)) })} /></div>
            <div><label className="lbl">If still invalid</label>
              <select className="input" value={g.output.on_fail} onChange={(e) => set("output", { on_fail: e.target.value })}><option value="error">Return an error (422)</option><option value="passthrough">Return the answer anyway (header X-SG-Guardrail)</option></select></div>
            <p className="text-xs text-[#787884] md:col-span-2">The gateway collects the complete answer before checking it, so streaming clients receive it as one burst once it is accepted.</p>
          </div>}
        </div>
      </div>
    </details>
  );
}
