import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { Button } from "../../components/ui/Button";
import { formatBytes, relativeTime } from "../../utils/formatters";
import type { MediaItem } from "../../services/types";

// Known content_type values (matches COLLECTION_SPEC storage conventions).
const CONTENT_TYPES = [
  "image", "photo", "profile_photo", "activity_photo", "thumbnail",
  "video", "story_video", "pdf", "document", "audio", "sticker", "post",
];

type MediaCategory = "image" | "video" | "pdf" | "audio" | "other";

function categoryOf(contentType: string): MediaCategory {
  const t = (contentType || "").toLowerCase();
  if (["image", "photo", "profile_photo", "user_profile_photo", "activity_photo",
       "thumbnail", "story", "sticker"].includes(t)) return "image";
  if (["video", "story_video", "reel"].includes(t)) return "video";
  if (t === "pdf") return "pdf";
  if (t === "audio") return "audio";
  return "other";
}

// Simple inline glyphs so we don't add an icon dependency.
function CategoryIcon({ category }: { category: MediaCategory }) {
  const glyph = category === "video" ? "▶" : category === "pdf" ? "PDF"
    : category === "audio" ? "♪" : "FILE";
  return (
    <div className="flex flex-col items-center justify-center text-text-muted">
      <span className="text-2xl font-semibold">{glyph}</span>
    </div>
  );
}

function Tile({ item, onClick }: { item: MediaItem; onClick: () => void }) {
  const cat = categoryOf(item.content_type);
  return (
    <div onClick={onClick} className="cursor-pointer group border border-border rounded-lg overflow-hidden hover:border-white/30 transition-colors">
      <div className="aspect-square bg-background flex items-center justify-center relative">
        {cat === "image" ? (
          <img
            src={api.thumbnailUrl(item.id)}
            alt={item.filename}
            className="w-full h-full object-cover"
            loading="lazy"
            onError={(e) => {
              // Fall back to an icon tile instead of a blank/broken image.
              const el = e.target as HTMLImageElement;
              el.style.display = "none";
              el.parentElement?.querySelector(".fallback-icon")?.classList.remove("hidden");
            }}
          />
        ) : null}
        {cat !== "image" && <CategoryIcon category={cat} />}
        <div className={`fallback-icon absolute inset-0 items-center justify-center ${cat === "image" ? "hidden flex" : "hidden"}`}>
          <CategoryIcon category={cat} />
        </div>
        <span className="absolute bottom-1 right-1 text-[10px] uppercase bg-black/60 px-1 rounded text-text-muted">
          {item.content_type}
        </span>
      </div>
      <div className="p-2">
        <p className="text-xs truncate">{item.filename}</p>
        <p className="text-xs text-text-muted">{formatBytes(item.file_size)}</p>
      </div>
    </div>
  );
}

function PreviewModal({ item, onClose }: { item: MediaItem; onClose: () => void }) {
  const cat = categoryOf(item.content_type);
  const fileUrl = api.fileUrl(item.id);
  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50" onClick={onClose}>
      <div className="max-w-3xl max-h-[80vh] flex flex-col items-center" onClick={(e) => e.stopPropagation()}>
        {cat === "image" && (
          <img src={api.thumbnailUrl(item.id)} alt={item.filename} className="max-w-full max-h-[70vh] object-contain rounded-lg" />
        )}
        {cat === "video" && (
          <video src={fileUrl} controls autoPlay className="max-w-full max-h-[70vh] rounded-lg bg-black" />
        )}
        {cat === "audio" && (
          <div className="p-8 flex flex-col items-center gap-4">
            <span className="text-5xl text-text-muted">♪</span>
            <audio src={fileUrl} controls autoPlay />
          </div>
        )}
        {(cat === "pdf" || cat === "other") && (
          <div className="p-8 flex flex-col items-center gap-4">
            <span className="text-4xl text-text-muted">{cat === "pdf" ? "PDF" : "FILE"}</span>
            <a href={fileUrl} target="_blank" rel="noopener noreferrer" className="text-sm text-blue-400 hover:underline">
              Open {cat === "pdf" ? "PDF" : "file"} in new tab ↗
            </a>
          </div>
        )}
        <div className="mt-3 text-center">
          <p className="text-sm font-medium">{item.filename}</p>
          <p className="text-xs text-text-muted">{item.entity_name} &middot; {item.content_type} &middot; {formatBytes(item.file_size)} &middot; {relativeTime(item.collected_at)}</p>
        </div>
        <button onClick={onClose} className="mt-3 text-sm text-text-muted hover:text-text-primary">Close</button>
      </div>
    </div>
  );
}

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
          <select value={contentType} onChange={(e) => { setContentType(e.target.value); setPage(1); }} className="bg-background border border-border rounded-md text-sm px-2 py-1.5 w-36">
            <option value="">All</option>
            {CONTENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
              {data?.items.map((item) => (
                <Tile key={item.id} item={item} onClick={() => setPreview(item)} />
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
      {preview && <PreviewModal item={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
