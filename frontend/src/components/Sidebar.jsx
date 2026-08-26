import { GraduationCap, LogOut, MessageSquarePlus, Sparkles } from "lucide-react";

const studentPrompts = [
  "What is my attendance in Machine Learning?",
  "Show my enrolled and pending courses",
  "Submit leave from 2026-08-20 to 2026-08-22 for a medical appointment",
  "What is the minimum attendance policy?"
];

const facultyPrompts = [
  "Show my assigned classes",
  "Which leave requests are pending for my students?",
  "Check Lab-302 availability on 2026-08-21",
  "Book Seminar-1 on 2026-08-22 from 10:00 to 11:00 for orientation"
];

export default function Sidebar({ user, sessionId, onNewSession, onPrompt, onLogout }) {
  const prompts = user.role === "faculty" ? facultyPrompts : studentPrompts;
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <GraduationCap size={26} />
        </div>
        <div>
          <strong>College Assistant</strong>
          <span>Multi-agent console</span>
        </div>
      </div>

      <section className="profile">
        <span className="role-pill">{user.role}</span>
        <h2>{user.name}</h2>
        <p>{user.user_id}</p>
      </section>

      <button className="toolbar-button" onClick={onNewSession} title="New session">
        <MessageSquarePlus size={18} />
        <span>New Session</span>
      </button>

      <div className="session-label">
        <span>Current</span>
        <strong>{sessionId}</strong>
      </div>

      <section className="prompt-list">
        <div className="prompt-heading">
          <Sparkles size={16} />
          <span>Demo prompts</span>
        </div>
        {prompts.map((prompt) => (
          <button key={prompt} onClick={() => onPrompt(prompt)}>
            {prompt}
          </button>
        ))}
      </section>

      <button className="logout-button" onClick={onLogout} title="Log out">
        <LogOut size={18} />
        <span>Log out</span>
      </button>
    </aside>
  );
}
