import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Heart, MessageCircle, Play, Share2, BadgeCheck } from "lucide-react";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime, formatTimestamp, formatNumber } from "../../utils/formatters";
import type { ThreadsProfile, ThreadsPost } from "../../services/types";

// Threads feed page — two-pane layout mirroring the Telegram/WhatsApp chat
// pages, but with a grid of post cards on the right instead of a message
// stream (Threads is not a chat platform). Backed by /threads/profiles for
// the picker and /threads/profile/{username} for the selected profile's
// posts + media UUIDs. Thumbnails come from /media/<uuid>/thumbnail, the
// same endpoint the browser/media pages already use.

// Compact 1.2K / 3.4M style — Threads itself renders counts this way and
// full toLocaleString() ("4,600,000") wastes horizontal room on cards
// where four stats sit side by side.
function compactCount(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

// Threads clip durations are seconds-int; 3m30s reads better than 210s.
function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function profileDisplayName(p: ThreadsProfile): string {
  return `@${p.username}`;
}

export function ThreadsFeedPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const profiles = useQuery({
    queryKey: ["threads-profiles"],
    queryFn: () => api.threadsProfiles(100),
  });

  const profile = useQuery({
    queryKey: ["threads-profile", selected],
    queryFn: () => api.threadsProfile(selected!),
    enabled: !!selected,
  });

  if (profiles.error)
    return <ErrorState message={String(profiles.error)} onRetry={() => profiles.refetch()} />;

  return (
    <div>
      <Header
        title="Threads"
        subtitle="Profiles and their collected posts (video thumbnails · stats · post links)"
        onRefresh={() => {
          profiles.refetch();
          if (selected) profile.refetch();
        }}
      />

      <div className="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-4">
        {/* Profile picker */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {profiles.isLoading ? (
            <LoadingSpinner />
          ) : !profiles.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No Threads profiles yet. Onboard cookies and run the threads
              collector; profiles + posts will start populating here.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[80vh] overflow-y-auto">
              {profiles.data.map((p) => {
                const isActive = selected === p.username;
                return (
                  <li
                    key={p.username}
                    onClick={() => p.username && setSelected(p.username)}
                    className={`px-3 py-2.5 cursor-pointer hover:bg-white/5 ${
                      isActive ? "bg-white/10" : ""
                    } ${!p.username ? "opacity-50 cursor-not-allowed" : ""}`}
                  >
                    <div className="flex items-center gap-2">
                      {p.avatar_url ? (
                        // Threads CDN avatars — served over HTTPS with public
                        // caching. Fall back to the initial mono-badge if
                        // the CDN blocks the referer (rare, e.g. p-16-va).
                        <img
                          src={p.avatar_url}
                          alt=""
                          loading="lazy"
                          className="w-8 h-8 rounded-full object-cover shrink-0 bg-background"
                          onError={(e) => {
                            (e.currentTarget as HTMLImageElement).style.display = "none";
                          }}
                        />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-background border border-border/60 shrink-0" />
                      )}
                      <div className="min-w-0 flex-1">
                          <span className="text-sm font-medium truncate">
                            {profileDisplayName(p)}
                          </span>
                        </div>
                    </div>
                    <div className="mt-1 flex items-center justify-between text-[11px] text-text-muted tabular-nums">
                      <span>
                        {p.posts_collected ?? 0} posts
                        {p.last_post_at ? ` · ${relativeTime(p.last_post_at)}` : ""}
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
                Select a profile to view its posts.
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
              <ProfileCard p={profile.data.profile} postCount={profile.data.posts.length} />
              {profile.data.posts.length === 0 ? (
                <div className="bg-surface rounded-lg border border-border p-8">
                  <p className="text-sm text-text-muted text-center">
                    No posts collected for this profile yet.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
                  {profile.data.posts.map((post) => (
                    <PostCard key={post.platform_post_id} post={post} />
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

function ProfileCard({ p, postCount }: { p: ThreadsProfile; postCount: number }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-start gap-3">
        {p.avatar_url && (
          <img
            src={p.avatar_url}
            alt=""
            loading="lazy"
            className="w-16 h-16 rounded-full object-cover shrink-0 bg-background"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h3 className="text-base font-semibold text-text-primary truncate">
              {profileDisplayName(p)}
            </h3>
          </div>
          {p.username && (
            <a
              href={`https://www.threads.net/@${p.username}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-info hover:underline"
            >
              @{p.username} <ExternalLink className="inline w-3 h-3" />
            </a>
          )}
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-text-muted tabular-nums">
            <span>{postCount} collected</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// Photo carousels come back with content_type='photo'; everything else
// (video posts + the generic 'post' fallback) uses the video icon. Keeps
// the visual distinct enough to eyeball a mixed feed.
const PHOTO_CONTENT_TYPES = new Set(["photo", "image"]);

function PostCard({ post }: { post: ThreadsPost }) {
  const isPhoto = post.media_type === "image" || post.media_content_type === "photo" || post.media_content_type === "image";
  const desc = post.caption || "";

  return (
    <div className="bg-surface rounded-lg border border-border overflow-hidden flex flex-col">
      <a
        href={post.post_url}
        target="_blank"
        rel="noopener noreferrer"
        className="block relative aspect-[3/4] bg-background overflow-hidden group"
        title="Open on Threads"
      >
        {post.media_item_id ? (
          <img
            src={api.thumbnailUrl(post.media_item_id)}
            alt=""
            loading="lazy"
            className="w-full h-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-text-muted text-xs">
            no thumbnail
          </div>
        )}
        {/* corner badges: content kind */}
        <div className="absolute top-1.5 left-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wide">
          {isPhoto ? "photo" : "video"}
        </div>
        
        {/* hover overlay → view count */}
        {post.likes_count != null && (
          <div className="absolute bottom-1.5 left-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded flex items-center gap-1">
            <Play className="w-3 h-3" />
            <span className="tabular-nums">{compactCount(post.likes_count)}</span>
          </div>
        )}
        <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 bg-black/30 transition-opacity">
          <ExternalLink className="w-6 h-6 text-white" />
        </div>
      </a>
      <div className="p-2.5 flex flex-col gap-1.5 flex-1">
        {desc && (
          <p
            className="text-xs text-text-primary line-clamp-3 break-words"
            title={desc}
          >
            {desc}
          </p>
        )}
        <div className="flex items-center gap-3 text-[11px] text-text-muted tabular-nums mt-auto pt-1">
          <span className="flex items-center gap-1" title="Likes">
            <Heart className="w-3 h-3" />
            {compactCount(post.likes_count)}
          </span>
          <span className="flex items-center gap-1" title="Comments">
            <MessageCircle className="w-3 h-3" />
            {compactCount(post.comments_count)}
          </span>
          <span className="flex items-center gap-1" title="Shares">
            <Share2 className="w-3 h-3" />
            {compactCount(post.reposts_count)}
          </span>
          <span className="ml-auto text-text-muted">
            {post.platform_created_at ? relativeTime(post.platform_created_at) : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
