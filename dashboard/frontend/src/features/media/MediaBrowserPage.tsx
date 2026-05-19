import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { Button } from "../../components/ui/Button";
import { formatBytes, relativeTime } from "../../utils/formatters";
import type { MediaItem } from "../../services/types";

export function MediaBrowserPage() {
  const [source, setSource] = useState("");
  const [entity, setEntity] = useState("");
  const [contentType, setContentType] = useState("");
  const [page, setPage] = useState(1);
  const [preview, setPreview] = useState<MediaItem | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["media-browse", source, entity, contentType, page],
    queryFn: () => api.mediaBrowse({ source: source || undefined, entity: entity || undefined, type: contentType || undefined, page }),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  const totalPages = data ? Math.ceil(data.total / data.page_size) : 0;

  return (
    <div>
      <Header title="Media Browser" subtitle={data ? `${data.total} items` : "Browse collected media"} onRefresh={() => refetch()} />
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <div>
          <label className="text-xs text-text-muted block mb-1">Source</label>
          <input value={source} onChange={(e) => { setSource(e.target.value); setPage(1); }} placeholder="All" className="bg-background border border-border rounded-md text-sm px-2 py-1.5 w-32" />
        </div>
        <div>
          <label className="text-xs text-text-muted block mb-1">Entity</label>
          <input value={entity} onChange={(e) => { setEntity(e.target.value); setPage(1); }} placeholder="All" className="bg-background border border-border rounded-md text-sm px-2 py-1.5 w-32" />
        </div>
        <div>
          <label className="text-xs text-text-muted block mb-1">Type</label>
          <input value={contentType} onChange={(e) => { setContentType(e.target.value); setPage(1); }} placeholder="All" className="bg-background border border-border rounded-md text-sm px-2 py-1.5 w-32" />
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
              {data?.items.map((item) => (
                <div key={item.id} onClick={() => setPreview(item)} className="cursor-pointer group border border-border rounded-lg overflow-hidden hover:border-white/30 transition-colors">
                  <div className="aspect-square bg-background flex items-center justify-center">
                    <img src={api.thumbnailUrl(Number(item.id))} alt={item.filename} className="w-full h-full object-cover" loading="lazy" onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
                  </div>
                  <div className="p-2">
                    <p className="text-xs truncate">{item.filename}</p>
                    <p className="text-xs text-text-muted">{formatBytes(item.file_size)}</p>
                  </div>
                </div>
              ))}
            </div>
            <div className="flex items-center justify-between mt-4">
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
              <span className="text-sm text-text-muted">Page {page} of {totalPages}</span>
              <Button size="sm" variant="ghost" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</Button>
            </div>
          </>
        )}
      </div>
      {preview && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50" onClick={() => setPreview(null)}>
          <div className="max-w-3xl max-h-[80vh] flex flex-col items-center" onClick={(e) => e.stopPropagation()}>
            <img src={api.thumbnailUrl(Number(preview.id))} alt={preview.filename} className="max-w-full max-h-[70vh] object-contain rounded-lg" />
            <div className="mt-3 text-center">
              <p className="text-sm font-medium">{preview.filename}</p>
              <p className="text-xs text-text-muted">{preview.entity_name} &middot; {preview.content_type} &middot; {relativeTime(preview.collected_at)}</p>
            </div>
            <button onClick={() => setPreview(null)} className="mt-3 text-sm text-text-muted hover:text-text-primary">Close</button>
          </div>
        </div>
      )}
    </div>
  );
}
