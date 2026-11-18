import { Camera, ChevronDown, Clock, LogOut, MessageSquarePlus, RefreshCw, Sparkles, Trash2, X } from "lucide-react";
import { useRef, useState } from 'react';
import Avatar from './Avatar';

const studentPrompts = [
  "What is my attendance in Machine Learning?",
  "Show my completed, currently enrolled, and to-be-completed courses",
  "What will my attendance be if I take leave tomorrow, and will I meet the minimum attendance policy?",
  "How many leaves are allowed per semester?",
  "Show available 2-sharing AC hostel rooms",
  "What is Tuesday dinner in the veg mess?",
  "Which bus serves Manikonda and what is its monthly fare?",
  "Show my pending leave requests",
  "Show my results for completed courses",
  "Which courses have prerequisites?",
  "Show my class timetable for tomorrow",
  "Compare room and mess fees for 3-sharing non-AC accommodation",
  "What is the non-veg mess menu for Friday?",
  "What time does the Miyapur bus pick up and drop off students?",
  "Show available 4-sharing hostel rooms",
  "What are the examination eligibility rules?"
];

const facultyPrompts = [
  "Show my assigned classes",
  "Which leave requests are pending for my students?",
  "Check Lab-302 availability tomorrow from 14:00 to 16:00",
  "Find an AC room with a projector and 30 computers tomorrow from 10:00 to 11:00",
  "Show students in my department",
  "Show attendance for students in my department",
  "Find classrooms with at least 80 seats and a projector",
  "What is the leave approval policy?",
  "Show results for completed courses in my department",
  "Show my teaching timetable for tomorrow",
  "Compare Lab-302 and Lab-303 equipment",
  "Which buses serve HITEC City campus?",
  "Show pending course registrations in my department",
  "Find non-AC classrooms with at least 40 seats",
  "What are the examination rules?",
  "Which classrooms are available tomorrow from 14:00 to 16:00?"
];

export default function Sidebar({ user, sessionId, messages = [], onNewSession, onPrompt, onLogout, sessions, onSelectSession, onDeleteSession, onClearHistory, disabled, avatar, onEditAvatar, open, onClose }) {
  const prompts = user.role === "faculty" ? facultyPrompts : studentPrompts;
  const [promptPage, setPromptPage] = useState(0);
  const visiblePrompts = prompts.slice((promptPage % (prompts.length / 4)) * 4, (promptPage % (prompts.length / 4)) * 4 + 4);
  const [deleting, setDeleting] = useState(null);
  const confirmDialog = useRef(null);
  const history = useRef(null);
  const clearDialog = useRef(null);
  const clearButton = useRef(null);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState('');
  function closeClear() {
    if (clearing) return;
    clearDialog.current.close();
    const target = clearButton.current?.disabled ? history.current.querySelector('summary') : clearButton.current;
    target?.focus();
  }
  const title = session => session.messages?.find(m => m.role === 'user')?.content?.slice(0, 70) || 'Empty conversation';
  const currentTitle = messages.find(message => message.role === 'user')?.content || 'New conversation';
  function confirmDelete(session) { setDeleting(session); confirmDialog.current.showModal(); }
  return (
    <aside className={`sidebar ${open ? 'sidebar-open' : ''}`} aria-label="Account and conversations">
      <button className="mobile-sidebar-close icon-button" onClick={onClose} aria-label="Close sidebar"><X /></button>

      <section className="profile">
        <div className="profile-avatar"><Avatar avatar={avatar} /><button className="camera-button" onClick={onEditAvatar} aria-label="Edit profile photo"><Camera size={21} /></button></div>
        <div className="profile-details"><h2 title={user.name}>{user.name}</h2>
        <div className="profile-meta"><span className="role-pill">{user.role}</span>
        <p>{user.user_id}</p></div></div>
      </section>

      <button className="toolbar-button" onClick={onNewSession} title="New session" disabled={disabled}>
        <MessageSquarePlus size={18} />
        <span>New Session</span>
      </button>

      <div className="session-label">
        <span>Current chat</span>
        <strong title={currentTitle}>{currentTitle}</strong>
      </div>

      <details className="history-dropdown" ref={history} onKeyDown={event => { if (event.key === 'Escape') { history.current.open = false; history.current.querySelector('summary').focus(); } }}>
        <summary><Clock size={19} /> Chat History <ChevronDown size={18} /></summary>
        <div className="history-options">
          <button type="button" className="clear-chat-history" ref={clearButton} disabled={disabled || !sessions.length || clearing} onClick={()=>{setClearError('');clearDialog.current.showModal();}}><Trash2 size={16}/>Clear chat history</button>
          {!sessions.length && <p>No saved conversations yet.</p>}
          {[...sessions].reverse().map(session => <div className={`history-option ${session.session_id === sessionId ? 'selected' : ''}`} key={session.session_id}>
            <button disabled={disabled} aria-current={session.session_id === sessionId ? 'true' : undefined} onClick={() => { onSelectSession(session.session_id); history.current.open = false; onClose(); }}><strong>{title(session)}</strong><span>{session.timestamp ? new Date(session.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}</span></button>
            <button disabled={disabled || clearing} aria-label={`Delete conversation: ${title(session)}`} onClick={() => confirmDelete(session)}><Trash2 size={18} /></button>
          </div>)}
        </div>
      </details>

      <div className="prompt-section">
      <details className="prompt-list" open>
        <summary className="prompt-heading">
          <Sparkles size={16} />
          <span>Demo prompts</span>
        </summary>
        {visiblePrompts.map((prompt) => (
          <button key={prompt} onClick={() => onPrompt(prompt)} disabled={disabled}>
            {prompt}
          </button>
        ))}
      </details>
      <button type="button" className="refresh-prompts" aria-label="Refresh demo prompts" title="Refresh demo prompts" onClick={() => setPromptPage(page => page + 1)}><RefreshCw key={promptPage} size={17} className={promptPage ? 'prompt-refresh-spin' : ''} /></button>
      <span className="prompt-refresh-status" role="status">{promptPage ? 'New demo prompts shown.' : ''}</span>
      </div>

      <button className="logout-button" onClick={onLogout} title="Log out" disabled={disabled}>
        <LogOut size={18} />
        <span>Log out</span>
      </button>
      <dialog ref={confirmDialog} className="delete-dialog" aria-labelledby="delete-title">
        <h2 id="delete-title">Delete conversation?</h2><p>“{deleting && title(deleting)}” will be permanently removed.</p>
        <div className="dialog-actions"><button onClick={() => confirmDialog.current.close()}>Cancel</button><button className="danger-button" onClick={async () => { const selected = deleting; confirmDialog.current.close(); await onDeleteSession(selected.session_id); }}>Delete conversation</button></div>
      </dialog>
      <dialog ref={clearDialog} className="delete-dialog" aria-labelledby="clear-history-title" onCancel={event=>{event.preventDefault();closeClear();}}>
        <h2 id="clear-history-title">Clear chat history?</h2>
        <p>Delete all your conversations? This cannot be undone.</p>
        {clearError && <p role="alert">{clearError}</p>}
        <div className="dialog-actions"><button autoFocus disabled={clearing} onClick={closeClear}>Cancel</button><button className="danger-button" disabled={disabled || clearing || !sessions.length} onClick={async()=>{
          setClearing(true);setClearError('');
          const success = await onClearHistory();
          setClearing(false);
          if (success) { clearDialog.current.close(); history.current.querySelector('summary').focus(); }
          else setClearError('Could not clear chat history. Please try again.');
        }}>{clearing?'Clearing…':'Clear history'}</button></div>
      </dialog>
    </aside>
  );
}
