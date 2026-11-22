// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// util: 6 top-level defs moved verbatim; imports added for cross-module refs.

export const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export const _jitterMs = (base, spread = 0.55) => Math.round(base * (1 - spread + Math.random() * spread * 2));

export const _humanGap = (base) => {
  let ms = _jitterMs(base, 0.65);
  if (Math.random() < 0.12) ms += 3000 + Math.random() * 9000;
  return Math.max(750, Math.round(ms));
};

export const _randBetween = (min, max) => Math.round(min + Math.random() * (max - min));

export const _alarmJitter = () => _sleep(15000 + Math.random() * 120000);

export async function mapLimit(items, limit, worker) {
  const out = [];
  let next = 0;
  const runners = Array.from({ length: Math.max(1, Math.min(limit, items.length || 1)) }, async () => {
    while (next < items.length) {
      const index = next++;
      out[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  return out;
}
