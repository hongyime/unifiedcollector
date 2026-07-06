import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";

// TikTok DMs — captured via the browser extension's WS-hook client-side
// protobuf decoder (Option B of #39). Populated in tiktok_dm{,_thread}
// through POST /social/dm-decoded. Empty state is expected on a fresh boot
// or if the user hasn't DMed with the v1.21.8+ extension installed.
//
// Unlike Instagram DMs (which come with sender_username via direct_v2
// responses), TikTok's frontier protocol only exposes sender_uid — a raw
// uint64 that maps to an account but doesn't carry a display name. The UI
// shows the UID + secUid; unifiedanalyzer's identity resolver can later
// backfill display names from social_users when the same UID appears in a
// browsed profile.
function awe_type_label(t: number | null): string {
  // Speculative mapping — see src/db/migrations/add_tiktok_dm_media_url.sql
  // for the source and add_tiktok_dm.sql for the canonical column.
  switch (t) {
    case 0: return "text";
    case 1: return "sticker";
    case 2: return "image";
    case 3: return "video";
    case 5: return "audio";
    case 6: return "gif";
    case 7: return "share";
    default: return t === null ? "unknown" : `awe_type=${t}`;
  }
}
export function TiktokDmPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const threads = useQuery({
    queryKey: ["tt-dm-threads"],
    queryFn: () => api.ttDmThreads(),
  });

  const thread = useQuery({
    queryKey: ["tt-dm-thread", selected],
    queryFn: () => api.ttDmThread(selected!),
    enabled: !!selected,
  });

  if (threads.error)
    return <ErrorState message={String(threads.error)} onRetry={() => threads.refetch()} />;

  return (
    <div>
      <Header
        title="TikTok DMs"
        subtitle="Direct messages decoded from the frontier WebSocket"
        onRefresh={() => threads.refetch()}
      />
      <div className="grid grid-cols-1 md:grid-cols-[320px_1fr] gap-4">
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {threads.isLoading ? (
            <LoadingSpinner />
          ) : !threads.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No DM threads captured yet. Open tiktok.com/messages in a
              logged-in browser tab with the extension active (v1.21.8+),
              then send or receive a DM to seed a thread.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[70vh] overflow-y-auto">
              {threads.data.map((t) => (
                <li
                  key={t.thread_id}
                  onClick={() => setSelected(t.thread_id)}
                  className={`px-4 py-3 cursor-pointer hover:bg-white/5 ${
                    selected === t.thread_id ? "bg-white/10" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium truncate font-mono">
                      {t.participants?.join(" ↔ ") ?? t.thread_id}
                    </span>
                    <span className="text-xs text-text-muted shrink-0">
                      {t.message_count ?? 0}
                    </span>
                  </div>
                  <div className="text-xs text-text-muted mt-0.5">
                    {t.last_activity || t.last_message_ts
                      ? relativeTime((t.last_activity || t.last_message_ts)!)
                      : "-"}
                    {t.owner_account ? ` · owner ${t.owner_account}` : ""}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="bg-surface rounded-lg border border-border p-4 min-h-[40vh]">
          {!selected ? (
            <p className="text-sm text-text-muted py-8 text-center">
              Select a thread to view messages.
            </p>
          ) : thread.isLoading ? (
            <LoadingSpinner />
          ) : !thread.data?.messages.length ? (
            <p className="text-sm text-text-muted py-8 text-center">
              No messages in this thread yet.
            </p>
          ) : (
            <div className="space-y-2 max-h-[70vh] overflow-y-auto">
              {thread.data.messages.map((m) => (
                <div
                  key={m.message_id}
                  className={`flex ${m.is_from_me ? "justify-end" : "justify-start"}`}
                >
                  <div
                    className={`max-w-[75%] rounded-lg px-3 py-2 text-sm text-text-primary ${
                      m.is_from_me ? "bg-info/20" : "bg-background"
                    }`}
                  >
                    {!m.is_from_me && (
                      <div className="text-xs text-text-muted mb-0.5 font-mono">
                        {m.sender_id || "unknown"}
                      </div>
                    )}
                    <div className="whitespace-pre-wrap break-words">
                      {m.text ? (
                        m.text
                      ) : m.media_url ? (
                        // Speculative media URL (from raw_content by aweType);
                        // may be a CDN URL requiring auth cookies to load.
                        <a href={m.media_url} target="_blank" rel="noopener noreferrer"
                           className="underline text-info">
                          {awe_type_label(m.awe_type)} · open
                        </a>
                      ) : (
                        <span className="italic text-text-muted">
                          (awe_type={m.awe_type ?? "?"} — non-text content)
                        </span>
                      )}
                    </div>
                    <div className="text-[10px] text-text-muted mt-1 text-right">
                      {m.timestamp ? relativeTime(m.timestamp) : ""}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
