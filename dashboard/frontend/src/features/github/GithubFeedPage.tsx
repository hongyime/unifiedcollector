import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime, formatTimestamp, formatNumber } from "../../utils/formatters";
import type { GithubProfile, GithubCommit, GithubRepo, GithubEdgeStats } from "../../services/types";

// GitHub feed page — two-pane layout mirroring the Telegram/WhatsApp chat
// pages, but with a grid of post cards on the right instead of a message
// stream (GitHub is not a chat platform). Backed by /github/profiles for
// the picker and /github/profile/{username} for the selected profile's
// posts + media UUIDs. Thumbnails come from /media/<uuid>/thumbnail, the
// same endpoint the browser/media pages already use.

// Compact 1.2K / 3.4M style — GitHub itself renders counts this way and
// full toLocaleString() ("4,600,000") wastes horizontal room on cards
// where four stats sit side by side.
function compactCount(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

// GitHub clip durations are seconds-int; 3m30s reads better than 210s.
function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function profileDisplayName(p: GithubProfile): string {
  return p.owner || "unknown";
}

export function GithubFeedPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const profiles = useQuery({
    queryKey: ["github-profiles"],
    queryFn: () => api.githubProfiles(100),
  });

  const edgeStats = useQuery({
    queryKey: ["github-edge-stats"],
    queryFn: () => api.githubEdgeStats(),
  });

  const profile = useQuery({
    queryKey: ["github-profile", selected],
    queryFn: () => api.githubProfile(selected!),
    enabled: !!selected,
  });

  if (profiles.error)
    return <ErrorState message={String(profiles.error)} onRetry={() => profiles.refetch()} />;

  return (
    <div>
      <Header
        title="GitHub"
        subtitle="Profiles, repositories, and collected commits"
        onRefresh={() => {
          profiles.refetch();
          edgeStats.refetch();
          if (selected) profile.refetch();
        }}
      />

      {edgeStats.data && <EdgeStatsBar stats={edgeStats.data} />}

      <div className="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-4">
        {/* Profile picker */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {profiles.isLoading ? (
            <LoadingSpinner />
          ) : !profiles.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No GitHub profiles yet. Onboard cookies and run the github
              collector; profiles + posts will start populating here.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[80vh] overflow-y-auto">
              {profiles.data.map((p) => {
                const isActive = selected === p.owner;
                return (
                  <li
                    key={p.owner}
                    onClick={() => setSelected(p.owner)}
                    className={`px-3 py-2.5 cursor-pointer hover:bg-white/5 ${
                      isActive ? "bg-white/10" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <div className="w-8 h-8 rounded-full bg-background border border-border/60 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="text-sm font-medium truncate">
                            {profileDisplayName(p)}
                          </span>
                        </div>
                        <div className="text-[11px] text-text-muted truncate">
                          {formatNumber(p.repos_collected)} repos
                          {p.commits_loaded ? ` · ${formatNumber(p.commits_loaded)} recent commits` : ""}
                        </div>
                      </div>
                    </div>
                    <div className="mt-1 flex items-center justify-between text-[11px] text-text-muted tabular-nums">
                      <span>{compactCount(p.stargazers_count)} stars</span>
                      <span>
                        {compactCount(p.forks_count)} forks
                        {p.updated_at ? ` · ${relativeTime(p.updated_at)}` : ""}
                      </span>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Post grid + profile card */}
        <div className="space-y-4">
          {!selected ? (
            <div className="bg-surface rounded-lg border border-border p-8">
              <p className="text-sm text-text-muted text-center">
                Select a profile to view repositories and commits.
              </p>
            </div>
          ) : profile.isLoading ? (
            <LoadingSpinner />
          ) : !profile.data?.profile ? (
            <div className="bg-surface rounded-lg border border-border p-8">
              <p className="text-sm text-text-muted text-center">
                Profile not found.
              </p>
            </div>
          ) : (
            <>
              <ProfileCard p={profile.data.profile} repos={profile.data.repos} commitCount={profile.data.commits.length} />
              {profile.data.commits.length === 0 ? (
                <div className="bg-surface rounded-lg border border-border p-8">
                  <p className="text-sm text-text-muted text-center">
                    No commits collected for this profile yet.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-3">
                  {profile.data.commits.map((post) => (
                    <PostCard key={post.sha} post={post} />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function EdgeStatsBar({ stats }: { stats: GithubEdgeStats }) {
  const topTypes = stats.by_type.slice(0, 4);
  return (
    <div className="mb-4 grid grid-cols-2 lg:grid-cols-5 gap-3">
      <div className="bg-surface rounded-lg border border-border p-3">
        <div className="text-[11px] uppercase text-text-muted">Edges</div>
        <div className="text-lg font-semibold text-text-primary tabular-nums">
          {formatNumber(stats.total_edges)}
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-3">
        <div className="text-[11px] uppercase text-text-muted">This Hour</div>
        <div className="text-lg font-semibold text-text-primary tabular-nums">
          {formatNumber(stats.edges_current_hour)}
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-3">
        <div className="text-[11px] uppercase text-text-muted">Profiles</div>
        <div className="text-lg font-semibold text-text-primary tabular-nums">
          {formatNumber(stats.distinct_targets)}
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-3">
        <div className="text-[11px] uppercase text-text-muted">Queued</div>
        <div className="text-lg font-semibold text-text-primary tabular-nums">
          {formatNumber(stats.queued_profiles)}
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-3 col-span-2 lg:col-span-1">
        <div className="text-[11px] uppercase text-text-muted">Top Evidence</div>
        <div className="mt-1 space-y-0.5">
          {topTypes.length ? topTypes.map((t) => (
            <div key={t.edge_type} className="flex items-center justify-between gap-2 text-[11px]">
              <span className="truncate text-text-secondary">{t.edge_type.replace(/_/g, " ")}</span>
              <span className="text-text-primary tabular-nums">{compactCount(t.count)}</span>
            </div>
          )) : (
            <div className="text-[11px] text-text-muted">No edge evidence yet</div>
          )}
        </div>
      </div>
    </div>
  );
}

function ProfileCard({ p, repos, commitCount }: { p: GithubProfile; repos: GithubRepo[]; commitCount: number }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-start gap-3">
        <div className="w-16 h-16 rounded-full bg-background border border-border/60 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h3 className="text-base font-semibold text-text-primary truncate">
              {profileDisplayName(p)}
            </h3>
          </div>
          {p.owner && (
            <a
              href={`https://github.com/${p.owner}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-info hover:underline"
            >
              {p.owner} <ExternalLink className="inline w-3 h-3" />
            </a>
          )}
          {repos.length > 0 && (
            <p className="mt-1.5 text-xs text-text-secondary truncate">
              Top repos: {repos.slice(0, 4).map((r) => r.name || r.full_name).join(", ")}
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-text-muted tabular-nums">
            <span><b className="text-text-primary">{compactCount(p.stargazers_count)}</b> stars</span>
            <span><b className="text-text-primary">{compactCount(p.forks_count)}</b> forks</span>
            <span><b className="text-text-primary">{formatNumber(p.repos_collected)}</b> repos</span>
            <span className="text-text-muted">·</span>
            <span>{commitCount} recent commits loaded</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function PostCard({ post }: { post: GithubCommit }) {
  const desc = post.message || "";

  return (
    <div className="bg-surface rounded-lg border border-border overflow-hidden flex flex-col p-3 gap-2">
      <a
        href={post.commit_url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-sm font-semibold text-info hover:underline truncate block"
        title="Open on GitHub"
      >
        {post.sha.substring(0, 7)} <ExternalLink className="inline w-3 h-3" />
      </a>
      {desc && (
        <p className="text-xs text-text-primary line-clamp-3 break-words">
          {desc}
        </p>
      )}
      <div className="flex flex-col gap-1 text-[11px] text-text-muted tabular-nums mt-auto pt-1">
        <div className="flex items-center gap-2">
            <span>{post.author_name || post.author_login || "unknown"}</span>
            {post.repo_full_name && <span className="truncate">in {post.repo_full_name}</span>}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-emerald-500">+{post.insertions || 0}</span>
          <span className="text-rose-500">-{post.deletions || 0}</span>
          <span className="ml-auto text-text-muted">
            {post.date ? relativeTime(post.date) : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
