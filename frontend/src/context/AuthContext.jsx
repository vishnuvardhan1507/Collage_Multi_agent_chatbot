import { createContext, useContext, useEffect, useMemo, useState } from "react";
import api from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    const cached = window.localStorage.getItem("college_assistant_user");
    return cached ? JSON.parse(cached) : null;
  });
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const token = window.localStorage.getItem("college_assistant_token");
    if (!token || user) return;
    api.get("/auth/me")
      .then((res) => {
        setUser(res.data);
        window.localStorage.setItem("college_assistant_user", JSON.stringify(res.data));
      })
      .catch(() => {
        window.localStorage.removeItem("college_assistant_token");
        window.localStorage.removeItem("college_assistant_user");
      });
  }, [user]);

  async function login(userId, password) {
    setLoading(true);
    try {
      const response = await api.post("/auth/login", { user_id: userId, password });
      const nextUser = { user_id: userId, name: response.data.name, role: response.data.role };
      window.localStorage.setItem("college_assistant_token", response.data.access_token);
      window.localStorage.setItem("college_assistant_user", JSON.stringify(nextUser));
      setUser(nextUser);
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    window.localStorage.removeItem("college_assistant_token");
    window.localStorage.removeItem("college_assistant_user");
    setUser(null);
  }

  const value = useMemo(() => ({ user, loading, login, logout }), [user, loading]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
