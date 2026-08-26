import { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, SendHorizonal } from "lucide-react";
import api from "../api/client.js";
import { useAuth } from "../context/AuthContext.jsx";
import MessageBubble from "../components/MessageBubble.jsx";
import Sidebar from "../components/Sidebar.jsx";
import LeavePanel from "../components/LeavePanel.jsx";

function makeSessionId() {
  return `session_${crypto.randomUUID().slice(0, 8)}`;
}

export default function Chat() {
  const { user, logout } = useAuth();
  const [sessionId, setSessionId] = useState(() => window.localStorage.getItem("college_session_id") || "session_001");
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    window.localStorage.setItem("college_session_id", sessionId);
    api.get(`/chat/history?session_id=${encodeURIComponent(sessionId)}`).then((res) => {
      const session = res.data.sessions?.find((item) => item.session_id === sessionId);
      setMessages(session?.messages || []);
    });
  }, [sessionId]);

  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  const placeholder = useMemo(() => {
    return user.role === "faculty"
      ? "Ask about your classes, students, leave requests, or classroom bookings..."
      : "Ask about your attendance, courses, results, leave, or college policies...";
  }, [user.role]);

  async function sendMessage(text = draft) {
    const content = text.trim();
    if (!content || sending) return;
    setNotice(null);
    setDraft("");
    setMessages((current) => [...current, { role: "user", content }]);
    setSending(true);
    try {
      const response = await api.post("/chat", { message: content, session_id: sessionId });
      setMessages((current) => [
        ...current,
        { role: "assistant", content: response.data.response, trace: response.data.agent_trace }
      ]);
    } catch (err) {
      setNotice({ type: "error", text: err.response?.data?.error || "The assistant could not complete that request." });
    } finally {
      setSending(false);
    }
  }

  function handleSubmit(event) {
    event.preventDefault();
    sendMessage();
  }

  function newSession() {
    setSessionId(makeSessionId());
    setMessages([]);
  }

  return (
    <main className="chat-shell">
      <Sidebar
        user={user}
        sessionId={sessionId}
        onNewSession={newSession}
        onPrompt={sendMessage}
        onLogout={logout}
      />
      <section className="chat-panel">
        <header className="chat-header">
          <div>
            <span className="eyebrow">Authenticated as {user.role}</span>
            <h1>Assistant</h1>
          </div>
          <div className="status-cluster">
            <span>Guardrail</span>
            <span>Supervisor</span>
            <span>Tools</span>
          </div>
        </header>

        <LeavePanel user={user} onNotice={setNotice} />

        <div className="messages">
          {messages.length === 0 && (
            <div className="empty-state">
              <strong>Start with a scoped college question.</strong>
              <span>The trace under each answer shows which agents handled the request.</span>
            </div>
          )}
          {messages.map((message, index) => (
            <MessageBubble message={message} key={`${message.role}-${index}`} />
          ))}
          {sending && <div className="typing">Thinking through the agent graph...</div>}
          <div ref={scrollRef} />
        </div>

        {notice && (
          <div className={`chat-notice notice-${notice.type}`}>
            <AlertCircle size={17} />
            <span>{notice.text}</span>
          </div>
        )}

        <form className="composer" onSubmit={handleSubmit}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={placeholder}
            rows={1}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
              }
            }}
          />
          <button disabled={sending || !draft.trim()} title="Send message">
            <SendHorizonal size={20} />
          </button>
        </form>
      </section>
    </main>
  );
}
