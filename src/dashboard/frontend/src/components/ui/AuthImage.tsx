import { useEffect, useState } from "react";
import { API_BASE } from "../../utils/constants";

type AuthImageProps = {
  src: string;
  alt: string;
  className?: string;
  loading?: "eager" | "lazy";
  fallbackLabel?: string;
};

function shouldFetchWithAuth(src: string): boolean {
  const token = localStorage.getItem("auth_token");
  if (!token) return false;
  if (src.startsWith("/")) return src.startsWith("/media/");
  try {
    const url = new URL(src, window.location.origin);
    if (url.origin === window.location.origin && url.pathname.startsWith("/media/")) {
      return true;
    }
    if (API_BASE) {
      const apiUrl = new URL(API_BASE, window.location.origin);
      return url.origin === apiUrl.origin && url.pathname.startsWith("/media/");
    }
  } catch {
    return false;
  }
  return false;
}

export function AuthImage({ src, alt, className, loading = "lazy", fallbackLabel = "preview" }: AuthImageProps) {
  const [resolvedSrc, setResolvedSrc] = useState(src);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let revoked: string | null = null;
    let cancelled = false;
    setFailed(false);
    setResolvedSrc(src);

    if (!shouldFetchWithAuth(src)) {
      return;
    }

    const token = localStorage.getItem("auth_token");
    fetch(src, { headers: token ? { Authorization: `Bearer ${token}` } : undefined })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        revoked = URL.createObjectURL(blob);
        setResolvedSrc(revoked);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [src]);

  if (failed) {
    return (
      <div className={`${className ?? ""} flex items-center justify-center bg-background text-[10px] uppercase tracking-wide text-text-muted`}>
        {fallbackLabel}
      </div>
    );
  }

  return (
    <img
      src={resolvedSrc}
      alt={alt}
      loading={loading}
      className={className}
      onError={() => setFailed(true)}
    />
  );
}
