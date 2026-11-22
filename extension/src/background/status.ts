// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// status: 2 top-level defs moved verbatim; imports added for cross-module refs.

// ---- persistent logging --------------------------------------------------
// `log(level, msg)`, `LOG_KEY`, and `LOG_MAX` are imported from
// src/shared/log.js — see the module for docstrings + rationale.
export async function setStatus(patch) {
  const { ucStatus = {} } = await chrome.storage.local.get("ucStatus");
  await chrome.storage.local.set({ ucStatus: { ...ucStatus, ...patch } });
}

export async function getStatus() {
  const { ucStatus = {} } = await chrome.storage.local.get("ucStatus");
  return ucStatus;
}
