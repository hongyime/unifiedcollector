import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { Button } from "../../components/ui/Button";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime, formatNumber } from "../../utils/formatters";

export function FacesPage() {
  const qc = useQueryClient();
  const [editId, setEditId] = useState<string | null>(null);
  const [labelVal, setLabelVal] = useState("");
  const [mergeSelection, setMergeSelection] = useState<string[]>([]);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["faces"],
    queryFn: () => api.faces(),
  });

  const labelMut = useMutation({
    mutationFn: ({ id, label }: { id: string; label: string }) => api.labelFace(id, label),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["faces"] }); setEditId(null); },
  });

  const mergeMut = useMutation({
    mutationFn: () => api.mergeFaces(mergeSelection[0], mergeSelection[1]),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["faces"] }); setMergeSelection([]); },
  });

  const toggleMerge = (id: string) => {
    setMergeSelection((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length < 2 ? [...prev, id] : prev,
    );
  };

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Face Identities" subtitle="WhatsApp face recognition" onRefresh={() => refetch()} actions={
        mergeSelection.length === 2 ? <Button size="sm" onClick={() => mergeMut.mutate()} loading={mergeMut.isPending}>Merge Selected</Button> : undefined
      } />
      {mergeSelection.length > 0 && (
        <p className="text-xs text-text-muted mb-3">Select {2 - mergeSelection.length} more to merge. <button onClick={() => setMergeSelection([])} className="text-info hover:underline">Clear</button></p>
      )}
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
            {data?.map((face) => (
              <div key={face.id} className={`border rounded-lg p-3 transition-colors ${mergeSelection.includes(face.id) ? "border-info bg-info/10" : "border-border hover:border-white/30"}`}>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-mono text-text-muted truncate">{face.id.slice(0, 8)}</span>
                  <input type="checkbox" checked={mergeSelection.includes(face.id)} onChange={() => toggleMerge(face.id)} className="accent-info" />
                </div>
                {editId === face.id ? (
                  <div className="flex gap-1">
                    <input value={labelVal} onChange={(e) => setLabelVal(e.target.value)} className="bg-background border border-border rounded text-xs px-1 py-0.5 flex-1 min-w-0" autoFocus />
                    <button onClick={() => labelMut.mutate({ id: face.id, label: labelVal })} className="text-xs text-info">OK</button>
                  </div>
                ) : (
                  <button onClick={() => { setEditId(face.id); setLabelVal(face.label ?? ""); }} className="text-sm font-medium truncate w-full text-left hover:text-info">
                    {face.label || "Unlabeled"}
                  </button>
                )}
                <div className="mt-2 text-xs text-text-muted space-y-0.5">
                  <p>{formatNumber(face.occurrence_count)} occurrences</p>
                  <p>Last: {relativeTime(face.last_seen)}</p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
