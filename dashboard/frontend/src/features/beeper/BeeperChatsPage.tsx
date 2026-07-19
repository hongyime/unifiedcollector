import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { API_BASE } from "../../utils/constants";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime, formatTimestamp, formatNumber } from "../../utils/formatters";
import type { BeeperChat, BeeperMessage } from "../../services/types";

// Telegram lives at :8700 /telegram/chats. Left pane = recent chats (newest
// updated_at first, LIMIT 100 by default), right pane = the 200 newest
// messages for the selected chat with sender identity, edit/deleted flags,
// and inline media thumbnails cross-referenced through media_items. Chat
// metadata card sits below the message list — a single scroll-through view.
//
// Chat/message data comes from telegram_chats + telegram_messages via
// /telegram/chats and /telegram/chat/{platform_chat_id}. The endpoints
// degrade to an empty payload if migrations haven't run, so this page
// renders a clean empty state on a partial boot instead of a red screen.

// Photo/video-ish media types that carry an inline preview. Everything else
// (documents, stickers, audio, forwarded_status, etc.) falls through to a
// text-only bubble with an italic type label.
const INLINE_MEDIA_TYPES = new Set([
  "photo",
  "image",
  "video",
  "animation",
  "gif",
  "sticker",
]);

function chatDisplayName(c: BeeperChat): string {
  return c.title?.trim() || c.chat_id;
}

function senderDisplayName(m: BeeperMessage): string {
  return m.sender_name || m.sender_id || "unknown";
}

type BeeperChatsPageProps = {
  network?: string;
  title?: string;
  subtitle?: string;
};

export function BeeperChatsPage({
  network,
  title = "Beeper",
  subtitle = "Chats and messages captured across networks",
}: BeeperChatsPageProps) {
  const [selected, setSelected] = useState<string | null>(null);

  const chats = useQuery({
    queryKey: ["beeper-chats", network ?? "all"],
    queryFn: () => api.beeperChats(100, network),
  });

  const chat = useQuery({
    queryKey: ["beeper-chat", selected],
    queryFn: () => api.beeperChat(selected!),
    enabled: !!selected,
  });

  if (chats.error)
    return <ErrorState message={String(chats.error)} onRetry={() => chats.refetch()} />;

  return (
    <div>
      <Header
        title={title}
        subtitle={subtitle}
        onRefresh={() => {
          chats.refetch();
          if (selected) chat.refetch();
        }}
      />

      <div className="grid grid-cols-1 md:grid-cols-[320px_1fr] gap-4">
        {/* Chat list */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {chats.isLoading ? (
            <LoadingSpinner />
          ) : !chats.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No Beeper chats yet.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[70vh] overflow-y-auto">
              {chats.data.map((c) => (
                <li
                  key={c.chat_id}
                  onClick={() => setSelected(c.chat_id)}
                  className={`px-4 py-3 cursor-pointer hover:bg-white/5 ${
                    selected === c.chat_id ? "bg-white/10" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      {c.img_url ? (
                        <img
                          src={c.img_url}
                          alt=""
                          loading="lazy"
                          className="w-6 h-6 rounded-full object-cover shrink-0 bg-background"
                          onError={(e) => {
                            (e.currentTarget as HTMLImageElement).style.display = "none";
                          }}
                        />
                      ) : (
                        <div className="w-6 h-6 rounded-full bg-background border border-border/60 shrink-0" />
                      )}
                      <span className="text-sm font-medium truncate">
                        {chatDisplayName(c)}
                      </span>
                    </div>
                  </div>
                  <div className="text-xs text-text-muted mt-0.5 flex items-center gap-1.5 ml-8">
                    {c.network && (
                      <span className="uppercase tracking-wide text-[10px] text-text-muted/80">
                        {c.network}
                      </span>
                    )}
                    <span>·</span>
                    <span>
                      {c.last_seen_at ? relativeTime(c.last_seen_at) : "-"}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Message pane + metadata card */}
        <div className="space-y-4">
          <div className="bg-surface rounded-lg border border-border p-4 min-h-[40vh]">
            {!selected ? (
              <p className="text-sm text-text-muted py-8 text-center">
                Select a chat to view its messages.
              </p>
            ) : chat.isLoading ? (
              <LoadingSpinner />
            ) : !chat.data?.messages.length ? (
              <p className="text-sm text-text-muted py-8 text-center">
                No messages captured for this chat yet.
              </p>
            ) : (
              <div className="space-y-2 max-h-[60vh] overflow-y-auto">
                {chat.data.messages.map((m) => (
                  <MessageBubble key={m.message_id} m={m} />
                ))}
              </div>
            )}
          </div>

          {selected && chat.data?.chat && <ChatMetadataCard c={chat.data.chat} />}
        </div>
      </div>
    </div>
  );
}

function MessageBubble({ m }: { m: BeeperMessage }) {
  const body = m.text || "";
  const hasInlineMedia =
    !!m.media_item_id &&
    !!m.media_type &&
    INLINE_MEDIA_TYPES.has(m.media_type.toLowerCase());
  return (
    <div className="flex gap-2 group">
      <div className="flex-1 min-w-0 bg-white/5 rounded p-2.5">
        <div className="flex items-center justify-between gap-4 mb-1">
          <span className="text-xs font-medium text-text-primary truncate">
            {senderDisplayName(m)}
          </span>
          <div className="flex items-center gap-2 shrink-0">
            {m.is_deleted && (
              <span className="text-[10px] text-rose-400 bg-rose-400/10 px-1 rounded">
                deleted
              </span>
            )}
            <span className="text-[10px] text-text-muted tabular-nums">
              {m.timestamp ? formatTimestamp(m.timestamp) : ""}
            </span>
          </div>
        </div>
        {hasInlineMedia && m.media_item_id && (
          <a
            href={`${API_BASE}/media/${m.media_item_id}/file`}
            target="_blank"
            rel="noopener noreferrer"
            className="block mb-2"
          >
            <AuthImage
              src={`${API_BASE}/media/${m.media_item_id}/thumbnail`}
              alt={m.media_type ?? "media"}
              className="max-h-40 rounded border border-border/60 object-cover"
              fallbackLabel={m.media_type ?? "media"}
            />
          </a>
        )}
        {body && (
          <div className="whitespace-pre-wrap break-words text-sm text-text-primary">
            {body}
          </div>
        )}
      </div>
    </div>
  );
}

function ChatMetadataCard({ c }: { c: BeeperChat }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4 text-sm">
      <div className="flex items-center gap-3 mb-4">
        {c.img_url ? (
          <img
            src={c.img_url}
            alt=""
            loading="lazy"
            className="w-12 h-12 rounded-full object-cover shrink-0 bg-background"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <div className="w-12 h-12 rounded-full bg-background border border-border/60 shrink-0" />
        )}
        <div className="min-w-0">
          <h3 className="font-semibold text-text-primary truncate">
            {chatDisplayName(c)}
          </h3>
          <div className="text-xs text-text-muted flex items-center gap-1.5 mt-0.5">
            <span>{c.network}</span>
          </div>
        </div>
      </div>

      <dl className="space-y-2 text-xs">
        <div className="flex justify-between gap-4">
          <dt className="text-text-muted">Chat ID</dt>
          <dd className="text-text-primary font-mono truncate">{c.chat_id}</dd>
        </div>
        {c.account_id && (
          <div className="flex justify-between gap-4">
            <dt className="text-text-muted">Account</dt>
            <dd className="text-text-primary truncate">{c.account_id}</dd>
          </div>
        )}
        <div className="flex justify-between gap-4">
          <dt className="text-text-muted">Direct</dt>
          <dd className="text-text-primary truncate">{c.is_direct ? "Yes" : "No"}</dd>
        </div>
        
        <div className="flex justify-between gap-4 pt-2 mt-2 border-t border-border/50">
          <dt className="text-text-muted">Messages Collected</dt>
          <dd className="text-text-primary tabular-nums">
            {c.messages_collected ? formatNumber(c.messages_collected) : 0}
          </dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-text-muted">Last Message</dt>
          <dd className="text-text-primary tabular-nums">
            {c.last_message_at ? relativeTime(c.last_message_at) : "Never"}
          </dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-text-muted">Last Seen</dt>
          <dd className="text-text-primary tabular-nums">
            {c.last_seen_at ? relativeTime(c.last_seen_at) : "Never"}
          </dd>
        </div>
      </dl>
    </div>
  );
}
