import { useMemo, useState } from "react";
import { AlertTriangle, ArrowDown, ChevronDown, ChevronUp, GripVertical, Plus, Power, Route, X, Zap } from "lucide-react";
import { CapChip, StatusDot } from "./ui";

/** A step of the chain: members that share load, tried before the next step. `hold` = a fallback step (waits for the steps above). */
export type Step = { hold: boolean; idxs: number[] };

/** Steps from the stored fields: primaries by priority, then fallbacks by priority (that is the order the scheduler uses them in). */
export function buildSteps(members: any[]): Step[] {
  const keyed = new Map<string, Step & { p: number }>();
  members.forEach((x, i) => {
    const hold = !!x.is_fallback, p = +x.priority || 0, key = `${hold ? 1 : 0}:${p}`;
    const s = keyed.get(key) || { hold, p, idxs: [] };
    s.idxs.push(i);
    keyed.set(key, s);
  });
  const sorted = [...keyed.values()].sort((a, b) => (a.hold === b.hold ? a.p - b.p : a.hold ? 1 : -1));
  return sorted.map(({ hold, idxs }, si) => ({ hold: si === 0 ? false : hold, idxs }));
}

/** Drop empty steps and keep the fallback flags in order: once a step waits for the ones above, so do all below it. */
function tidy(steps: Step[]): Step[] {
  const out = steps.filter((s) => s.idxs.length).map((s) => ({ ...s, idxs: [...s.idxs] }));
  let hold = false;
  return out.map((s, si) => { hold = hold || (si > 0 && s.hold); return { ...s, hold: si > 0 && hold }; });
}

/** Write the steps back: priority = position, is_fallback = the step waits for the ones above. */
export function applySteps(members: any[], steps: Step[]): any[] {
  const out = members.map((x) => ({ ...x }));
  tidy(steps).forEach((s, si) => s.idxs.forEach((i) => { out[i].priority = si; out[i].is_fallback = si > 0 && s.hold; }));
  return out;
}

const grace = 3;   // seconds a fallback step waits for busy primaries (SG_FALLBACK_GRACE)

export default function FallbackChain({ members, byId, strategy, maxRetries, available, onChange, onUseFailover, onAdd }: {
  members: any[]; byId: (id: number) => any; strategy: string; maxRetries: number;
  available: any[];                                    // models that can still be added
  onChange: (members: any[]) => void; onUseFailover: () => void; onAdd: (modelId: number, step: number | "new") => void;
}) {
  const steps = useMemo(() => buildSteps(members), [members]);
  const [drag, setDrag] = useState<number | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [down, setDown] = useState<Set<number>>(new Set());   // "what if this one fails": preview only, never saved
  const ordered = strategy === "priority_failover";

  const commit = (next: Step[]) => onChange(applySteps(members, next));
  const stepOf = (idx: number) => steps.findIndex((s) => s.idxs.includes(idx));
  const without = (idx: number) => steps.map((s) => ({ ...s, idxs: s.idxs.filter((i) => i !== idx) }));
  const moveTo = (idx: number, si: number) => { const n = without(idx); n[si].idxs.push(idx); commit(n); };
  /** a new step at position p; it behaves like the step it is inserted before (a fallback when it goes last) */
  const moveToNew = (idx: number, p: number) => {
    const n = without(idx), hold = p > 0 && (n[p]?.hold ?? true);
    n.splice(p, 0, { hold, idxs: [idx] });
    commit(n);
  };
  const remove = (idx: number) => {
    const keep = members.filter((_, i) => i !== idx);
    onChange(applySteps(keep, buildSteps(keep)));
  };
  const setHold = (si: number, hold: boolean) => commit(steps.map((s, j) => (hold ? (j >= si ? { ...s, hold: true } : s) : (j <= si ? { ...s, hold: false } : s))));
  const up = (idx: number) => { const si = stepOf(idx); if (si > 0) moveTo(idx, si - 1); };
  const downStep = (idx: number) => {
    const si = stepOf(idx);
    if (si < steps.length - 1) moveTo(idx, si + 1);
    else if (steps[si].idxs.length > 1) moveToNew(idx, steps.length);
  };

  const live = (x: any) => { const um = byId(x.model_id); return um && um.enabled !== false && x.enabled !== false && um.endpoint_status !== "offline" && !down.has(x.model_id); };
  const serving = ordered ? steps.findIndex((s) => s.idxs.some((i) => live(members[i]))) : -1;

  const dropProps = (key: string, onDrop: () => void) => ({
    onDragOver: (e: React.DragEvent) => { if (drag !== null) { e.preventDefault(); setOver(key); } },
    onDragLeave: () => setOver((o) => (o === key ? null : o)),
    onDrop: (e: React.DragEvent) => { e.preventDefault(); if (drag !== null) onDrop(); setDrag(null); setOver(null); },
  });

  /** A drop target that makes a new step. The one at the end is always there (so there is always an obvious place to drop a fallback);
   *  the ones in between only open up while something is being dragged. */
  const zone = (key: string, p: number, label: string, always = false) => (
    <div key={key} {...dropProps(key, () => drag !== null && moveToNew(drag, p))} data-testid={`zone-${key}`}
      className={`rounded-lg border border-dashed text-center text-xs transition-all ${drag !== null || always ? "py-3" : "py-0 opacity-0 h-0 overflow-hidden border-0"} ${over === key ? "border-[#a79cff] bg-[#6c5ce722] text-[#c9c2ff]" : "border-white/15 text-[#787884]"}`}>
      {label}
    </div>
  );

  return (
    <div className="mt-4 rounded-md border border-white/[0.08] p-4" data-testid="fallback-chain">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium"><Route size={16} className="text-[#a79cff]" /> Routing order &amp; fallbacks</div>
        {ordered && members.length > 0 && (
          <div className="text-xs text-[#8e8e9a]" data-testid="serving-now">
            {serving >= 0 ? <>Requests go to <b className="text-[#c8c8d0]">step {serving + 1}</b> right now{down.size ? " (with your simulated failures)" : ""}</> : <span className="text-red-300">No member is available{down.size ? " with these simulated failures" : ""}</span>}
          </div>
        )}
      </div>
      <p className="mb-3 text-xs text-[#787884]">
        Requests use the models in <b>step 1</b> first. If none of them can answer (down, busy, error, rate limit), the next step takes over, and so on.
        Models in the same step share the load. Drag models between steps, or use the arrows.
      </p>

      {!ordered && members.length > 1 && (
        <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-200" data-testid="strategy-hint">
          <AlertTriangle size={15} className="shrink-0" />
          <span className="flex-1">The strategy here is not “Priority with failover”, so traffic is spread over all steps and the order below only breaks ties (fallback steps are still held back).</span>
          <button className="btn !py-1" onClick={onUseFailover}>Use ordered failover for this model</button>
        </div>
      )}

      {members.length === 0 && <div className="rounded-md border border-dashed border-white/15 p-4 text-center text-sm text-[#8e8e9a]">No models yet. Add one below: it becomes step 1.</div>}

      <div className="space-y-1">
        {steps.map((s, si) => (
          <div key={si}>
            {si > 0 && zone(`z${si}`, si, "Drop here for a new step in between")}
            {si > 0 && (
              <div className="flex items-center gap-2 py-1.5 pl-5 text-xs text-[#787884]">
                <ArrowDown size={14} className="text-[#a79cff]" />
                {s.hold
                  ? <span>if the steps above are down or stay busy for over {grace}s, or fail</span>
                  : <span>as soon as the steps above are busy, or fail</span>}
                <label className="ml-auto flex cursor-pointer items-center gap-1.5" title={`Waiting gives the steps above ${grace} seconds to free up before this step is used, which keeps traffic on your preferred models.`}>
                  <input type="checkbox" checked={s.hold} onChange={(e) => setHold(si, e.target.checked)} data-testid={`hold-${si}`} /> wait for the steps above
                </label>
              </div>
            )}
            <div {...dropProps(`s${si}`, () => drag !== null && moveTo(drag, si))} data-testid={`step-${si}`}
              className={`rounded-lg border p-3 transition-colors ${over === `s${si}` ? "border-[#a79cff] bg-[#6c5ce722]" : serving === si ? "border-emerald-400/40 bg-emerald-400/[0.04]" : "border-white/10 bg-white/[0.02]"}`}>
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-[#8e8e9a]">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#6c5ce733] text-[11px] text-[#c9c2ff]">{si + 1}</span>
                {si === 0 ? "Primary" : `Fallback ${si}`}
                {serving === si && <span className="chip normal-case" style={{ color: "#4cc38a", background: "#4cc38a22" }}><Zap size={11} /> serving now</span>}
                {s.idxs.length > 1 && <span className="normal-case tracking-normal text-[#787884]">· {s.idxs.length} models share the load</span>}
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                {s.idxs.map((idx) => {
                  const x = members[idx], um = byId(x.model_id), isDown = down.has(x.model_id);
                  const off = um && um.enabled === false;
                  return (
                    <div key={idx} draggable onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(idx)); setTimeout(() => setDrag(idx), 0); }}
                      onDragEnd={() => { setDrag(null); setOver(null); }} data-testid={`member-${um?.upstream_id}`}
                      className={`flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.04] px-2 py-2 ${drag === idx ? "opacity-40" : ""} ${isDown || off ? "opacity-60" : ""}`}>
                      <GripVertical size={15} className="shrink-0 cursor-grab text-[#787884]" />
                      <StatusDot status={isDown ? "offline" : um?.endpoint_status || "unknown"} />
                      <div className="min-w-0 flex-1">
                        <div className="mono truncate text-sm" title={um?.upstream_id}>{um?.upstream_id || `model #${x.model_id}`}</div>
                        <div className="truncate text-xs text-[#787884]">@ {um?.endpoint_name}</div>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {off && <span className="chip" style={{ color: "#e0a458", background: "#e0a45822" }} title="Enable it on the Endpoints page, otherwise it is skipped">model disabled</span>}
                          {um?.endpoint_status === "offline" && <span className="chip" style={{ color: "#d9776f", background: "#d9776f22" }}>endpoint offline</span>}
                          {isDown && <span className="chip" style={{ color: "#d9776f", background: "#d9776f22" }}>simulated failure</span>}
                          {um?.capabilities?.slice(0, 3).map((c: string) => <CapChip key={c} cap={c} />)}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-col gap-0.5">
                        <button className="btn !p-1" disabled={si === 0} title="Move up a step" onClick={() => up(idx)} data-testid="up"><ChevronUp size={13} /></button>
                        <button className="btn !p-1" disabled={si === steps.length - 1 && s.idxs.length < 2} title="Move down a step" onClick={() => downStep(idx)} data-testid="down"><ChevronDown size={13} /></button>
                      </div>
                      <div className="flex shrink-0 flex-col gap-0.5">
                        <button className={`btn !p-1 ${isDown ? "primary" : ""}`} title="Preview: pretend this model fails (not saved)" onClick={() => setDown((d) => { const n = new Set(d); n.has(x.model_id) ? n.delete(x.model_id) : n.add(x.model_id); return n; })} data-testid="simulate"><Power size={13} /></button>
                        <button className="btn danger !p-1" title="Remove from this model" onClick={() => remove(idx)} data-testid="remove"><X size={13} /></button>
                      </div>
                    </div>
                  );
                })}
              </div>
              {available.length > 0 && (
                <select className="input mt-2 !w-auto !py-1 text-xs" value="" onChange={(e) => e.target.value && onAdd(+e.target.value, si)}>
                  <option value="">+ Add a model to this step…</option>
                  {available.map((um) => <option key={um.id} value={um.id}>{um.upstream_id} @ {um.endpoint_name}</option>)}
                </select>
              )}
            </div>
          </div>
        ))}
        {steps.length > 0 && zone("zend", steps.length, "Drag a model here to make it the next fallback step", true)}
      </div>

      {available.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <select className="input !w-auto" value="" onChange={(e) => e.target.value && onAdd(+e.target.value, "new")} data-testid="add-new-step">
            <option value="">{steps.length ? "+ Add as a new fallback step…" : "+ Add upstream model…"}</option>
            {available.map((um) => <option key={um.id} value={um.id}>{um.upstream_id} @ {um.endpoint_name} [{um.capabilities.join(", ")}]</option>)}
          </select>
          {down.size > 0 && <button className="btn" onClick={() => setDown(new Set())}><Plus size={14} className="rotate-45" /> Clear simulated failures</button>}
        </div>
      )}
      {down.size > 0 && available.length === 0 && <button className="btn mt-3" onClick={() => setDown(new Set())}>Clear simulated failures</button>}

      <p className="mt-3 text-xs text-[#787884]">
        If a model errors, is rate limited or times out mid-request, the gateway moves on to the next available model, up to {maxRetries} more time{maxRetries === 1 ? "" : "s"}.
        Models that are disabled on the Endpoints page, or whose endpoint is offline, are skipped.
      </p>
    </div>
  );
}
