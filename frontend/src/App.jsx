import Login from "./pages/Login.jsx";
import Chat from "./pages/Chat.jsx";
import { useAuth } from "./context/AuthContext.jsx";

export default function App() {
  const { user } = useAuth();
  return user ? <Chat /> : <Login />;
}
