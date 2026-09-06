import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";

function StatCard({
  label,
  value,
  approximate = false,
}: {
  label: string;
  value: number | undefined;
  approximate?: boolean;
}) {
  const formatted = value === undefined ? "-" : `${approximate ? "~" : ""}${value.toLocaleString()}`;
  return (
    <div className="bg-surface border border-border rounded-lg p-4">
      <p className="text-xs text-text-muted uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-semibold font-mono mt-1">
        {formatted}
      </p>
    </div>
  );
}

export function TelegramStatsPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["telegram-stats"],
    queryFn: () => api.telegramStats(),
    refetchInterval: 30_000,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;
  if (isLoading || !data) return <LoadingSpinner />;

  const t = data.totals;
  const estimated = data.estimated ?? {};
  return (
    <div>
      <Header title="Telegram" subtitle="Collection stats" onRefresh={() => refetch()} />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <StatCard label="Messages" value={t.messages} approximate={estimated.messages} />
        <StatCard label="Users" value={t.users} approximate={estimated.users} />
        <StatCard label="Chats" value={t.chats} approximate={estimated.chats} />
        <StatCard label="Reactions" value={t.reactions} approximate={estimated.reactions} />
        <StatCard label="Accounts" value={t.accounts} />
        <StatCard label="Spider queue" value={t.spider_queue} />
        <StatCard label="Messages (24h)" value={data.recent.messages_24h} />
        <StatCard label="Messages (1h)" value={data.recent.messages_1h} />
        <StatCard label="Media (24h)" value={data.recent.media_24h} />
        <StatCard label="Media (1h)" value={data.recent.media_1h} />
      </div>

      <h3 className="text-sm font-semibold mb-3">Top chats by messages ({data.top_chats_window ?? "24h"})</h3>
      <div className="bg-surface rounded-lg border border-border p-4">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">Chat</th>
              <th className="pb-2">Username</th>
              <th className="pb-2 text-right">Messages</th>
            </tr>
          </thead>
          <tbody>
            {data.top_chats.map((c, i) => (
              <tr key={i} className="border-b border-border/50 hover:bg-white/5">
                <td className="py-2">{c.title || <span className="text-text-muted">(untitled)</span>}</td>
                <td className="py-2 text-text-muted">{c.username ? `@${c.username}` : "-"}</td>
                <td className="py-2 text-right font-mono">{c.messages.toLocaleString()}</td>
              </tr>
            ))}
            {data.top_chats.length === 0 && (
              <tr>
                <td colSpan={3} className="py-4 text-center text-text-muted">
                  No chat data yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
