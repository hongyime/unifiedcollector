import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { Button } from "../../components/ui/Button";
import { MetricCard } from "../../components/ui/MetricCard";
import { AuthImage } from "../../components/ui/AuthImage";
import { formatNumber, relativeTime } from "../../utils/formatters";
import type { MediaItem } from "../../services/types";

// Ephemeral media lives under media_items.kind ('story' | 'highlight'), NOT
// content_type — this page surfaces the stories/highlights the collectors
// already capture (Instagram today; whatsapp/telegram/tiktok as they land).

const KIND_OPTIONS = [
  { value: "story,highlight", label: "Stories + Highlights" },
  { value: "story", label: "Stories only" },
  { value: "highlight", label: "Highlights only" },
];

function isVideo(item: MediaItem): boolean {
  const t = (item.content_type || "").toLowerCase();
  return t === "video" || t === "story_video" || t === "reel";
}

function StoryTile({ item, onClick }: { item: MediaItem; onClick: () => void }) {
  return (
    <div
      onClick={onClick}
      className="cursor-pointer group border border-border rounded-lg overflow-hidden hover:border-white/30 transition-colors"
    >
      <div className="aspect-[9/16] bg-background flex items-center justify-center relative">
        <AuthImage
          src={api.thumbnailUrl(item.id)}
          alt={item.filename}
          className="w-full h-full object-cover"
          fallbackLabel={item.kind || "story"}
        />
        {isVideo(item) && (
          <span className="absolute top-1 right-1 text-[10px] bg-black/70 px-1 rounded">▶</span>
        )}
        <span className="absolute bottom-1 left-1 text-[10px] uppercase bg-black/60 px-1 rounded text-text-muted">
          {item.kind || "story"}
        </span>
      </div>
      <div className="p-1.5">
        <p className="text-xs truncate">{item.entity_name}</p>
        <p className="text-[10px] text-text-muted">{relativeTime(item.collected_at)}</p>
      </div>
    </div>
  );
}

function StoryModal({ item, onClose }: { item: MediaItem; onClose: () => void }) {
  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50" onClick={onClose}>
      <div className="flex flex-col items-center" onClick={(e) => e.stopPropagation()}>
        {isVideo(item) ? (
          <video src={api.fileUrl(item.id)} controls autoPlay className="max-w-full max-h-[75vh] rounded-lg bg-black" />
        ) : (
          <AuthImage src={api.fileUrl(item.id)} alt={item.filename} className="max-w-full max-h-[75vh] object-contain rounded-lg" />
        )}
        <div className="mt-3 text-center">
          <p className="text-sm font-medium">{item.entity_name}</p>
          <p className="text-xs text-text-muted">
            {item.source} &middot; {item.kind || "story"} &middot; {relativeTime(item.collected_at)}
          </p>
        </div>
        <button onClick={onClose} className="mt-3 text-sm text-text-muted hover:text-text-primary">Close</button>
      </div>
    </div>
  );
}

export function StoriesPage() {
  const [kind, setKind] = useState("story,highlight");
  const [entity, setEntity] = useState("");
  const [page, setPage] = useState(1);
  const [preview, setPreview] = useState<MediaItem | null>(null);

  const overview = useQuery({
    queryKey: ["stories-overview"],
    queryFn: () => api.storiesOverview(),
  });

  const browse = useQuery({
    queryKey: ["stories-browse", kind, entity, page],
    queryFn: () =>
      api.mediaBrowse({ kind, entity: entity || undefined, page, pageSize: 30 }),
  });

  if (overview.error)
    return <ErrorState message={String(overview.error)} onRetry={() => overview.refetch()} />;

  const stats = overview.data?.stats;
  const totalPages = browse.data ? Math.ceil(browse.data.total / browse.data.page_size) : 0;

  return (
    <div>
      <Header
        title="Stories & Highlights"
        subtitle="Ephemeral media captured across platforms"
        onRefresh={() => { overview.refetch(); browse.refetch(); }}
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <MetricCard label="Stories" value={formatNumber(stats?.stories ?? 0)} status="info" />
        <MetricCard label="Highlights" value={formatNumber(stats?.highlights ?? 0)} status="info" />
        <MetricCard label="Accounts" value={formatNumber(stats?.accounts ?? 0)} status="info" />
        <MetricCard label="Newest" value={stats?.newest ? relativeTime(stats.newest) : "—"} status="info" />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4">
        {/* Account list */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          <div className="px-3 py-2 border-b border-border text-xs text-text-muted uppercase tracking-wide">
            Accounts
          </div>
          {overview.isLoading ? (
            <LoadingSpinner />
          ) : (
            <ul className="divide-y divide-border/50 max-h-[70vh] overflow-y-auto">
              <li
                onClick={() => { setEntity(""); setPage(1); }}
                className={`px-3 py-2 cursor-pointer hover:bg-white/5 text-sm ${entity === "" ? "bg-white/10" : ""}`}
              >
                All accounts
              </li>
              {overview.data?.accounts.map((a) => (
                <li
                  key={`${a.source}/${a.entity_name}`}
                  onClick={() => { setEntity(a.entity_name); setPage(1); }}
                  className={`px-3 py-2 cursor-pointer hover:bg-white/5 ${entity === a.entity_name ? "bg-white/10" : ""}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm truncate">{a.entity_name}</span>
                    <span className="text-[10px] text-text-muted shrink-0">{a.total}</span>
                  </div>
                  <div className="text-[10px] text-text-muted">
                    {a.source} · {a.story_count} stories · {a.highlight_count} hl
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Grid */}
        <div className="bg-surface rounded-lg border border-border p-4">
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            {KIND_OPTIONS.map((o) => (
              <button
                key={o.value}
                onClick={() => { setKind(o.value); setPage(1); }}
                className={`text-xs px-2.5 py-1 rounded border ${kind === o.value ? "border-white/40 bg-white/10" : "border-border text-text-muted hover:text-text-primary"}`}
              >
                {o.label}
              </button>
            ))}
            {entity && (
              <span className="text-xs text-text-muted ml-2">filtered: <b>{entity}</b></span>
            )}
          </div>
          {browse.isLoading ? (
            <LoadingSpinner />
          ) : !browse.data?.items.length ? (
            <p className="text-sm text-text-muted py-10 text-center">No ephemeral media for this filter yet.</p>
          ) : (
            <>
              <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 gap-3">
                {browse.data.items.map((item) => (
                  <StoryTile key={item.id} item={item} onClick={() => setPreview(item)} />
                ))}
              </div>
              <div className="flex items-center justify-between mt-4">
                <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
                <span className="text-sm text-text-muted">Page {page} of {totalPages || 1}</span>
                <Button size="sm" variant="ghost" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</Button>
              </div>
            </>
          )}
        </div>
      </div>

      {preview && <StoryModal item={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
