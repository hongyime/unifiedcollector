import { Header } from "../../components/layout/Header";
import { SOURCES } from "../../utils/constants";

export function SettingsPage() {
  return (
    <div>
      <Header title="Settings" subtitle="Configuration" />

      <div className="bg-surface rounded-lg border border-border p-4 mb-4">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-3">Registered Sources</h2>
        <div className="grid grid-cols-5 gap-2">
          {SOURCES.map((s) => (
            <div key={s} className="bg-background border border-border rounded-md px-3 py-2 text-sm text-text-secondary font-mono">
              {s}
            </div>
          ))}
        </div>
      </div>

      <div className="bg-surface rounded-lg border border-border p-4">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-3">Environment</h2>
        <div className="space-y-2 text-sm text-text-secondary font-mono">
          <div>API: <span className="text-text-primary">{window.location.origin}</span></div>
          <div>Version: <span className="text-text-primary">0.1.0</span></div>
        </div>
      </div>
    </div>
  );
}
