import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../services/api";
import type { Target } from "../../services/types";
import { Header } from "../../components/layout/Header";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { Button } from "../../components/ui/Button";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";
import { SOURCES } from "../../utils/constants";
import { Trash2 } from "lucide-react";

const SOURCE_LABELS: Record<string, string> = {
  github: "GitHub",
  website: "Website",
  instagram: "Instagram",
  telegram: "Telegram",
  tiktok: "TikTok",
  youtube: "YouTube",
  lemon8: "Lemon8",
  strava: "Strava",
  whatsapp: "WhatsApp",
  search: "Search",
};

const friendly = (s: string) => SOURCE_LABELS[s] ?? (s.charAt(0).toUpperCase() + s.slice(1));

function targetName(t: Target): string {
  return t.target_id ?? t.target ?? "";
}

function analyzerHint(t: Target): Record<string, unknown> | null {
  const hint = t.metadata?.analyzer_priority_hint;
  return hint && typeof hint === "object" && !Array.isArray(hint) ? hint as Record<string, unknown> : null;
}

function confidenceLabel(value: unknown): string | null {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  const pct = n <= 1 ? n * 100 : n;
  return `${Math.round(pct)}%`;
}

const sourceOptions = [
  { value: "", label: "All sources" },
  ...SOURCES.map((s) => ({ value: s, label: friendly(s) })),
];

type ConflictData = {
  source: string;
  target_id: string;
  discovered_via: string | null;
  last_seen: string | null;
};

export function TargetsPage() {
  const qc = useQueryClient();
  const [source, setSource] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ source: SOURCES[0] as string, target: "", priority: "0" });
  const [conflictData, setConflictData] = useState<ConflictData | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["targets", source],
    queryFn: () => api.targets(source || undefined),
  });

  // Your REAL captured network (social_users) — the Targets table below is just a
  // manual seed list, so this explains why it's tiny even though we track your
  // whole follow graph.
  const network = useQuery({
    queryKey: ["social-network"],
    queryFn: () => api.socialNetwork(),
    refetchInterval: 60_000,
  });

  const resetForm = () => {
    setShowForm(false);
    setForm({ source: SOURCES[0], target: "", priority: "0" });
  };

  const create = useMutation({
    mutationFn: (force: boolean = false) =>
      api.createTarget(form.source, form.target, Number(form.priority), force),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["targets"] });
      resetForm();
      setConflictData(null);
    },
    onError: (err: unknown) => {
      const e = err as { status?: number; detail?: unknown };
      if (e?.status === 409 && e.detail && typeof e.detail === "object") {
        const d = e.detail as Record<string, unknown>;
        if (d.code === "already_discovered") {
          setConflictData({
            source: String(d.source ?? form.source),
            target_id: String(d.target_id ?? form.target),
            discovered_via: (d.discovered_via as string | null) ?? null,
            last_seen: (d.last_seen as string | null) ?? null,
          });
        }
      }
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteTarget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["targets"] }),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Targets" subtitle="Collection targets" onRefresh={() => refetch()} actions={<Button size="sm" onClick={() => setShowForm(!showForm)}>Add Target</Button>} />

      {/* Your real captured network — the seed table below is separate/manual. */}
      <div className="bg-surface border border-border rounded-lg p-4 mb-4">
        <div className="flex items-baseline justify-between mb-2">
          <h3 className="text-sm font-medium">Your captured network</h3>
          <span className="text-[11px] text-text-muted">from social_users — the follow graph (the table below is a manual seed list)</span>
        </div>
        {network.isLoading ? (
          <span className="text-xs text-text-muted">loading…</span>
        ) : (
          <div className="flex flex-wrap gap-2">
            {(network.data ?? []).filter((n) => n.total > 0).map((n) => (
              <div key={n.platform} className="bg-background border border-border rounded-md px-3 py-2 min-w-[120px]">
                <div className="text-xs uppercase text-text-muted">{n.platform}</div>
                <div className="text-lg font-semibold tabular-nums">{n.total.toLocaleString()}</div>
                <div className="text-[11px] text-text-secondary tabular-nums">
                  {n.following.toLocaleString()} following · {n.followers.toLocaleString()} followers
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="flex items-center gap-3 mb-4">
        <FilterDropdown label="Source" value={source} onChange={setSource} options={sourceOptions} />
      </div>
      {showForm && (
        <div className="bg-surface border border-border rounded-lg p-4 mb-4 flex items-end gap-3">
          <div>
            <label className="text-xs text-text-muted block mb-1">Source</label>
            <select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} className="bg-background border border-border rounded-md text-sm px-2 py-1.5">
              {SOURCES.map((s) => <option key={s} value={s}>{friendly(s)}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-text-muted block mb-1">Target</label>
            <input value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} className="bg-background border border-border rounded-md text-sm px-2 py-1.5" placeholder="username or URL" />
          </div>
          <div>
            <label className="text-xs text-text-muted block mb-1">Priority</label>
            <input type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} className="bg-background border border-border rounded-md text-sm px-2 py-1.5 w-20" />
          </div>
          <Button size="sm" onClick={() => create.mutate(false)} loading={create.isPending} disabled={!form.target}>Save</Button>
        </div>
      )}
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">ID</th><th className="pb-2">Source</th><th className="pb-2">Target</th><th className="pb-2">Priority</th><th className="pb-2">Provenance</th><th className="pb-2">Created</th><th className="pb-2" />
            </tr></thead>
            <tbody>
              {data?.map((t) => {
                const hint = analyzerHint(t);
                const hintType = String(hint?.hint_type ?? "").replaceAll("_", " ");
                const confidence = confidenceLabel(hint?.confidence);
                return (
                  <tr key={t.id} className="border-b border-border/50 hover:bg-white/5">
                    <td className="py-2">{t.id}</td>
                    <td className="py-2 uppercase text-xs">{friendly(t.source)}</td>
                    <td className="py-2 font-medium">{targetName(t)}</td>
                    <td className="py-2">{t.priority}</td>
                    <td className="py-2 max-w-[240px]">
                      {hint ? (
                        <div>
                          <span className="inline-flex items-center rounded border border-sky-400/40 bg-sky-400/10 px-2 py-0.5 text-[11px] text-sky-200">
                            Analyzer{confidence ? ` · ${confidence}` : ""}
                          </span>
                          {hintType ? <div className="mt-1 truncate text-[11px] text-text-muted" title={hintType}>{hintType}</div> : null}
                        </div>
                      ) : (
                        <span className="text-xs text-text-muted">manual/local</span>
                      )}
                    </td>
                    <td className="py-2 text-text-muted">{relativeTime(t.created_at)}</td>
                    <td className="py-2"><button onClick={() => remove.mutate(t.id)} className="text-error hover:text-error/80"><Trash2 className="w-4 h-4" /></button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {conflictData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
          <div className="bg-surface border border-border rounded-lg p-6 max-w-md w-full shadow-xl">
            <h3 className="text-lg font-semibold mb-3">Already discovered</h3>
            <p className="text-sm text-text-muted mb-2">
              <span className="font-mono">{friendly(conflictData.source)}/{conflictData.target_id}</span>
            </p>
            <p className="text-sm mb-4">
              Already discovered via spider graph from{" "}
              <span className="font-medium">
                {conflictData.discovered_via || "a logged-in account"}
              </span>
              . Adding explicitly will bump priority. Add anyway?
            </p>
            {conflictData.last_seen && (
              <p className="text-xs text-text-muted mb-4">Last seen: {relativeTime(conflictData.last_seen)}</p>
            )}
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="ghost" onClick={() => setConflictData(null)}>Cancel</Button>
              <Button size="sm" onClick={() => create.mutate(true)} loading={create.isPending}>Add anyway</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
