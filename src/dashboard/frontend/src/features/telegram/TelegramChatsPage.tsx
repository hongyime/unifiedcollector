import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { API_BASE } from "../../utils/constants";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime, formatTimestamp, formatNumber } from "../../utils/formatters";
import type { TelegramChat, TelegramMessage } from "../../services/types";

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

function chatDisplayName(c: Pick<TelegramChat, "title" | "username" | "platform_chat_id">): string {
  return c.title?.trim() || (c.username ? `@${c.username}` : c.platform_chat_id);
}

function senderDisplayName(m: TelegramMessage): string {
  if (m.sender_username) return `@${m.sender_username}`;
  const parts = [m.sender_first_name, m.sender_last_name].filter(Boolean);
  if (parts.length) return parts.join(" ");
  if (m.sender_platform_id) return `id:${m.sender_platform_id}`;
  return "unknown";
}

export function TelegramChatsPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const chats = useQuery({
    queryKey: ["telegram-chats"],
    queryFn: () => api.telegramChats(),
  });

  const chat = useQuery({
    queryKey: ["telegram-chat", selected],
    queryFn: () => api.telegramChat(selected!),
    enabled: !!selected,
  });

  if (chats.error)
    return <ErrorState message={String(chats.error)} onRetry={() => chats.refetch()} />;

  return (
    <div>
      <Header
        title="Telegram"
        subtitle="Chats and messages captured via MTProto (live + historical backfill)"
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
              No Telegram chats yet. Onboard an account under Accounts →
              Telegram and the collector will populate this list.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[70vh] overflow-y-auto">
              {chats.data.map((c) => (
                <li
                  key={c.platform_chat_id}
                  onClick={() => setSelected(c.platform_chat_id)}
                  className={`px-4 py-3 cursor-pointer hover:bg-white/5 ${
                    selected === c.platform_chat_id ? "bg-white/10" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium truncate">
                      {chatDisplayName(c)}
                    </span>
                    {/* member count is only meaningful for groups/channels;
                        show it as a soft badge alongside the type */}
                    {typeof c.members_count === "number" && c.members_count > 0 && (
                      <span className="text-xs text-text-muted shrink-0 tabular-nums">
                        {formatNumber(c.members_count)}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-text-muted mt-0.5 flex items-center gap-1.5">
                    {c.type && (
                      <span className="uppercase tracking-wide text-[10px] text-text-muted/80">
                        {c.type}
                      </span>
                    )}
                    <span>·</span>
                    <span>
                      {c.updated_at ? relativeTime(c.updated_at) : "-"}
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
                {/* Messages come back newest-first from the API; reverse into
                    chronological order for a natural top-to-bottom read. */}
                {[...chat.data.messages].reverse().map((m) => (
                  <MessageBubble key={m.platform_message_id} m={m} />
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

function MessageBubble({ m }: { m: TelegramMessage }) {
  const body = m.text || m.caption || "";
  const hasInlineMedia =
    !!m.media_item_id &&
    !!m.media_type &&
    INLINE_MEDIA_TYPES.has(m.media_type.toLowerCase());
  return (
    <div
      className={`bg-background rounded-lg px-3 py-2 text-sm border ${
        m.is_deleted ? "border-danger/40 opacity-70" : "border-border/60"
      }`}
    >
      <div className="flex items-baseline justify-between gap-2 mb-1">
        <span className="text-xs text-text-muted truncate">
          {senderDisplayName(m)}
        </span>
        <span className="text-[10px] text-text-muted shrink-0">
          {m.platform_created_at ? relativeTime(m.platform_created_at) : "-"}
          {m.is_edited && (
            <span className="ml-1 text-warning" title={m.edit_date ?? undefined}>
              (edited)
            </span>
          )}
          {m.is_deleted && (
            <span className="ml-1 text-danger" title={m.deleted_at ?? undefined}>
              (deleted)
            </span>
          )}
        </span>
      </div>
      {hasInlineMedia && m.media_item_id && (
        // Thumbnails already served by /media/<uuid>/thumbnail; clicking pops
        // the full-size file endpoint in a new tab. Keeps this page cheap.
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
      {body ? (
        <div className="whitespace-pre-wrap break-words text-text-primary">
          {body}
        </div>
      ) : m.media_type ? (
        <div className="italic text-text-muted text-xs">
          ({m.media_type}
          {m.media_file_id ? ` · ${m.media_file_id.slice(0, 12)}` : ""})
        </div>
      ) : (
        <div className="italic text-text-muted text-xs">(empty)</div>
      )}
    </div>
  );
}

function ChatMetadataCard({ c }: { c: TelegramChat }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="text-sm font-semibold text-text-primary">
          {chatDisplayName(c)}
        </h3>
        {c.type && (
          <span className="text-[11px] text-text-muted uppercase tracking-wide">
            {c.type}
          </span>
        )}
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <dt className="text-text-muted">Platform ID</dt>
        <dd className="font-mono text-text-primary break-all">
          {c.platform_chat_id}
        </dd>
        {c.username && (
          <>
            <dt className="text-text-muted">Username</dt>
            <dd className="text-text-primary">@{c.username}</dd>
          </>
        )}
        {typeof c.members_count === "number" && c.members_count > 0 && (
          <>
            <dt className="text-text-muted">Members</dt>
            <dd className="text-text-primary tabular-nums">
              {formatNumber(c.members_count)}
            </dd>
          </>
        )}
        {typeof c.message_count === "number" && (
          <>
            <dt className="text-text-muted">Messages collected</dt>
            <dd className="text-text-primary tabular-nums">
              {formatNumber(c.message_count)}
            </dd>
          </>
        )}
        {c.updated_at && (
          <>
            <dt className="text-text-muted">Last updated</dt>
            <dd className="text-text-primary">{formatTimestamp(c.updated_at)}</dd>
          </>
        )}
        {c.collected_at && (
          <>
            <dt className="text-text-muted">First collected</dt>
            <dd className="text-text-primary">
              {formatTimestamp(c.collected_at)}
            </dd>
          </>
        )}
      </dl>
      {c.description && (
        <div className="mt-3 pt-3 border-t border-border/50">
          <p className="text-xs text-text-muted mb-1">Description</p>
          <p className="text-xs text-text-primary whitespace-pre-wrap break-words">
            {c.description}
          </p>
        </div>
      )}
    </div>
  );
}
