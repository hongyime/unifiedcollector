import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";

// Instagram DMs are captured ban-safely: the extension OBSERVES the direct_v2
// responses the logged-in page already fetches (no extra requests). Tables stay
// empty until the user opens IG DMs in a logged-in tab, so an empty state here
// is expected, not an error.
export function InstagramDmPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const threads = useQuery({
    queryKey: ["ig-dm-threads"],
    queryFn: () => api.igDmThreads(),
  });

  const thread = useQuery({
    queryKey: ["ig-dm-thread", selected],
    queryFn: () => api.igDmThread(selected!),
    enabled: !!selected,
  });

  if (threads.error)
    return <ErrorState message={String(threads.error)} onRetry={() => threads.refetch()} />;

  return (
    <div>
      <Header
        title="Instagram DMs"
        subtitle="Direct messages observed from your logged-in session"
        onRefresh={() => threads.refetch()}
      />
      <div className="grid grid-cols-1 md:grid-cols-[320px_1fr] gap-4">
        {/* Thread list */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {threads.isLoading ? (
            <LoadingSpinner />
          ) : !threads.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No DM threads captured yet. Open Instagram Direct in a logged-in
              browser tab with the extension active to start collecting.
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
                    <span className="text-sm font-medium truncate">
                      {t.title || (t.participants?.join(", ") ?? t.thread_id)}
                    </span>
                    <span className="text-xs text-text-muted shrink-0">
                      {t.message_count ?? 0}
                    </span>
                  </div>
                  <div className="text-xs text-text-muted mt-0.5">
                    {t.last_activity || t.last_message_ts
                      ? relativeTime((t.last_activity || t.last_message_ts)!)
                      : "-"}
                    {t.owner_account ? ` · @${t.owner_account}` : ""}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Message pane */}
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
                      <div className="text-xs text-text-muted mb-0.5">
                        {m.sender_username || m.sender_id || "unknown"}
                      </div>
                    )}
                    <div className="whitespace-pre-wrap break-words">
                      {m.text || (
                        <span className="italic text-text-muted">
                          ({m.item_type || "media"})
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
