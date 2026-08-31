import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Heart, MessageCircle, Play, Share2, BadgeCheck } from "lucide-react";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime, formatTimestamp, formatNumber } from "../../utils/formatters";
import type { TtProfile, TtPost } from "../../services/types";

// TikTok feed page — two-pane layout mirroring the Telegram/WhatsApp chat
// pages, but with a grid of post cards on the right instead of a message
// stream (TikTok is not a chat platform). Backed by /tiktok/profiles for
// the picker and /tiktok/profile/{username} for the selected profile's
// posts + media UUIDs. Thumbnails come from /media/<uuid>/thumbnail, the
// same endpoint the browser/media pages already use.

// Compact 1.2K / 3.4M style — TikTok itself renders counts this way and
// full toLocaleString() ("4,600,000") wastes horizontal room on cards
// where four stats sit side by side.
function compactCount(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

// TikTok clip durations are seconds-int; 3m30s reads better than 210s.
function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function profileDisplayName(p: Pick<TtProfile, "nickname" | "username">): string {
  return p.nickname?.trim() || (p.username ? `@${p.username}` : "unknown");
}

export function TiktokFeedPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const profiles = useQuery({
    queryKey: ["tt-profiles"],
    queryFn: () => api.tiktokProfiles(100),
  });

  const profile = useQuery({
    queryKey: ["tt-profile", selected],
    queryFn: () => api.tiktokProfile(selected!),
    enabled: !!selected,
  });

  if (profiles.error)
    return <ErrorState message={String(profiles.error)} onRetry={() => profiles.refetch()} />;

  return (
    <div>
      <Header
        title="TikTok"
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
              No TikTok profiles yet. Onboard cookies and run the tiktok
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
                        // TikTok CDN avatars — served over HTTPS with public
                        // caching. Fall back to the initial mono-badge if
                        // the CDN blocks the referer (rare, e.g. p-16-va).
                        <AuthImage
                          src={p.avatar_url}
                          alt=""
                          className="w-8 h-8 rounded-full object-cover shrink-0 bg-background"
                          fallbackLabel="tt"
                        />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-background border border-border/60 shrink-0" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="text-sm font-medium truncate">
                            {profileDisplayName(p)}
                          </span>
                          {p.is_verified && (
                            <BadgeCheck className="w-3.5 h-3.5 text-info shrink-0" />
                          )}
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

function ProfileCard({ p, postCount }: { p: TtProfile; postCount: number }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-start gap-3">
        {p.avatar_url && (
          <AuthImage
            src={p.avatar_url}
            alt=""
            className="w-16 h-16 rounded-full object-cover shrink-0 bg-background"
            fallbackLabel="tt"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h3 className="text-base font-semibold text-text-primary truncate">
              {profileDisplayName(p)}
            </h3>
            {p.is_verified && <BadgeCheck className="w-4 h-4 text-info shrink-0" />}
            {p.is_private && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-warning/20 text-warning uppercase tracking-wide">
                private
              </span>
            )}
          </div>
          {p.username && (
            <a
              href={`https://www.tiktok.com/@${p.username}`}
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
            <span><b className="text-text-primary">{compactCount(p.heart_count)}</b> hearts</span>
            <span><b className="text-text-primary">{formatNumber(p.video_count ?? 0)}</b> videos</span>
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

function PostCard({ post }: { post: TtPost }) {
  const isPhoto = post.media_content_type
    ? PHOTO_CONTENT_TYPES.has(post.media_content_type)
    : false;
  const desc = post.description || post.title || "";

  return (
    <div className="bg-surface rounded-lg border border-border overflow-hidden flex flex-col">
      <div className="relative aspect-[3/4] bg-background overflow-hidden group">
        {post.media_item_id && !isPhoto ? (
          <video
            src={api.fileUrl(post.media_item_id)}
            poster={api.thumbnailUrl(post.media_item_id)}
            controls
            preload="none"
            playsInline
            className="w-full h-full object-contain bg-black"
          />
        ) : post.media_item_id ? (
          <a href={post.post_url} target="_blank" rel="noopener noreferrer" className="block w-full h-full" title="Open on TikTok">
            <AuthImage
              src={api.thumbnailUrl(post.media_item_id)}
              alt=""
              className="w-full h-full object-cover transition-transform group-hover:scale-105"
              fallbackLabel={post.media_content_type || "media"}
            />
          </a>
        ) : post.cover_image_url ? (
          <a href={post.post_url} target="_blank" rel="noopener noreferrer" className="block w-full h-full" title="Open on TikTok">
            <AuthImage
              src={post.cover_image_url}
              alt=""
              className="w-full h-full object-cover transition-transform group-hover:scale-105"
              fallbackLabel={post.media_content_type || "media"}
            />
          </a>
        ) : (
          <div className="w-full h-full flex items-center justify-center text-text-muted text-xs">
            no thumbnail
          </div>
        )}
        <div className="absolute top-1.5 left-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wide pointer-events-none">
          {isPhoto ? "photo" : "video"}
        </div>
        {!isPhoto && post.duration ? (
          <div className="absolute bottom-1.5 right-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded tabular-nums pointer-events-none">
            {formatDuration(post.duration)}
          </div>
        ) : null}
        {post.view_count != null && (
          <div className="absolute bottom-1.5 left-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded flex items-center gap-1 pointer-events-none">
            <Play className="w-3 h-3" />
            <span className="tabular-nums">{compactCount(post.view_count)}</span>
          </div>
        )}
        {post.media_item_id && !isPhoto && (
          <a href={post.post_url} target="_blank" rel="noopener noreferrer" className="absolute top-1.5 right-1.5 bg-black/60 text-white rounded p-1" title="Open on TikTok">
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        )}
      </div>
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
            {post.create_time ? relativeTime(post.create_time) : ""}
          </span>
        </div>
        {post.music_title && (
          <div className="text-[10px] text-text-muted truncate italic" title={`${post.music_title}${post.music_author ? " · " + post.music_author : ""}`}>
            ♪ {post.music_title}
            {post.music_author ? ` · ${post.music_author}` : ""}
          </div>
        )}
      </div>
    </div>
  );
}
