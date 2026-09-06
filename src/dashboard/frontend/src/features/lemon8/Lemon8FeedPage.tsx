import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Heart, MessageCircle, Play, Share2, BadgeCheck } from "lucide-react";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime, formatTimestamp, formatNumber } from "../../utils/formatters";
import type { Lemon8Profile, Lemon8Post } from "../../services/types";

// Lemon8 feed page — two-pane layout mirroring the Telegram/WhatsApp chat
// pages, but with a grid of post cards on the right instead of a message
// stream (Lemon8 is not a chat platform). Backed by /lemon8/profiles for
// the picker and /lemon8/profile/{username} for the selected profile's
// posts + media UUIDs. Thumbnails come from /media/<uuid>/thumbnail, the
// same endpoint the browser/media pages already use.

// Compact 1.2K / 3.4M style — Lemon8 itself renders counts this way and
// full toLocaleString() ("4,600,000") wastes horizontal room on cards
// where four stats sit side by side.
function compactCount(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

// Lemon8 clip durations are seconds-int; 3m30s reads better than 210s.
function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function profileDisplayName(p: Pick<Lemon8Profile, "nickname" | "username">): string {
  return p.nickname?.trim() || (p.username ? `@${p.username}` : "unknown");
}

export function Lemon8FeedPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const profiles = useQuery({
    queryKey: ["lemon8-profiles"],
    queryFn: () => api.lemon8Profiles(100),
  });

  const profile = useQuery({
    queryKey: ["lemon8-profile", selected],
    queryFn: () => api.lemon8Profile(selected!),
    enabled: !!selected,
  });

  if (profiles.error)
    return <ErrorState message={String(profiles.error)} onRetry={() => profiles.refetch()} />;

  return (
    <div>
      <Header
        title="Lemon8"
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
              No Lemon8 profiles yet. Onboard cookies and run the lemon8
              collector; profiles + posts will start populating here.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[80vh] overflow-y-auto">
              {profiles.data.map((p) => {
                const isActive = selected === p.username;
                return (
                  <li
                    key={p.platform_user_id}
                    onClick={() => p.username && setSelected(p.username)}
                    className={`px-3 py-2.5 cursor-pointer hover:bg-white/5 ${
                      isActive ? "bg-white/10" : ""
                    } ${!p.username ? "opacity-50 cursor-not-allowed" : ""}`}
                  >
                    <div className="flex items-center gap-2">
                      {p.avatar_url ? (
                        // Lemon8 CDN avatars — served over HTTPS with public
                        // caching. Fall back to the initial mono-badge if
                        // the CDN blocks the referer (rare, e.g. p-16-va).
                        <AuthImage
                          src={p.avatar_url}
                          alt=""
                          className="w-8 h-8 rounded-full object-cover shrink-0 bg-background"
                          fallbackLabel="l8"
                        />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-background border border-border/60 shrink-0" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="text-sm font-medium truncate">
                            {profileDisplayName(p)}
                          </span>
                        </div>
                        {p.username && p.nickname && (
                          <div className="text-[11px] text-text-muted truncate">
                            @{p.username}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="mt-1 flex items-center justify-between text-[11px] text-text-muted tabular-nums">
                      <span>{compactCount(p.followers_count)} followers</span>
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

function ProfileCard({ p, postCount }: { p: Lemon8Profile; postCount: number }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-start gap-3">
        {p.avatar_url && (
          <AuthImage
            src={p.avatar_url}
            alt=""
            className="w-16 h-16 rounded-full object-cover shrink-0 bg-background"
            fallbackLabel="l8"
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
              href={`https://www.lemon8-app.com/${p.username}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-info hover:underline"
            >
              @{p.username} <ExternalLink className="inline w-3 h-3" />
            </a>
          )}
          {p.bio && (
            <p className="mt-1.5 text-xs text-text-secondary whitespace-pre-wrap break-words">
              {p.bio}
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-text-muted tabular-nums">
            <span><b className="text-text-primary">{compactCount(p.followers_count)}</b> followers</span>
            <span><b className="text-text-primary">{compactCount(p.following_count)}</b> following</span>
            <span><b className="text-text-primary">{compactCount(p.like_count)}</b> likes</span>
            <span className="text-text-muted">·</span>
            <span>{postCount} collected</span>
          </div>
          {p.updated_at && (
            <div className="mt-1 text-[10px] text-text-muted">
              Profile refreshed {formatTimestamp(p.updated_at)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Photo carousels come back with content_type='photo'; everything else
// (video posts + the generic 'post' fallback) uses the video icon. Keeps
// the visual distinct enough to eyeball a mixed feed.
const PHOTO_CONTENT_TYPES = new Set(["photo", "image"]);

function PostCard({ post }: { post: Lemon8Post }) {
  const isPhoto = post.media_content_type
    ? PHOTO_CONTENT_TYPES.has(post.media_content_type)
    : false;
  const desc = post.description || post.title || "";

  return (
    <div className="bg-surface rounded-lg border border-border overflow-hidden flex flex-col">
      <a
        href={post.post_url}
        target="_blank"
        rel="noopener noreferrer"
        className="block relative aspect-[3/4] bg-background overflow-hidden group"
        title="Open on Lemon8"
      >
        {post.media_item_id ? (
          <AuthImage
            src={api.thumbnailUrl(post.media_item_id)}
            alt=""
            className="w-full h-full object-cover transition-transform group-hover:scale-105"
            fallbackLabel={post.media_content_type || "media"}
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
        {post.like_count != null && (
          <div className="absolute bottom-1.5 left-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded flex items-center gap-1">
            <Heart className="w-3 h-3" />
            <span className="tabular-nums">{compactCount(post.like_count)}</span>
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
            {compactCount(post.like_count)}
          </span>
          <span className="flex items-center gap-1" title="Comments">
            <MessageCircle className="w-3 h-3" />
            {compactCount(post.comment_count)}
          </span>
          <span className="flex items-center gap-1" title="Shares">
            <Share2 className="w-3 h-3" />
            {compactCount(post.share_count)}
          </span>
          <span className="ml-auto text-text-muted">
            {post.platform_created_at ? relativeTime(post.platform_created_at) : ""}
          </span>
        </div>
        {post.music_title && (
          <div className="text-[10px] text-text-muted truncate italic" title={post.music_title}>
            ♪ {post.music_title}
          </div>
        )}
      </div>
    </div>
  );
}
