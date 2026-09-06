// Standalone entry point for tabs.html, which loads dist/platforms.js as a
// classic <script> tag (not a module). This file re-exports the shared
// registry and installs it on globalThis / window so the classic-script
// context has access to UC_PLATFORMS with no ES-module machinery.
//
// Bundled by esbuild as an IIFE — the import below is resolved at build
// time and the resulting dist/platforms.js is a plain <script>-safe blob.
import { UC_PLATFORMS } from "./shared/platforms.js";

globalThis.UC_PLATFORMS = UC_PLATFORMS;
if (typeof window !== "undefined") window.UC_PLATFORMS = UC_PLATFORMS;
