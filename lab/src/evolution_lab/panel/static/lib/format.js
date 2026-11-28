// lib/format.js — display formats only; no rounding rules beyond digits (spec §2.1 arithmetic).
const HEX32 = /^[0-9a-f]{32,}$/;
export function shortId(id) {
  if (typeof id !== "string") return "—";
  const bare = id.startsWith("sha256:") ? id.slice(7) : id;
  return HEX32.test(bare) || id.startsWith("sha256:") ? bare.slice(0, 8) : id;
}
export const number = (x, digits = 3) => (typeof x === "number" && Number.isFinite(x) ? x.toFixed(digits) : "—");
export function signed(x, digits = 3) {
  if (typeof x !== "number" || !Number.isFinite(x)) return "—";
  if (x === 0) return `±${(0).toFixed(digits)}`;
  return x > 0 ? `+${x.toFixed(digits)}` : `−${Math.abs(x).toFixed(digits)}`;
}
const pad = (n) => String(n).padStart(2, "0");
export function time(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : "—";
}
export function date(iso) {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime())
    ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}` : "—";
}
export function duration(fromIso, toIso) {
  const a = fromIso ? Date.parse(fromIso) : NaN, b = toIso ? Date.parse(toIso) : NaN;
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return "—";
  const s = Math.round((b - a) / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${pad(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${pad(Math.floor((s % 3600) / 60))}m`;
}
export function tokens(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(Math.round(n));
}
