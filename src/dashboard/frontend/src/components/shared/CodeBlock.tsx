interface CodeBlockProps {
  code: string;
  language?: string;
}

export function CodeBlock({ code }: CodeBlockProps) {
  return (
    <pre className="bg-background border border-border rounded-md p-3 text-xs font-mono text-text-secondary overflow-auto">
      <code>{code}</code>
    </pre>
  );
}
