import { useEffect, useState } from "react";
import Login from "./pages/Login.jsx";
import Admin from "./pages/Admin.jsx";
import Chat from "./pages/Chat.jsx";
import { useAuth } from "./context/AuthContext.jsx";

export default function App() {
  const [resetting, setResetting] = useState(() => window.location.hash.startsWith("#reset-password"));
  useEffect(() => {
    const update = () => setResetting(window.location.hash.startsWith("#reset-password"));
    window.addEventListener("hashchange", update);
    window.addEventListener("auth:navigation", update);
    return () => { window.removeEventListener("hashchange", update); window.removeEventListener("auth:navigation", update); };
  }, []);
  const { user, initializing } = useAuth();
  if (initializing) return <main className="empty-state">Checking your session...</main>;
  return user && !resetting ? (user.role === "admin" ? <Admin /> : <Chat key={user.user_id} />) : <Login />;
}
