import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { formatTimestamp } from "../../utils/formatters";
import type { Schedule } from "../../services/types";

// Sources whose PRIMARY collection is the continuous browser-extension in-tab loop
// (it ignores this schedule). For these the headless run below is only a gentle
// backup, so the "Next Run" is the backup's — not when data actually flows.
const EXTENSION_SOURCES = new Set(["instagram", "tiktok", "lemon8", "threads", "x", "facebook"]);

export function SchedulesPage() {
  const qc = useQueryClient();
  const [edits, setEdits] = useState<Record<string, number>>({});

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api.schedules(),
  });

  const update = useMutation({
    mutationFn: (s: Schedule) => api.updateSchedule(s.source, edits[s.source] ?? s.interval_hours, s.enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }),
  });

  const toggle = useMutation({
    mutationFn: (s: Schedule) => api.updateSchedule(s.source, s.interval_hours, !s.enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Schedules" subtitle="Collection intervals" onRefresh={() => refetch()} />
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">Source</th><th className="pb-2">Interval (hrs)</th><th className="pb-2">Enabled</th><th className="pb-2">Next Run</th><th className="pb-2" />
            </tr></thead>
            <tbody>
              {data?.map((s) => (
                <tr key={s.source} className="border-b border-border/50 hover:bg-white/5">
                  <td className="py-2 font-medium">
                    <span className="uppercase">{s.source}</span>
                    {EXTENSION_SOURCES.has(s.source) && (
                      <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-info/20 text-info align-middle normal-case">
                        ＋extension
                      </span>
                    )}
                  </td>
                  <td className="py-2">
                    <input
                      type="number"
                      min={1}
                      value={edits[s.source] ?? s.interval_hours}
                      onChange={(e) => setEdits({ ...edits, [s.source]: Number(e.target.value) })}
                      className="bg-background border border-border rounded-md text-sm px-2 py-1 w-20"
                    />
                  </td>
                  <td className="py-2">
                    <button
                      onClick={() => toggle.mutate(s)}
                      className={`w-10 h-5 rounded-full relative transition-colors ${s.enabled ? "bg-success" : "bg-text-muted/40"}`}
                    >
                      <span className={`block w-4 h-4 rounded-full bg-white absolute top-0.5 transition-transform ${s.enabled ? "translate-x-5" : "translate-x-0.5"}`} />
                    </button>
                  </td>
                  <td className="py-2 text-text-muted">
                    {EXTENSION_SOURCES.has(s.source) ? (
                      <span title="Primary path is the continuous extension; this is the headless backup run">
                        <span className="text-info">continuous</span>
                        <span className="text-text-muted/60"> · backup {formatTimestamp(s.next_run)}</span>
                      </span>
                    ) : (
                      formatTimestamp(s.next_run)
                    )}
                  </td>
                  <td className="py-2">
                    {edits[s.source] != null && edits[s.source] !== s.interval_hours && (
                      <button onClick={() => update.mutate(s)} className="text-xs text-info hover:underline">Save</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
