import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { api, getToken, setToken } from "./api";
import { AccountPage, CallListPage, DataQualityPage, ModelPage, RunsPage, TeamPage } from "./pages";
import { Me, MeCtx } from "./ctx";
import "./styles.css";


function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      setToken(r.token);
      nav("/");
    } catch (e: any) {
      setErr(e.message === "invalid email or password" ? "That email and password don't match a user. Check both and try again." : e.message);
    } finally {
      setBusy(false);
    }
  };
  // The demo password is only offered outside production; in production the buttons just fill the email.
  const [demoPw, setDemoPw] = useState<string | null>(null);
  useEffect(() => { api<{ demo_password: string | null }>("/auth/config").then((c) => setDemoPw(c.demo_password)).catch(() => {}); }, []);
  const demo = (e: string) => { setEmail(e); if (demoPw) setPassword(demoPw); };
  return (
    <div className="login">
      <form onSubmit={submit}>
        <div className="brand"><span className="brand-mark" /> Triage</div>
        <p className="sub" style={{ marginTop: 8 }}>Who to call this week, and why.</p>
        <label htmlFor="email">Work email</label>
        <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />
        <label htmlFor="pw">Password</label>
        <input id="pw" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        {err && <p className="error">{err}</p>}
        <div className="actions"><button className="btn" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button></div>
        <div className="demo-logins">
          {demoPw ? `Demo accounts (password ${demoPw}):` : "Demo accounts (ask for the password):"}
          <button type="button" onClick={() => demo("manager@northwind.example")}>Northwind CS manager</button>
          <button type="button" onClick={() => demo("manager@helios.example")}>Helios CS manager</button>
          <button type="button" onClick={() => demo("manager@quill.example")}>Quill CS manager</button>
        </div>
      </form>
    </div>
  );
}

function Shell() {
  const [me, setMe] = useState<Me | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const nav = useNavigate();
  useEffect(() => { api<Me>("/me").then(setMe).catch((e) => setErr(e.message)); }, []);
  if (err) return <div className="main"><p className="error">Couldn't load your profile: {err}</p></div>;
  if (!me) return <div className="main muted">Loading…</div>;
  const signOut = () => { setToken(null); nav("/login"); };
  return (
    <MeCtx.Provider value={me}>
      <div className="shell">
        <aside className="rail">
          <div>
            <div className="brand"><span className="brand-mark" /> Triage</div>
            <div className="tenant">{me.tenant.name}</div>
          </div>
          <nav className="nav">
            <NavLink to="/" end>{me.user.role === "manager" ? "Call lists" : "My week"}</NavLink>
            {me.user.role === "manager" && <NavLink to="/team">Team</NavLink>}
            <NavLink to="/runs">Agent runs</NavLink>
            <NavLink to="/model">Model</NavLink>
            <NavLink to="/data">Data quality</NavLink>
          </nav>
          <div className="rail-foot">
            {me.user.name}
            <br />
            <button onClick={signOut}>Sign out</button>
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<CallListPage />} />
            <Route path="/team" element={<TeamPage />} />
            <Route path="/accounts/:id" element={<AccountPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/model" element={<ModelPage />} />
            <Route path="/data" element={<DataQualityPage />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </main>
      </div>
    </MeCtx.Provider>
  );
}

function RequireAuth() {
  // read the token at render time, so signing in takes effect immediately
  return getToken() ? <Shell /> : <Navigate to="/login" />;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/*" element={<RequireAuth />} />
      </Routes>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
