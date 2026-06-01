import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";

function BridgeCard({ bridge }: { bridge: 1 | 2 }) {
  // Poll every 3s. Once a bridge reports ready we keep polling (slower) so a
  // stale/phantom "connected" can never freeze the panel -- it always reflects
  // the bridge's live /health state.
  const { data } = useQuery({
    queryKey: ["wa-qr", bridge],
    queryFn: () => api.waQr(bridge),
    refetchInterval: (q) => (q.state.data?.ready ? 10_000 : 3_000),
    refetchOnWindowFocus: true,
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
      <p className="mt-3 text-center text-sm">
        {ready ? (
          <span className="text-success font-semibold">Connected</span>
        ) : status === "unreachable" ? (
          <span className="text-danger">Bridge unreachable</span>
        ) : qrSrc ? (
          <span className="text-text-muted">Waiting for scan&hellip; (auto-refreshing)</span>
        ) : (
          <span className="text-text-muted">{status}&hellip;</span>
        )}
      </p>
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
