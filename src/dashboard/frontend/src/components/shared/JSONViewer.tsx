interface JSONViewerProps {
  data: unknown;
  maxHeight?: string;
}

export function JSONViewer({ data, maxHeight = "300px" }: JSONViewerProps) {
  return (
    <pre
      className="bg-background border border-border rounded-md p-3 text-xs font-mono text-text-secondary overflow-auto"
      style={{ maxHeight }}
    >
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}
