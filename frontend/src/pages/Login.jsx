import { useEffect, useState } from "react";
import { BookOpen, Building2, ChevronDown, GraduationCap, KeyRound, LoaderCircle, LogIn, Mail, UserPlus, UserRound } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import api from "../api/client.js";
import "./auth.css";

const departments = ["Computer Science", "Electronics", "Mathematics", "Management", "Information Technology", "Mechanical Engineering", "Civil Engineering"];

function currentView() {
  const route = window.location.hash.slice(1).split("?")[0];
  return ["signup", "forgot-password", "reset-password"].includes(route) ? route : "login";
}

function BrandMark() {
  return (
    <svg className="nexus-mark" viewBox="0 0 88 88" role="img" aria-label="NexusIQ logo">
      <path d="M32 13a31 31 0 0 1 42 38M17 29a31 31 0 0 0 35 45" fill="none" stroke="#091466" strokeWidth="10" strokeLinecap="round" />
      <circle cx="43" cy="42" r="12" fill="#09b2dc" />
      <path d="m57 58 14 14" fill="none" stroke="#09b2dc" strokeWidth="13" strokeLinecap="round" />
    </svg>
  );
}

function Field({ label, icon: Icon, children }) {
  return <label className="auth-field"><span>{label}</span><div className="auth-input"><Icon size={24} strokeWidth={1.8} aria-hidden="true" />{children}</div></label>;
}

export default function Login() {
  const { login, loading, logout } = useAuth();
  const [view, setView] = useState(currentView);
  const [form, setForm] = useState({ full_name: "", user_id: "", email: "", otp: "", password: "", confirmation: "", role: "student", department: "Computer Science" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [resendSeconds, setResendSeconds] = useState(0);
  useEffect(() => {
    if (!resendSeconds) return;
    const timer = window.setInterval(() => setResendSeconds(value => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [resendSeconds > 0]);

  useEffect(() => {
    function change() {
      setView(currentView());
      setError("");
      setNotice("");
      setForm((current) => ({ ...current, otp: "", password: "", confirmation: "" }));
    }
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);

  function navigate(next, message = "") {
    window.history.replaceState(null, "", `#${next}`);
    setView(next);
    setError("");
    setNotice(message);
    setForm((current) => ({ ...current, otp: "", password: "", confirmation: "" }));
    window.dispatchEvent(new Event("auth:navigation"));
  }

  function update(event) {
    const { name, value } = event.target;
    setForm((current) => ({ ...current, [name]: value }));
  }

  async function submit(event) {
    event.preventDefault();
    if (busy || loading) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      if (view === 'login' || view === 'signup') {
        const id = form.user_id.trim();
        const prefix = form.role === 'faculty' ? 'FAC' : 'STU';
        if (view === 'signup' && !id.startsWith(prefix)) throw new Error(`Username should start with ${prefix} for ${form.role}.`);
        if (view === 'login' && !/^(STU|FAC)/.test(id)) throw new Error('Username should start with STU for students or FAC for faculty.');
      }
      if (view === "login") {
        await login(form.user_id.trim(), form.password);
        window.history.replaceState(null, "", window.location.pathname);
        window.dispatchEvent(new Event("auth:navigation"));
      } else if (view === "signup") {
        await api.post("/auth/register", { full_name: form.full_name, user_id: form.user_id, email: form.email, password: form.password, role: form.role, department: form.department });
        navigate("login", "Account created. Log in to get started.");
      } else if (view === "forgot-password") {
        const response = await api.post("/auth/forgot-password", { user_id: form.user_id.trim(), email: form.email });
        setResendSeconds(response.data.resend_after || 60);
        navigate('reset-password', response.data.message);
      } else {
        if (form.password !== form.confirmation) throw new Error("Your passwords do not match.");
        const response = await api.post("/auth/reset-password", { user_id: form.user_id.trim(), email: form.email, otp: form.otp, password: form.password });
        logout();
        setForm(current => ({ ...current, user_id: response.data.user_id }));
        navigate("login", response.data.message);
      }
    } catch (err) {
      setError(err.response?.data?.error || err.message || "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function resendOtp() {
    if (busy || resendSeconds) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const response = await api.post('/auth/forgot-password', { user_id: form.user_id.trim(), email: form.email });
      setNotice(response.data.message);
      setResendSeconds(response.data.resend_after || 60);
      setForm(current => ({ ...current, otp: '' }));
    } catch (err) { setError(err.response?.data?.error || 'Could not request a new OTP. Please try again.'); }
    finally { setBusy(false); }
  }

  const signup = view === "signup";
  const recovery = view === "forgot-password" || view === "reset-password";
  const submitting = busy || loading;
  const ButtonIcon = submitting ? LoaderCircle : signup ? UserPlus : view === "forgot-password" ? Mail : view === "reset-password" ? KeyRound : LogIn;
  const buttonText = submitting ? (view === "login" ? "Logging in…" : "Please wait…") : signup ? "Create account" : view === "forgot-password" ? "Send OTP" : view === "reset-password" ? "Reset password" : "Login";

  return (
    <main className={`auth-scene ${signup ? "auth-scene-signup" : ""}`}>
      <div className="auth-decoration" aria-hidden="true">
        <div className="auth-corner" /><div className="auth-wave" />
        <div className="auth-dots auth-dots-top" /><div className="auth-dots auth-dots-left" /><div className="auth-dots auth-dots-right" />
        <GraduationCap className="auth-cap" strokeWidth={1.45} />
        <div className="auth-building"><Building2 strokeWidth={1.5} /><span /></div>
        <BookOpen className="auth-book" strokeWidth={1.45} />
      </div>
      <section className={`auth-card ${signup ? "auth-card-signup" : ""} ${recovery ? "auth-card-recovery" : ""}`} aria-labelledby="auth-heading">
        <header className="auth-brand"><BrandMark /><h1 id="auth-heading">NexusIQ<br />Institution</h1></header>
        {recovery && <div className="auth-intro"><h2>{view === "forgot-password" ? "Forgot password?" : "Reset your password"}</h2><p>{view === "forgot-password" ? "Enter your account ID and its registered email to receive a six-digit OTP." : "Enter the OTP from your email and choose a new password. The code expires in 10 minutes."}</p></div>}
        <form className="auth-form" onSubmit={submit}>
          {signup && <Field label="Full name" icon={UserRound}><input name="full_name" placeholder="Your full name" autoComplete="name" value={form.full_name} onChange={update} required minLength={2} maxLength={100} disabled={submitting} /></Field>}
          {<Field label="ID" icon={UserRound}><input aria-label="User ID" name="user_id" placeholder={signup && form.role === 'faculty' ? 'FAC001' : 'STU001'} autoComplete="username" value={form.user_id} onChange={update} required maxLength={40} disabled={submitting} /></Field>}
          {(signup || recovery) && <Field label="Registered email" icon={Mail}><input name="email" type="email" placeholder="Enter your email" autoComplete="email" value={form.email} onChange={update} required maxLength={254} disabled={submitting} /></Field>}
          {view === 'reset-password' && <Field label="OTP" icon={KeyRound}><input name="otp" type="text" inputMode="numeric" autoComplete="one-time-code" placeholder="Six-digit code" value={form.otp} onChange={event => setForm(current => ({ ...current, otp: event.target.value.replace(/\D/g, '').slice(0, 6) }))} required pattern="[0-9]{6}" minLength={6} maxLength={6} disabled={submitting} /></Field>}
          {view !== "forgot-password" && <Field label={view === "reset-password" ? "New password" : "Password"} icon={KeyRound}><input name="password" type="password" placeholder={signup ? "At least 6 characters" : ""} autoComplete={view === "login" ? "current-password" : "new-password"} value={form.password} onChange={update} required minLength={view === "login" ? undefined : 6} maxLength={128} disabled={submitting} /></Field>}
          {view === "reset-password" && <Field label="Confirm password" icon={KeyRound}><input name="confirmation" type="password" placeholder="Re-enter your password" autoComplete="new-password" value={form.confirmation} onChange={update} required minLength={6} maxLength={128} disabled={submitting} /></Field>}
          {signup && <>
            <Field label="Account type" icon={UserRound}><select name="role" value={form.role} onChange={update} disabled={submitting}><option value="student">Student</option><option value="faculty">Faculty</option></select><ChevronDown className="auth-select-arrow" size={20} aria-hidden="true" /></Field>
            <Field label="Department" icon={Building2}><select name="department" value={form.department} onChange={update} disabled={submitting}>{departments.map((department) => <option key={department}>{department}</option>)}</select><ChevronDown className="auth-select-arrow" size={20} aria-hidden="true" /></Field>
          </>}
          {view === "login" && <button type="button" className="auth-link auth-forgot" onClick={() => navigate("forgot-password")} disabled={submitting}>Forgot password?</button>}
          {error && <p className="auth-message auth-error" role="alert">{error}</p>}
          {notice && <p className="auth-message auth-success" role="status">{notice}</p>}
          <button className="auth-submit" type="submit" disabled={submitting}><ButtonIcon size={25} className={submitting ? "auth-spinner" : ""} aria-hidden="true" /><span>{buttonText}</span></button>
        </form>
        <footer className="auth-footer">
          {view === "login" ? <><span>New to NexusIQ?</span><button className="auth-link" type="button" onClick={() => navigate("signup")} disabled={submitting}>Sign up</button></> : <><span>Back to account access?</span><button className="auth-link" type="button" onClick={() => navigate("login")} disabled={submitting}>Login</button></>}
        </footer>
        {view === "reset-password" && <button type="button" className="auth-link auth-new-link" onClick={resendOtp} disabled={submitting || resendSeconds > 0}>{resendSeconds ? `Resend OTP in ${resendSeconds}s` : 'Resend OTP'}</button>}
      </section>
    </main>
  );
}
