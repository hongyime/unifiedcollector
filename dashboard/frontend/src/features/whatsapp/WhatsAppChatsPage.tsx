import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime } from "../../utils/formatters";
import { formatBytes } from "../../utils/formatters";
import type { WaChat, WaMessage } from "../../services/types";

// WhatsApp chats page — two-pane, mirrors InstagramDmPage. Chats come from the
// Baileys bridge → RabbitMQ → collector pipeline; media files land in
// media_items so the message row's media_id is enough to build a thumbnail URL
// through the existing /media/{id}/thumbnail endpoint.

// Colour cues so the four chat_type values are readable at a glance.
// dm = 1:1; group = the usual multi-participant thread; channel = one-way
// broadcast from an owner; broadcast = the personal "Broadcast lists" fanout.
const TYPE_STYLES: Record<string, string> = {
  dm: "bg-info/20 text-info",
  group: "bg-purple-500/20 text-purple-200",
  channel: "bg-emerald-500/20 text-emerald-200",
  broadcast: "bg-amber-500/20 text-amber-200",
};

function ChatTypeBadge({ type }: { type: string }) {
  const cls = TYPE_STYLES[type] ?? "bg-white/10 text-text-secondary";
  return (
    <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded font-medium uppercase tracking-wide ${cls}`}>
      {type}
    </span>
  );
}

function previewText(chat: WaChat): string {
  if (chat.last_text) return chat.last_text;
  if (chat.last_media_mime) {
    if (chat.last_media_mime.startsWith("image/")) return "📷 Photo";
    if (chat.last_media_mime.startsWith("video/")) return "🎥 Video";
    if (chat.last_media_mime.startsWith("audio/")) return "🎤 Audio";
    return "📎 Attachment";
  }
  return "";
}

function senderLabel(m: WaMessage): string {
  if (m.from_me) return "You";
  return m.sender_pushname || m.sender_name || m.sender_phone || m.sender_jid || "unknown";
}

// Only images and videos get an inline preview; everything else (audio, docs,
// stickers under some mime configs) renders as a small badge with the mime.
function isInlineMedia(mime: string | null): boolean {
  if (!mime) return false;
  return mime.startsWith("image/") || mime.startsWith("video/");
}

function MessageMedia({ m }: { m: WaMessage }) {
  if (!m.media_mime_type) return null;
  const label = m.media_mime_type + (m.media_size ? ` · ${formatBytes(m.media_size)}` : "");

  if (!isInlineMedia(m.media_mime_type) || !m.media_id) {
    return (
      <div className="mt-1 text-[11px] text-text-muted italic">
        📎 {label}
      </div>
    );
  }

  if (m.media_mime_type.startsWith("image/")) {
    return (
      <a href={api.fileUrl(m.media_id)} target="_blank" rel="noopener noreferrer" className="block mt-1">
        <AuthImage
          src={api.thumbnailUrl(m.media_id)}
          alt=""
          className="max-w-[240px] max-h-[240px] rounded border border-border/40"
          fallbackLabel="media"
        />
      </a>
    );
  }

  // Video: use the /file endpoint (Range-friendly) directly inside <video>.
  return (
    <video
      src={api.fileUrl(m.media_id)}
      controls
      preload="metadata"
      className="mt-1 max-w-[280px] max-h-[280px] rounded border border-border/40"
    />
  );
}

export function WhatsAppChatsPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const chats = useQuery({
    queryKey: ["wa-chats"],
    queryFn: () => api.waChats(100),
    // Realtime bridge streams new messages constantly; refresh so the list
    // reorders when a chat gets a new message.
    refetchInterval: 20_000,
  });

  const chat = useQuery({
    queryKey: ["wa-chat", selected],
    queryFn: () => api.waChat(selected!),
    enabled: !!selected,
  });

  if (chats.error)
    return <ErrorState message={String(chats.error)} onRetry={() => chats.refetch()} />;

  return (
    <div>
      <Header
        title="WhatsApp Chats"
        subtitle="Live messages from the Baileys bridge (DMs, groups, channels, broadcasts)"
        onRefresh={() => {
          chats.refetch();
          if (selected) chat.refetch();
        }}
      />

      <div className="grid grid-cols-1 md:grid-cols-[340px_1fr] gap-4">
        {/* Chat list */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {chats.isLoading ? (
            <LoadingSpinner />
          ) : !chats.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No WhatsApp chats yet. Link a device on the "Link Device" page and
              wait for the bridge to sync history + new messages.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[75vh] overflow-y-auto">
              {chats.data.map((c) => {
                const isActive = selected === c.platform_chat_id;
                const ts = c.last_message_ts || c.updated_at;
                const preview = previewText(c);
                return (
                  <li
                    key={c.platform_chat_id}
                    onClick={() => setSelected(c.platform_chat_id)}
                    className={`px-4 py-3 cursor-pointer hover:bg-white/5 ${
                      isActive ? "bg-white/10" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium truncate flex-1">
                        {c.name || c.platform_chat_id}
                      </span>
                      <ChatTypeBadge type={c.chat_type} />
                    </div>
                    <div className="flex items-center gap-2 mt-0.5 text-xs text-text-muted">
                      {(c.participant_count ?? 0) > 0 && (
                        <span className="tabular-nums shrink-0">
                          {c.participant_count} ppl
                        </span>
                      )}
                      <span className="shrink-0 ml-auto">
                        {ts ? relativeTime(ts) : "-"}
                      </span>
                    </div>
                    {preview && (
                      <div className="text-xs text-text-secondary mt-1 truncate">
                        {c.last_from_me ? (
                          <span className="text-text-muted">You: </span>
                        ) : null}
                        {preview}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Message pane */}
        <div className="bg-surface rounded-lg border border-border p-4 min-h-[40vh]">
          {!selected ? (
            <p className="text-sm text-text-muted py-8 text-center">
              Select a chat to view messages.
            </p>
          ) : chat.isLoading ? (
            <LoadingSpinner />
          ) : !chat.data?.messages.length ? (
            <p className="text-sm text-text-muted py-8 text-center">
              No messages in this chat yet.
            </p>
          ) : (
            <>
              {chat.data.chat && (
                <div className="mb-3 pb-3 border-b border-border/40">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-medium">
                      {chat.data.chat.name || chat.data.chat.platform_chat_id}
                    </h3>
                    <ChatTypeBadge type={chat.data.chat.chat_type} />
                  </div>
                  <div className="text-[11px] text-text-muted mt-0.5 font-mono truncate">
                    {chat.data.chat.platform_chat_id}
                  </div>
                  {chat.data.chat.description && (
                    <div className="text-xs text-text-secondary mt-1 whitespace-pre-wrap">
                      {chat.data.chat.description}
                    </div>
                  )}
                </div>
              )}
              <div className="space-y-2 max-h-[70vh] overflow-y-auto">
                {chat.data.messages.map((m) => (
                  <div
                    key={m.platform_message_id}
                    className={`flex ${m.from_me ? "justify-end" : "justify-start"}`}
                  >
                    <div
                      className={`max-w-[75%] rounded-lg px-3 py-2 text-sm ${
                        m.from_me
                          ? "bg-info/20 text-text-primary"
                          : "bg-background text-text-primary"
                      } ${m.is_deleted ? "opacity-50" : ""}`}
                    >
                      {!m.from_me && (
                        <div className="text-xs text-text-muted mb-0.5">
                          {senderLabel(m)}
                        </div>
                      )}
                      {m.quoted_text && (
                        <div className="text-[11px] text-text-muted border-l-2 border-info/40 pl-2 mb-1 italic whitespace-pre-wrap break-words">
                          {m.quoted_text}
                        </div>
                      )}
                      {m.forward_from_name && (
                        <div className="text-[11px] text-text-muted mb-0.5">
                          ↪ Forwarded from {m.forward_from_name}
                        </div>
                      )}
                      <div
                        className={`whitespace-pre-wrap break-words ${
                          m.is_deleted ? "line-through text-text-muted italic" : ""
                        }`}
                      >
                        {m.is_deleted
                          ? "(deleted)"
                          : m.text || (m.media_mime_type
                              ? ""
                              : <span className="italic text-text-muted">(no content)</span>)}
                      </div>
                      {m.text_truncated && (
                        <div className="text-[10px] text-text-muted mt-0.5 italic">
                          … truncated ({m.text_full_length?.toLocaleString()} chars total)
                        </div>
                      )}
                      {!m.is_deleted && <MessageMedia m={m} />}
                      <div className="text-[10px] text-text-muted mt-1 text-right">
                        {m.is_deleted && m.deleted_at
                          ? `deleted ${relativeTime(m.deleted_at)}`
                          : m.timestamp
                          ? relativeTime(m.timestamp)
                          : ""}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
