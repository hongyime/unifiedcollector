import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";

function BridgeCard({ bridge }: { bridge: 1 | 2 }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);

  // Poll every 3s. Once a bridge reports ready we keep polling (slower) so a
  // stale/phantom "connected" can never freeze the panel -- it always reflects
  // the bridge's live /health state.
  const { data } = useQuery({
    queryKey: ["wa-qr", bridge],
    queryFn: () => api.waQr(bridge),
    refetchInterval: (q) => (q.state.data?.ready ? 10_000 : 3_000),
    refetchOnWindowFocus: true,
  });

  // Session identity (phone_number + push_name) — only meaningful when the
  // bridge is paired. Poll every 15s while connected; every 5s while trying
  // to connect (a fresh QR scan flips identity into place within one tick).
  const { data: sessions } = useQuery({
    queryKey: ["wa-sessions"],
    queryFn: () => api.waSessions(),
    refetchInterval: (q) => {
      const anyConnected = (q.state.data?.sessions ?? []).some((s) => s.connected);
      return anyConnected ? 15_000 : 5_000;
    },
    refetchOnWindowFocus: true,
  });
  const identity = sessions?.sessions.find((s) => s.bridge === String(bridge));

  const refresh = () => qc.invalidateQueries({ queryKey: ["wa-qr", bridge] });
  const disconnect = useMutation({
    mutationFn: () => api.waDisconnect(bridge),
    onSuccess: (r) => { setMsg(r.ok ? "Unpaired — scan the new QR to re-link." : `Failed: ${r.error}`); refresh(); },
  });
  const reconnect = useMutation({
    mutationFn: () => api.waReconnect(bridge),
    onSuccess: (r) => { setMsg(r.ok ? "Reconnecting (keeping session)…" : `Failed: ${r.error}`); refresh(); },
  });
  const freshQr = useMutation({
    mutationFn: () => api.waFreshQr(bridge),
    onSuccess: (r) => {
      setMsg(r.ok ? "Fresh QR requested. Leave this panel open for the next code." : `Failed: ${r.error}`);
      refresh();
    },
  });

  const ready = data?.ready;
  const status = data?.status ?? "loading";
  const qrSrc = data?.qr
    ? data.qr.startsWith("data:")
      ? data.qr
      : `data:image/png;base64,${data.qr}`
    : null;

  return (
    <div className="bg-surface border border-border rounded-lg p-5 w-[340px]">
      <h2 className="text-base font-semibold mb-3 flex items-center gap-2">
        Bridge {bridge}
        <span
          className={`inline-block w-2.5 h-2.5 rounded-full ${
            ready ? "bg-success" : status === "unreachable" ? "bg-danger" : "bg-warning"
          }`}
        />
      </h2>
      <div className="w-[280px] h-[280px] mx-auto flex items-center justify-center rounded-lg bg-white">
        {ready ? (
          <span className="text-success text-7xl">&#10003;</span>
        ) : status === "unreachable" ? (
          <span className="text-danger text-5xl">&#9888;</span>
        ) : qrSrc ? (
          <img src={qrSrc} alt={`Bridge ${bridge} QR`} className="w-[264px] h-[264px] [image-rendering:pixelated]" />
        ) : (
          <span className="text-text-muted text-sm">Loading&hellip;</span>
        )}
      </div>
      <p className="mt-3 text-center text-sm min-h-[20px]">
        {ready ? (
          <span className="text-success font-semibold">Connected</span>
        ) : status === "unreachable" ? (
          <span className="text-danger">Bridge unreachable</span>
        ) : status === "qr_renderer_missing" ? (
          <span className="text-danger">Dashboard QR renderer missing</span>
        ) : qrSrc ? (
          <span className="text-text-muted">Scan this code now. It refreshes automatically.</span>
        ) : status === "connecting_unpaired" || status === "connecting" ? (
          <span className="text-text-muted">Bridge is starting. A QR should appear shortly.</span>
        ) : status === "fresh_qr_requested" || status === "auth_cleared" ? (
          <span className="text-text-muted">Fresh QR requested. Waiting for the new code.</span>
        ) : (
          <span className="text-text-muted">{status}&hellip;</span>
        )}
      </p>
      {data?.last_qr_at && !ready && (
        <p className="mt-1 text-center text-[11px] text-text-muted">
          QR issued {new Date(data.last_qr_at).toLocaleTimeString()}
        </p>
      )}

      {/* Session identity — surfaces the paired account (phone + WhatsApp
          display name) so we can tell WHICH account is on WHICH bridge slot.
          Only rendered when the bridge reports connected AND identity
          resolution succeeded (bridge itself must be reachable). */}
      {ready && identity?.ok && identity.connected && identity.phone_number && (
        <div className="mt-3 text-center">
          <div className="font-mono text-sm text-text-primary font-semibold">
            +{identity.phone_number}
          </div>
          {identity.push_name && (
            <div className="text-xs text-text-muted mt-0.5">
              {identity.push_name}
            </div>
          )}
          {identity.session_name && (
            <div className="text-[10px] text-text-muted mt-0.5">
              slot: {identity.session_name}
            </div>
          )}
        </div>
      )}

      {/* Per-device controls — independent of the other bridge. */}
      <div className="mt-4 flex items-center justify-center gap-2">
        <button
          onClick={() => reconnect.mutate()}
          disabled={reconnect.isPending}
          className="text-xs px-2.5 py-1 rounded-md border border-border text-text-secondary hover:bg-white/5 disabled:opacity-50"
          title="Soft reconnect — keeps the session, no re-scan"
        >{reconnect.isPending ? "…" : "Reconnect"}</button>
        <button
          onClick={() => freshQr.mutate()}
          disabled={freshQr.isPending}
          className="text-xs px-2.5 py-1 rounded-md border border-border text-text-secondary hover:bg-white/5 disabled:opacity-50"
          title="Clear local auth and request a fresh QR"
        >{freshQr.isPending ? "…" : "Fresh QR"}</button>
        <button
          onClick={() => { if (confirm(`Unpair Bridge ${bridge}? You'll need to scan a new QR to re-link.`)) disconnect.mutate(); }}
          disabled={disconnect.isPending}
          className="text-xs px-2.5 py-1 rounded-md border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-50"
          title="Unpair this device (logout) — then scan the new QR"
        >{disconnect.isPending ? "…" : "Disconnect"}</button>
      </div>
      {msg && <p className="mt-2 text-center text-[11px] text-text-muted">{msg}</p>}
    </div>
  );
}

export function WhatsAppLinkPage() {
  return (
    <div>
      <Header title="Link WhatsApp" subtitle="Scan to pair a WhatsApp account to a bridge" />
      <p className="text-sm text-text-muted mb-5 max-w-2xl">
        Two independent account slots &mdash; link either one in any order. The QR refreshes
        automatically, so just leave this open. A panel turns green the instant the bridge pairs.
      </p>
      <div className="flex gap-6 flex-wrap">
        <BridgeCard bridge={1} />
        <BridgeCard bridge={2} />
      </div>
      <div className="text-sm text-text-muted leading-relaxed mt-6 max-w-2xl">
        <b>On your phone:</b> WhatsApp &rarr; Settings &rarr; <b>Linked Devices</b> &rarr;{" "}
        <b>Link a Device</b> &rarr; point the camera at a QR above.
      </div>
    </div>
  );
}
