/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: { base: '#0a0a0a', surface: '#111111', elevated: '#161616', hover: '#1a1a1a' },
        border: { DEFAULT: '#1e1e1e', subtle: '#161616', strong: '#2a2a2a' },
        text: { primary: '#ffffff', secondary: '#a0a0a0', muted: '#606060', disabled: '#404040' },
        accent: {
          DEFAULT: '#ffffff',
          80: 'rgba(255,255,255,0.8)',
          60: 'rgba(255,255,255,0.6)',
          20: 'rgba(255,255,255,0.2)',
          10: 'rgba(255,255,255,0.1)',
          5: 'rgba(255,255,255,0.05)',
        },
        status: { up: '#22c55e', down: '#ef4444', unknown: '#6b7280', pending: '#f59e0b', running: '#3b82f6' },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'Monaco', 'monospace'],
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      animation: { 'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite' },
    },
  },
  plugins: [],
}
