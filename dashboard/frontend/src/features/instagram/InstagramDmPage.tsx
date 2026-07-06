import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";
import type { DmTelemetryPlatform } from "../../services/types";

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

  // P1.2: passive DM WS-hook telemetry. Refreshes every 30s so a live IG
  // browsing session immediately shows up as fresh probe/sample counts.
  const telemetry = useQuery({
    queryKey: ["dm-telemetry"],
    queryFn: () => api.dmTelemetry(),
    refetchInterval: 30_000,
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
        onRefresh={() => {
          threads.refetch();
          telemetry.refetch();
        }}
      />

      {/* DM probe/sample telemetry — key signal is whether real IG DM
          frames (≥ 24B on edge-chat.instagram.com/chat) have arrived. */}
      <DmTelemetryPanel platforms={telemetry.data?.platforms ?? []} />

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

// P1.2: small counter panel above the DM view. Shows both IG and TikTok
// because the whole point is comparing: TikTok streams real 1KB protobuf
// frames every DM, Instagram has been stuck at 1–4B keepalive frames. The
// moment IG samples cross the SAMPLE_MIN_BYTES threshold (24B, capped in the
// extension) is when the decoder work becomes unblocked.
function DmTelemetryPanel({ platforms }: { platforms: DmTelemetryPlatform[] }) {
  const platformsSorted = [...platforms].sort((a, b) =>
    a.platform.localeCompare(b.platform),
  );
  const hasAny = platformsSorted.some(
    (p) => p.probe.all_time + p.sample.all_time > 0,
  );
  return (
    <div className="mb-4 bg-surface rounded-lg border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-text-primary">
          DM WS-hook telemetry
        </h3>
        <span className="text-xs text-text-muted">
          probes = distinct sockets seen · samples = binary frames saved for decoder
        </span>
      </div>
      {!hasAny ? (
        <p className="text-xs text-text-muted py-2">
          No probes recorded yet. Extension WS wrapper reports here on every
          DM socket it hooks; if this stays empty, the extension isn't
          installed or the content script isn't running on the platform tabs.
        </p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {platformsSorted.map((p) => (
            <PlatformTelemetryCard key={p.platform} p={p} />
          ))}
        </div>
      )}
    </div>
  );
}

function PlatformTelemetryCard({ p }: { p: DmTelemetryPlatform }) {
  const noSamples = p.sample.all_time === 0;
  const hookStale = p.hook?.last_seen
    ? Date.now() - new Date(p.hook.last_seen).getTime() > 60 * 60 * 1000  // 1h
    : false;
  return (
    <div className="bg-background rounded border border-border/60 p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-semibold capitalize">{p.platform}</span>
        <span className="text-[11px] text-text-muted">
          {p.probe.last_seen
            ? `last probe ${relativeTime(p.probe.last_seen)}`
            : "no probes yet"}
        </span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
        <div>
          <div className="text-text-muted">Probes</div>
          <div className="text-text-primary">
            <span className="font-mono">{p.probe.last_24h}</span>
            <span className="text-text-muted"> / 24h</span>
            <span className="text-text-muted"> · {p.probe.all_time} total</span>
          </div>
        </div>
        <div>
          <div className="text-text-muted">Samples</div>
          <div className={noSamples ? "text-warning" : "text-text-primary"}>
            <span className="font-mono">{p.sample.last_24h}</span>
            <span className="text-text-muted"> / 24h</span>
            <span className="text-text-muted"> · {p.sample.all_time} total</span>
          </div>
        </div>
      </div>
      {p.sample.max_frame_size !== null && (
        <div className="mt-2 text-[11px] text-text-muted">
          frame size: {p.sample.min_frame_size}–{p.sample.max_frame_size} B
          {p.sample.last_seen
            ? ` · last sample ${relativeTime(p.sample.last_seen)}`
            : ""}
        </div>
      )}
      {/* P1.3 hook status. Absent = never installed; stale = bundle change
          likely broke the hook (watchdog will alert). */}
      <div className="mt-2 border-t border-border/40 pt-2 text-[11px]">
        {!p.hook ? (
          <span className="text-text-muted">
            hook: never heard from (extension not installed on this platform)
          </span>
        ) : (
          <span className={hookStale ? "text-warning" : "text-text-muted"}>
            hook: v{p.hook.extension_version ?? "?"} ·{" "}
            {p.hook.last_seen
              ? `heartbeat ${relativeTime(p.hook.last_seen)}`
              : "no heartbeat yet"}
            {hookStale && " · STALE"}
          </span>
        )}
      </div>
    </div>
  );
}
