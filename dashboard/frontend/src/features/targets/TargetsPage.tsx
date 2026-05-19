import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { Button } from "../../components/ui/Button";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";
import { SOURCES } from "../../utils/constants";
import { Trash2 } from "lucide-react";

const sourceOptions = [
  { value: "", label: "All sources" },
  ...SOURCES.map((s) => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) })),
];

export function TargetsPage() {
  const qc = useQueryClient();
  const [source, setSource] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ source: SOURCES[0] as string, target: "", priority: "0" });

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["targets", source],
    queryFn: () => api.targets(source || undefined),
  });

  const create = useMutation({
    mutationFn: () => api.createTarget(form.source, form.target, Number(form.priority)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["targets"] }); setShowForm(false); setForm({ source: SOURCES[0], target: "", priority: "0" }); },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteTarget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["targets"] }),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Targets" subtitle="Collection targets" onRefresh={() => refetch()} actions={<Button size="sm" onClick={() => setShowForm(!showForm)}>Add Target</Button>} />
      <div className="flex items-center gap-3 mb-4">
        <FilterDropdown label="Source" value={source} onChange={setSource} options={sourceOptions} />
      </div>
      {showForm && (
        <div className="bg-surface border border-border rounded-lg p-4 mb-4 flex items-end gap-3">
          <div>
            <label className="text-xs text-text-muted block mb-1">Source</label>
            <select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} className="bg-background border border-border rounded-md text-sm px-2 py-1.5">
              {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
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
          <Button size="sm" onClick={() => create.mutate()} loading={create.isPending} disabled={!form.target}>Save</Button>
        </div>
      )}
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">ID</th><th className="pb-2">Source</th><th className="pb-2">Target</th><th className="pb-2">Priority</th><th className="pb-2">Created</th><th className="pb-2" />
            </tr></thead>
            <tbody>
              {data?.map((t) => (
                <tr key={t.id} className="border-b border-border/50 hover:bg-white/5">
                  <td className="py-2">{t.id}</td>
                  <td className="py-2 uppercase text-xs">{t.source}</td>
                  <td className="py-2 font-medium">{t.target}</td>
                  <td className="py-2">{t.priority}</td>
                  <td className="py-2 text-text-muted">{relativeTime(t.created_at)}</td>
                  <td className="py-2"><button onClick={() => remove.mutate(t.id)} className="text-error hover:text-error/80"><Trash2 className="w-4 h-4" /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
