import { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Bell, CalendarDays, GraduationCap, Hand, Menu, Mic, SendHorizonal, Volume2, VolumeX, X } from "lucide-react";
import api from "../api/client.js";
import { useAuth } from "../context/AuthContext.jsx";
import MessageBubble from "../components/MessageBubble.jsx";
import Sidebar from "../components/Sidebar.jsx";
import LeavePanel from "../components/LeavePanel.jsx";
import AttendancePanel from "../components/AttendancePanel.jsx";
import BrandMark from '../components/BrandMark';
import AvatarEditor from '../components/AvatarEditor';
import useVoice from '../components/useVoice';
import AlertPanel from '../components/AlertPanel';

function makeSessionId() {
  return `session_${crypto.randomUUID()}`;
}

export default function Chat() {
  const { user, logout } = useAuth();
  const sessionKey = `college_session_id_${user.user_id}`;
  const [sessionId, setSessionId] = useState(() => window.localStorage.getItem(sessionKey) || makeSessionId());
  const [sessions, setSessions] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [clearingHistory, setClearingHistory] = useState(false);
  const [leaveRevision, setLeaveRevision] = useState(0);
  const [attendanceRevision, setAttendanceRevision] = useState(0);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [confirmationBusy, setConfirmationBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const scrollRef = useRef(null);
  const currentSession = useRef(sessionId);
  currentSession.current = sessionId;
  const [avatar, setAvatar] = useState(null);
  const [editingAvatar, setEditingAvatar] = useState(false);
  const [avatarError, setAvatarError] = useState('');
  const [leavesOpen, setLeavesOpen] = useState(false);
  const [toolTab, setToolTab] = useState('leave');
  const [alertRevision, setAlertRevision] = useState(0);
  const leaveDialog = useRef(null);
  const leaveButton = useRef(null);
  function closeLeaves() {
    leaveDialog.current?.close();
    setLeavesOpen(false);
    leaveButton.current?.focus();
  }
  useEffect(() => { if (leavesOpen) leaveDialog.current.showModal(); }, [leavesOpen]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const voice = useVoice(setDraft, text => setNotice({ type: 'error', text }));
  useEffect(() => {
    let active = true;
    api.get('/auth/me/avatar').then(res => { if (active) { setAvatar(res.data.avatar); setAvatarError(''); } }).catch(() => { if (active) setAvatarError('Your saved photo could not be loaded. You can retry by reopening this editor or upload a replacement.'); });
    return () => { active = false; };
  }, [user.user_id]);

  function applyHistory(data) {
    setSessions(data.sessions || []);
    const stored = data.sessions?.find(item => item.session_id === currentSession.current)?.messages || [];
    setMessages(current => current.map(message => {
      const latest = message.confirmation && stored.find(item => item.confirmation?.id === message.confirmation.id);
      return latest ? { ...message, confirmation: latest.confirmation } : message;
    }));
    if (data.deleted_sessions?.includes(currentSession.current)) {
      voice.stop(); setMessages([]); setSessionId(makeSessionId());
      return false;
    }
    return true;
  }
  async function refreshHistory() {
    const response = await api.get('/chat/history');
    return applyHistory(response.data);
  }

  useEffect(() => {
    let active = true;
    setHistoryLoading(true);
    window.localStorage.setItem(sessionKey, sessionId);
    api.get(`/chat/history?session_id=${encodeURIComponent(sessionId)}`).then((res) => {
      if (!active) return;
      if (!applyHistory(res.data)) return;
      const session = res.data.sessions?.find((item) => item.session_id === sessionId);
      setMessages(session?.messages || []);
    }).catch(() => {
      if (active) setNotice({ type: "error", text: "Could not load conversation history." });
    }).finally(() => { if (active) setHistoryLoading(false); });
    return () => { active = false; };
  }, [sessionId, sessionKey]);

  useEffect(() => {
    const refresh = () => { refreshHistory().catch(() => {}); };
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, []);

  useEffect(() => {
    const container = scrollRef.current;
    if (container) container.scrollTo({ top: container.scrollHeight, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }, [messages, sending]);

  const placeholder = useMemo(() => {
    return user.role === "faculty"
      ? "Ask about your classes, students, leave requests, or classroom bookings..."
      : "Ask about your attendance, courses, results, leave, or college policies...";
  }, [user.role]);

  async function sendMessage(text = draft) {
    const content = text.trim();
    if (!content || sending || historyLoading || clearingHistory) return;
    voice.stop();
    const requestSession = sessionId;
    setNotice(null);
    setDraft("");
    setMessages((current) => [...current, { role: "user", content, timestamp: new Date().toISOString() }]);
    setSending(true);
    try {
      const response = await api.post("/chat", { message: content, session_id: sessionId });
      const kept = await refreshHistory().catch(() => {
        setNotice({ type: 'error', text: 'Your answer was received, but the history list could not refresh.' });
        return true;
      });
      if (!kept || currentSession.current !== requestSession) return;
      setMessages((current) => [
        ...current,
        { role: "assistant", content: response.data.response, trace: response.data.agent_trace, sources: response.data.sources, timestamp: response.data.timestamp, confirmation: response.data.confirmation }
      ]);
      voice.readNewAnswer(response.data.response, `answer-${messages.length + 1}`);
      if (response.data.actions?.includes('mark_attendance')) setAttendanceRevision(value => value + 1);
      if (response.data.actions?.some(action => ['alert_saved','alert_deleted','research_queued'].includes(action))) setAlertRevision(value => value + 1);
      if (response.data.actions?.some((action) => ["submit_leave", "review_leave", "delete_leave"].includes(action))) {
        setLeaveRevision((current) => current + 1);
      }
    } catch (err) {
      if (currentSession.current !== requestSession) return;
      const error = err.response?.data?.error || "The assistant could not complete that request.";
      setNotice({ type: "error", text: error });
      if (err.response?.data?.response) {
        setMessages((current) => [...current, { role: "assistant", content: err.response.data.response }]);
      }
      await refreshHistory().catch(() => {});
    } finally {
      setSending(false);
    }
  }

  async function decideAction(id, decision) {
    if (confirmationBusy) return;
    const requestSession = sessionId;
    setConfirmationBusy(id); setNotice(null);
    try {
      const { data } = await api.post(`/chat/confirmations/${id}`, { session_id: requestSession, decision });
      if (currentSession.current !== requestSession) return;
      setMessages(current => [...current.map(message => message.confirmation?.id === id ? { ...message, confirmation: data.confirmation } : message).filter(message => message.confirmation_result_id !== id),
        { role: 'assistant', content: data.response, timestamp: data.timestamp, confirmation_result_id: id }]);
      if (data.actions?.includes('submit_leave')) setLeaveRevision(value => value + 1);
      if (data.actions?.includes('alert_deleted')) setAlertRevision(value => value + 1);
      await refreshHistory();
    } catch (error) {
      if (currentSession.current === requestSession) setNotice({ type: 'error', text: error.response?.data?.error || 'Could not retrieve the result. Retry safely using the same button.' });
    } finally { setConfirmationBusy(null); }
  }

  function handleSubmit(event) {
    event.preventDefault();
    sendMessage();
  }

  function newSession() {
    voice.stop(); setDraft(''); setNotice(null); setSidebarOpen(false);
    setSessionId(makeSessionId());
    setMessages([]);
  }

  async function deleteSession(id) {
    try {
      await api.delete(`/chat/history/${encodeURIComponent(id)}`);
      if (currentSession.current === id) { voice.stop(); currentSession.current = makeSessionId(); setSessionId(currentSession.current); setMessages([]); setDraft(''); }
      await refreshHistory();
    } catch { setNotice({ type: 'error', text: 'Could not delete conversation. Please try again.' }); }
  }

  async function clearHistory() {
    setClearingHistory(true);
    try {
      await api.delete('/chat/history');
      voice.stop();
      currentSession.current = makeSessionId();
      setSessionId(currentSession.current);
      setMessages([]);
      setSessions([]);
      setNotice({ type: 'success', text: 'Chat history cleared.' });
      return true;
    } catch {
      return false;
    } finally { setClearingHistory(false); }
  }

  return (
    <main className="chat-shell">
      {sidebarOpen && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} />}
      <Sidebar
        messages={messages}
        user={user}
        sessionId={sessionId}
        onNewSession={newSession}
        onPrompt={sendMessage}
        onLogout={() => { voice.stop(); logout(); }}
        sessions={sessions}
        onSelectSession={(value) => { voice.stop(); setDraft(''); setNotice(null); setMessages([]); setSessionId(value); }}
        onDeleteSession={deleteSession}
        onClearHistory={clearHistory}
        avatar={avatar}
        onEditAvatar={async () => {
          try { const res = await api.get('/auth/me/avatar'); setAvatar(res.data.avatar); setAvatarError(''); }
          catch { setAvatarError('Your saved photo could not be loaded. Please try uploading a photo.'); }
          setEditingAvatar(true);
        }}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        disabled={sending || historyLoading || clearingHistory || !!confirmationBusy}
      />
      <section className="chat-panel">
        <header className="chat-header">
          <button className="mobile-menu icon-button" aria-label="Open sidebar" onClick={() => setSidebarOpen(true)}><Menu /></button>
          <BrandMark /><div className="chat-heading">
            <span className="eyebrow">Authenticated as {user.role}</span>
            <h1>NexusIQ Assistant</h1>
          </div>
          <div className="header-actions"><div className="voice-control"><span className="voice-status" aria-live="polite">{voice.enabled ? 'On' : 'Off'}</span><button className="voice-toggle" aria-label="Toggle voice playback" title={voice.enabled ? 'Turn voice off' : 'Turn voice on'} aria-pressed={voice.enabled} disabled={!voice.canSpeak} onClick={voice.toggle}>{voice.enabled ? <Volume2 size={24} aria-hidden="true" /> : <VolumeX size={24} aria-hidden="true" />}</button></div><span className="header-bot"><img src="/images/dashboard-agent-flat.svg" alt="NexusIQ agent" width="68" height="68" /></span></div>
        </header>


        <div className="messages" ref={scrollRef}>
          {messages.map((message, index) => (
            <MessageBubble message={message} avatar={avatar} canSpeak={voice.canSpeak} speaking={voice.speaking === `answer-${index}`} paused={voice.paused} onPause={voice.togglePause} onRead={() => voice.speaking === `answer-${index}` ? voice.stop() : voice.speak(message.content, `answer-${index}`)} key={`${sessionId}-${message.role}-${index}`} onDecision={decideAction} confirmationBusy={confirmationBusy === message.confirmation?.id && !!confirmationBusy} actionsDisabled={sending || historyLoading || !!confirmationBusy} />
          ))}
          {sending && <div className="typing" role="status"><span className="thinking-label">Thinking</span><span className="thinking-dots" aria-hidden="true"><i /><i /><i /></span></div>}
        </div>

        {notice && (
          <div className={`chat-notice notice-${notice.type}`}>
            <AlertCircle size={17} />
            <span>{notice.text}</span>
          </div>
        )}

        <form className="composer" onSubmit={handleSubmit}>
          <button type="button" ref={leaveButton} className="voice-button" aria-label="Leave tools" title="Leave tools" onClick={() => { voice.stop(); setLeavesOpen(true); }}><Hand size={22} /></button>
          <button type="button" className={`voice-button ${voice.listening ? 'listening' : ''}`} aria-label={voice.listening ? 'Stop dictation' : 'Start voice typing'} aria-pressed={voice.listening} disabled={sending || historyLoading} onClick={() => voice.dictate(draft)} title={voice.canDictate ? 'Voice typing' : 'Voice typing unavailable in this browser'}><Mic size={22} /></button>
          {voice.listening && <span className="listening-label" role="status">Listening…</span>}
          <textarea
            aria-label="Message"
            disabled={historyLoading || clearingHistory}
            value={draft}
            onChange={(event) => { if (voice.listening) voice.stop(); setDraft(event.target.value); }}
            placeholder={placeholder}
            rows={1}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
              }
            }}
          />
          <button disabled={sending || historyLoading || clearingHistory || !draft.trim()} title="Send message">
            <SendHorizonal size={20} />
          </button>
        </form>
      </section>
      {editingAvatar && <AvatarEditor avatar={avatar} initialError={avatarError} onSave={value => { setAvatar(value); setAvatarError(''); }} onClose={() => setEditingAvatar(false)} />}
      {leavesOpen && <dialog ref={leaveDialog} className="leave-dialog" aria-label={user.role==='faculty'?'Faculty tools':'Leave tools'} onCancel={event => { event.preventDefault(); closeLeaves(); }}>
        <div className="leave-dialog-header"><span className="portal-icon"><GraduationCap size={28}/></span><div className="portal-heading"><strong>{user.role==='faculty'?'Faculty Portal':'Student Portal'}</strong><p>Manage your academic activities</p></div><button type="button" className="close-leaves" aria-label="Close leave tools" title="Close leave tools" onClick={closeLeaves}><X size={20} aria-hidden="true" /></button></div>
        <div className="faculty-tool-tabs" aria-label="Manual tools">
          <button type="button" aria-pressed={toolTab==='leave'} onClick={()=>setToolTab('leave')}><CalendarDays size={18} aria-hidden="true"/>Leave</button>
          {user.role==='faculty'&&<button type="button" aria-pressed={toolTab==='attendance'} onClick={()=>setToolTab('attendance')}><CalendarDays size={18} aria-hidden="true"/>Attendance</button>}
          <button type="button" aria-pressed={toolTab==='alerts'} onClick={()=>setToolTab('alerts')}><Bell size={18} aria-hidden="true"/>Alerts</button>
        </div>
        <div className="leave-dialog-content">
          {toolTab==='alerts'?<AlertPanel user={user} refreshKey={alertRevision}/>:user.role==='faculty'&&toolTab==='attendance'?<AttendancePanel refreshKey={attendanceRevision}/>:<LeavePanel user={user} onNotice={setNotice} refreshKey={leaveRevision}/>}
        </div>
      </dialog>}
    </main>
  );
}
