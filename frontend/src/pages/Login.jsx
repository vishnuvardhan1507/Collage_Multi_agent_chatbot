import { useState } from "react";
import { KeyRound, LogIn, UserRound } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";

export default function Login() {
  const { login, loading } = useAuth();
  const [userId, setUserId] = useState("192125022");
  const [password, setPassword] = useState("password123");
  const [error, setError] = useState("");

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    try {
      await login(userId, password);
    } catch (err) {
      setError(err.response?.data?.error || "Unable to sign in");
    }
  }

  return (
    <main className="login-shell">
      <section className="login-visual">
        <div className="campus-panel">
          <div className="campus-sky" />
          <div className="campus-building">
            <span />
            <span />
            <span />
            <span />
          </div>
          <div className="campus-steps" />
        </div>
      </section>
      <section className="login-panel">
        <div className="login-copy">
          <span className="eyebrow">LangGraph + Groq + SQLite</span>
          <h1>College Multi-Agent Assistant</h1>
          <p>Sign in as a seeded student or faculty member and ask scoped academic, leave, classroom, and handbook questions.</p>
        </div>
        <form onSubmit={handleSubmit} className="login-form">
          <label>
            <span>User ID</span>
            <div className="input-wrap">
              <UserRound size={18} />
              <input value={userId} onChange={(event) => setUserId(event.target.value)} />
            </div>
          </label>
          <label>
            <span>Password</span>
            <div className="input-wrap">
              <KeyRound size={18} />
              <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
            </div>
          </label>
          {error && <p className="form-error">{error}</p>}
          <button type="submit" disabled={loading}>
            <LogIn size={18} />
            <span>{loading ? "Signing in" : "Sign in"}</span>
          </button>
          <p className="hint">Try student 192125022 or faculty FAC001. Password: password123</p>
        </form>
      </section>
    </main>
  );
}
