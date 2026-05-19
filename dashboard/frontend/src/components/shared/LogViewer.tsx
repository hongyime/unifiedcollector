interface LogViewerProps {
  lines: string[];
  maxHeight?: string;
}

export function LogViewer({ lines, maxHeight = "400px" }: LogViewerProps) {
  return (
    <div
      className="bg-background border border-border rounded-md p-3 overflow-auto font-mono text-xs"
      style={{ maxHeight }}
    >
      {lines.map((line, i) => (
        <div key={i} className="text-text-secondary hover:bg-white/5 px-1">
          <span className="text-text-muted mr-3 select-none">{String(i + 1).padStart(4)}</span>
          {line}
        </div>
      ))}
      {lines.length === 0 && <div className="text-text-muted">No log entries</div>}
    </div>
  );
}
