const TOKEN_KEY = "triage.token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string | null) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (res.status === 401) {
    setToken(null);
    if (!location.pathname.startsWith("/login")) location.href = "/login";
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail ?? msg; } catch { /* not json */ }
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

export const fmtMoney = (v: number | string | null | undefined) =>
  v == null ? "–" : `$${Math.round(Number(v)).toLocaleString("en-US")}`;
export const fmtPct = (p: number | null | undefined) => (p == null ? "–" : `${Math.round(p * 100)}%`);
export const fmtDate = (d: string | null | undefined) =>
  d ? new Date(d.length === 10 ? d + "T00:00:00Z" : d).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "–";
export const renewalText = (days: number | null | undefined) =>
  days == null ? "renewal unknown" : days < 0 ? `renewal ${-days} days overdue` : days === 0 ? "renews today" : `renews in ${days} days`;
