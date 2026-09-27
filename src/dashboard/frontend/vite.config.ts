/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import { env } from "node:process";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    watch: { usePolling: env.CHOKIDAR_USEPOLLING === 'true' },
    proxy: {
      "/api": env.DEV_API_URL || "http://localhost:8700",
      "/ws": { target: env.DEV_API_URL || "ws://localhost:8700", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/setupTests.ts"],
    css: false,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // 'forks' (default) times out reliably on Windows waiting for worker
    // response; 'threads' is faster and stable across platforms.
    pool: "threads",
  },
});
