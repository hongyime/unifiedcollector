import { clsx } from "clsx";
import { Loader2 } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: ReactNode;
}

const variants = {
  primary: "bg-white text-black hover:bg-white/90",
  ghost: "bg-transparent text-text-primary hover:bg-white/10 border border-border",
  danger: "bg-error text-white hover:bg-error/90",
};

const sizes = {
  sm: "text-xs px-2 py-1 gap-1",
  md: "text-sm px-3 py-2 gap-2",
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  icon,
  children,
  className,
  disabled,
  ...props
}: ButtonProps) {
  return (
    <button
      className={clsx(
        "inline-flex items-center justify-center font-medium rounded-md transition-colors",
        "focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-50",
        variants[variant],
        sizes[size],
        className,
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}
