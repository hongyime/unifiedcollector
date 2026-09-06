import { clsx } from "clsx";

interface FilterDropdownProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
}

export function FilterDropdown({ label, value, onChange, options, className }: FilterDropdownProps) {
  return (
    <div className={clsx("flex items-center gap-2", className)}>
      <span className="text-xs text-text-muted">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-background border border-border rounded-md text-sm text-text-primary px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-white/20"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}
