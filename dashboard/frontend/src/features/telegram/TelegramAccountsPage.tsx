import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { Button } from "../../components/ui/Button";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { formatTimestamp } from "../../utils/formatters";

const statusColor: Record<string, string> = {
  active: "bg-success/20 text-success",
  disabled: "bg-warning/20 text-warning",
  expired: "bg-danger/20 text-danger",
  banned: "bg-danger/20 text-danger",
};

export function TelegramAccountsPage() {
  const qc = useQueryClient();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["telegram-accounts"],
    queryFn: () => api.telegramAccounts(),
  });

  const enable = useMutation({
    mutationFn: (name: string) => api.telegramAccountEnable(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["telegram-accounts"] }),
  });
  const disable = useMutation({
    mutationFn: (name: string) => api.telegramAccountDisable(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["telegram-accounts"] }),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Telegram Accounts" subtitle="Onboarded MTProto accounts" onRefresh={() => refetch()} />
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? (
          <LoadingSpinner />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-text-muted border-b border-border">
                <th className="pb-2">Name</th>
                <th className="pb-2">Phone</th>
                <th className="pb-2">Status</th>
                <th className="pb-2">Source</th>
                <th className="pb-2">Last connected</th>
                <th className="pb-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data?.map((a) => (
                <tr key={a.name} className="border-b border-border/50 hover:bg-white/5">
                  <td className="py-2 font-medium">{a.name}</td>
                  <td className="py-2 text-text-muted font-mono">{a.phone ?? "-"}</td>
                  <td className="py-2">
                    <span className={`text-xs px-1.5 py-0.5 rounded ${statusColor[a.status] ?? "bg-text-muted/20 text-text-muted"}`}>
                      {a.status}
                    </span>
                  </td>
                  <td className="py-2 text-text-muted">{a.owner_bot ?? "-"}</td>
                  <td className="py-2 text-text-muted">
                    {a.last_connected_at ? formatTimestamp(a.last_connected_at) : "-"}
                  </td>
                  <td className="py-2 text-right">
                    {a.status === "active" ? (
                      <Button size="sm" variant="ghost" onClick={() => disable.mutate(a.name)} disabled={disable.isPending}>
                        Disable
                      </Button>
                    ) : (
                      <Button size="sm" variant="primary" onClick={() => enable.mutate(a.name)} disabled={enable.isPending}>
                        Enable
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
              {(!data || data.length === 0) && (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-text-muted">
                    No onboarded accounts. The 4 collector accounts run from local session files.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
