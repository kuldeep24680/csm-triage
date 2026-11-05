// The access token is short-lived (15 min). The refresh token lives in an httpOnly
// cookie scoped to /api/auth, so page scripts can never read it.
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

// One refresh at a time: parallel 401s all wait on the same promise (the server
// rotates the cookie, so two concurrent refreshes would look like token reuse).
let refreshing: Promise<boolean> | null = null;
function refreshSession(): Promise<boolean> {
  refreshing ??= fetch("/api/auth/refresh", { method: "POST", credentials: "same-origin" })
    .then(async (r) => {
      if (!r.ok) return false;
      setToken((await r.json()).token);
      return true;
    })
    .catch(() => false)
    .finally(() => { refreshing = null; });
  return refreshing;
}

function toLogin() {
  setToken(null);
  if (!location.pathname.startsWith("/login")) location.href = "/login";
}

export async function api<T = any>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const token = getToken();
  const res = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (res.status === 401 && !path.startsWith("/auth/")) {
    if (!retried && (await refreshSession())) return api<T>(path, init, true);   // expired: renew once, retry
    toLogin();
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail ?? msg; } catch { /* not json */ }
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

export async function signOut() {
  try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch { /* offline: token expires anyway */ }
  setToken(null);
}

export const fmtMoney = (v: number | string | null | undefined) =>
  v == null ? "–" : `$${Math.round(Number(v)).toLocaleString("en-US")}`;
export const fmtPct = (p: number | null | undefined) => (p == null ? "–" : `${Math.round(p * 100)}%`);
export const fmtDate = (d: string | null | undefined) =>
  d ? new Date(d.length === 10 ? d + "T00:00:00Z" : d).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "–";
export const renewalText = (days: number | null | undefined) =>
  days == null ? "renewal unknown" : days < 0 ? `renewal ${-days} days overdue` : days === 0 ? "renews today" : `renews in ${days} days`;
