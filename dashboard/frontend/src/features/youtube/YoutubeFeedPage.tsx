import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Heart, MessageCircle, Play } from "lucide-react";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime, formatNumber } from "../../utils/formatters";
import type { YoutubeChannel, YoutubeCompleteness, YoutubeVideo } from "../../services/types";

// YouTube feed page — two-pane layout mirroring the Telegram/WhatsApp chat
// pages, but with a grid of post cards on the right instead of a message
// stream (YouTube is not a chat platform). Backed by /youtube/profiles for
// the picker and /youtube/profile/{username} for the selected profile's
// posts + media UUIDs. Thumbnails come from /media/<uuid>/thumbnail, the
// same endpoint the browser/media pages already use.

// Compact 1.2K / 3.4M style — YouTube itself renders counts this way and
// full toLocaleString() ("4,600,000") wastes horizontal room on cards
// where four stats sit side by side.
function compactCount(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

function numberFromRecord(
  record: Record<string, number | string | boolean | null> | undefined,
  key: string,
): number {
  const value = record?.[key];
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function formatDuration(value: string | number | null | undefined): string {
  if (value == null || value === "") return "";
  let seconds: number;
  if (typeof value === "number") {
    seconds = value;
  } else {
    const upper = value.toUpperCase();
    if (upper === "P0D" || upper === "PT0S") return "live";
    const match = upper.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
    if (!match) return value;
    seconds =
      Number(match[1] || 0) * 3600 +
      Number(match[2] || 0) * 60 +
      Number(match[3] || 0);
  }
  if (!seconds || seconds < 0) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  if (m === 0) return `${s}s`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function profileDisplayName(p: YoutubeChannel): string {
  return p.title || p.custom_url || p.platform_channel_id;
}

export function YoutubeFeedPage() {
  const [selected, setSelected] = useState<string | null>(null);

  const profiles = useQuery({
    queryKey: ["youtube-channels"],
    queryFn: () => api.youtubeChannels(100),
  });

  const completeness = useQuery({
    queryKey: ["youtube-completeness"],
    queryFn: () => api.youtubeCompleteness(),
    refetchInterval: 60_000,
  });

  const profile = useQuery({
    queryKey: ["youtube-channel", selected],
    queryFn: () => api.youtubeChannel(selected!),
    enabled: !!selected,
  });

  if (profiles.error)
    return <ErrorState message={String(profiles.error)} onRetry={() => profiles.refetch()} />;

  return (
    <div>
      <Header
        title="YouTube"
        subtitle="Channels and their collected videos"
        onRefresh={() => {
          profiles.refetch();
          completeness.refetch();
          if (selected) profile.refetch();
        }}
      />

      <CompletenessStrip data={completeness.data} />

      <div className="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-4">
        {/* Profile picker */}
        <div className="bg-surface rounded-lg border border-border overflow-hidden">
          {profiles.isLoading ? (
            <LoadingSpinner />
          ) : !profiles.data?.length ? (
            <p className="text-sm text-text-muted py-8 px-4 text-center">
              No YouTube profiles yet. Onboard cookies and run the youtube
              collector; profiles + posts will start populating here.
            </p>
          ) : (
            <ul className="divide-y divide-border/50 max-h-[80vh] overflow-y-auto">
              {profiles.data.map((p) => {
                const isActive = selected === p.platform_channel_id;
                return (
                  <li
                    key={p.platform_channel_id}
                    onClick={() => setSelected(p.platform_channel_id)}
                    className={`px-3 py-2.5 cursor-pointer hover:bg-white/5 ${
                      isActive ? "bg-white/10" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      {p.thumbnail_url ? (
                        <AuthImage
                          src={p.thumbnail_url}
                          alt=""
                          className="w-8 h-8 rounded-full object-cover shrink-0 bg-background"
                          fallbackLabel="yt"
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
                      <span>{compactCount(p.subscriber_count)} subs</span>
                      <span>
                        {p.videos_collected ?? 0} videos
                        {p.last_video_at ? ` · ${relativeTime(p.last_video_at)}` : ""}
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
          ) : !profile.data?.channel ? (
            <div className="bg-surface rounded-lg border border-border p-8">
              <p className="text-sm text-text-muted text-center">
                Channel not found.
              </p>
            </div>
          ) : (
            <>
              <ProfileCard p={profile.data.channel} postCount={profile.data.videos.length} />
              {profile.data.videos.length === 0 ? (
                <div className="bg-surface rounded-lg border border-border p-8">
                  <p className="text-sm text-text-muted text-center">
                    No videos collected for this channel yet.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
                  {profile.data.videos.map((post) => (
                    <PostCard key={post.platform_video_id} post={post} />
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

function CompletenessStrip({ data }: { data?: YoutubeCompleteness }) {
  if (!data) {
    return null;
  }
  const videos = data.videos;
  const channels = data.channels;
  const targets = data.targets;
  const backlog = data.media_backlog;
  const archivedVideos = numberFromRecord(videos, "archived_video_files");
  const totalVideos = numberFromRecord(videos, "total_videos");
  const missingEligible = numberFromRecord(backlog, "eligible_missing_videos");
  const pendingProfiles =
    data.profile_queue?.by_status?.find((row) => row.status === "pending")?.profiles ?? 0;
  const pendingSpider =
    data.spider_queue?.by_status?.find((row) => row.status === "pending")?.channels ?? 0;

  const items = [
    {
      label: "Channels",
      value: formatNumber(numberFromRecord(channels, "total_channels")),
      sub: `${formatNumber(numberFromRecord(channels, "channels_with_profile_photo_media"))} profile photos`,
    },
    {
      label: "Videos",
      value: `${formatNumber(archivedVideos)} / ${formatNumber(totalVideos)}`,
      sub: `${formatNumber(missingEligible)} eligible files missing`,
    },
    {
      label: "Transcripts",
      value: formatNumber(numberFromRecord(videos, "transcript_status_stored")),
      sub: `${formatNumber(numberFromRecord(videos, "transcript_status_pending"))} pending`,
    },
    {
      label: "Comments",
      value: formatNumber(numberFromRecord(data.comments, "comments")),
      sub: `${formatNumber(numberFromRecord(data.comments, "comments_with_author_channel"))} with author IDs`,
    },
    {
      label: "Edges",
      value: formatNumber(data.edges?.total_edges ?? 0),
      sub: `${formatNumber(pendingProfiles + pendingSpider)} discovery queued`,
    },
    {
      label: "Targets",
      value: formatNumber(numberFromRecord(targets, "youtube_targets")),
      sub: `${formatNumber(numberFromRecord(targets, "auto_discovered_targets"))} auto-discovered`,
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2 mb-4">
      {items.map((item) => (
        <div key={item.label} className="bg-surface rounded-lg border border-border px-3 py-2">
          <div className="text-[11px] uppercase tracking-wide text-text-muted">{item.label}</div>
          <div className="text-sm font-semibold text-text-primary tabular-nums">{item.value}</div>
          <div className="text-[11px] text-text-muted truncate" title={item.sub}>
            {item.sub}
          </div>
        </div>
      ))}
    </div>
  );
}

function ProfileCard({ p, postCount }: { p: YoutubeChannel; postCount: number }) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-start gap-3">
        {p.thumbnail_url && (
          <AuthImage
            src={p.thumbnail_url}
            alt=""
            className="w-16 h-16 rounded-full object-cover shrink-0 bg-background"
            fallbackLabel="yt"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h3 className="text-base font-semibold text-text-primary truncate">
              {profileDisplayName(p)}
            </h3>
          </div>
          {p.custom_url && (
            <a
              href={`https://www.youtube.com/${p.custom_url}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-info hover:underline"
            >
              {p.custom_url} <ExternalLink className="inline w-3 h-3" />
            </a>
          )}
          {p.description && (
            <p className="mt-1.5 text-xs text-text-secondary whitespace-pre-wrap break-words">
              {p.description}
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-text-muted tabular-nums">
            <span><b className="text-text-primary">{compactCount(p.subscriber_count)}</b> subs</span>
            <span><b className="text-text-primary">{compactCount(p.view_count)}</b> views</span>
            <span><b className="text-text-primary">{formatNumber(p.video_count ?? 0)}</b> videos</span>
            <span className="text-text-muted">·</span>
            <span>{postCount} collected</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function PostCard({ post }: { post: YoutubeVideo }) {
  const desc = post.title || "";

  return (
    <div className="bg-surface rounded-lg border border-border overflow-hidden flex flex-col">
      <a
        href={post.video_url}
        target="_blank"
        rel="noopener noreferrer"
        className="block relative aspect-[16/9] bg-background overflow-hidden group"
        title="Open on YouTube"
      >
        {post.media_item_id ? (
          <AuthImage
            src={api.thumbnailUrl(post.media_item_id)}
            alt=""
            className="w-full h-full object-cover transition-transform group-hover:scale-105"
            fallbackLabel="video"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-text-muted text-xs">
            no thumbnail
          </div>
        )}
        {/* corner badges: content kind */}
        {post.duration && (
          <div className="absolute bottom-1.5 right-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded tabular-nums">
            {formatDuration(post.duration)}
          </div>
        )}
        
        {/* hover overlay → view count */}
        {post.view_count != null && (
          <div className="absolute bottom-1.5 left-1.5 bg-black/60 text-white text-[10px] px-1.5 py-0.5 rounded flex items-center gap-1">
            <Play className="w-3 h-3" />
            <span className="tabular-nums">{compactCount(post.view_count)}</span>
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
          <span className="ml-auto text-text-muted">
            {post.platform_published_at ? relativeTime(post.platform_published_at) : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
