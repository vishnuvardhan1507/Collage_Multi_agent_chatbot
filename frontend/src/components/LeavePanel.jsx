import { CalendarDays, Check, RefreshCw, Send, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import api from "../api/client.js";


export default function LeavePanel({ user, onNotice, refreshKey }) {
  const [rejectTarget, setRejectTarget] = useState(null);
  const [rejectionReason, setRejectionReason] = useState('');
  const [rejectionError, setRejectionError] = useState('');
  const rejectDialog = useRef(null);
  useEffect(() => { if (rejectTarget) rejectDialog.current.showModal(); }, [rejectTarget]);
  function cancelReject() {
    rejectDialog.current?.close();
    setRejectTarget(null); setRejectionReason(''); setRejectionError('');
  }
  const [deleteTarget, setDeleteTarget] = useState(null);
  const deleteDialog = useRef(null);
  useEffect(() => {
    if (deleteTarget && !deleteDialog.current.open) deleteDialog.current.showModal();
  }, [deleteTarget]);
  function cancelDelete() {
    deleteDialog.current?.close();
    setDeleteTarget(null);
  }
  const [feedback, setFeedback] = useState(null);
  function notify(message) { setFeedback(message); }
  const [leaves, setLeaves] = useState([]);
  const [reviewSection, setReviewSection] = useState('pending');
  const [loading, setLoading] = useState(false);
  const [form, setForm] = useState({ from_date: "", to_date: "", reason: "" });

  async function loadLeaves() {
    setLoading(true);
    try {
      const response = await api.get('/leaves');
      setLeaves(response.data.leaves || []);
    } catch (error) {
      notify({ type: "error", text: error.response?.data?.error || "Could not load leave requests." });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadLeaves();
  }, [user.role, user.user_id, refreshKey]);

  async function submitLeave(event) {
    event.preventDefault();
    if (!form.from_date || !form.to_date || !form.reason.trim()) return;
    setLoading(true);
    try {
      await api.post("/leaves", form);
      setForm({ from_date: "", to_date: "", reason: "" });
      notify({ type: "success", text: "Leave request submitted as pending." });
      await loadLeaves();
    } catch (error) {
      notify({ type: "error", text: error.response?.data?.error || "Could not submit leave request." });
    } finally {
      setLoading(false);
    }
  }

  async function reviewLeave(leaveId, status) {
    if (loading || (status === "rejected" && !rejectionReason.trim())) return;
    setLoading(true);
    try {
      await api.patch(`/leaves/${leaveId}/review`, { status, ...(status === "rejected" ? { rejection_reason: rejectionReason.trim() } : {}) });
      if (status === "rejected") cancelReject();
      notify({ type: "success", text: `Leave request ${status}.` });
      await loadLeaves();
    } catch (error) {
      if (status === "rejected") setRejectionError(error.response?.data?.error || "Could not reject leave request.");
      else notify({ type: "error", text: error.response?.data?.error || "Could not review leave request." });
    } finally {
      setLoading(false);
    }
  }

  async function deleteLeave(leave) {
    if (loading) return;
    setLoading(true);
    try {
      await api.delete(`/leaves/${leave.leave_id}`);
      notify({ type: 'success', text: 'Leave request deleted.' });
      await loadLeaves();
    } catch (error) {
      notify({ type: 'error', text: error.response?.data?.error || 'Could not delete leave request.' });
    } finally { setLoading(false); cancelDelete(); }
  }

  const title = user.role === "faculty" ? "Leave review" : "Leave requests";
  const sections = [['pending', 'Pending'], ['approved', 'Accepted'], ['rejected', 'Rejected']];
  const sectionLeaves = (status) => leaves.filter(leave => leave.status === status &&
    (status === 'pending' || leave.reviewed_by === user.user_id));
  const visibleLeaves = user.role === 'faculty' ? sectionLeaves(reviewSection) : leaves;

  return (
    <section className="leave-panel">
      <div className="leave-panel-header">
        <div>
          <h2>{title}</h2>
          <p>{user.role==='faculty'?'Review leave requests from students in your department.':'Apply for leave and view your previous requests.'}</p>
        </div>
      </div>

      {user.role === "student" && (
        <form className="leave-form" onSubmit={submitLeave}>
          <h3>Request leave</h3>
          <label>Start date<input
            type="date"
            value={form.from_date}
            onChange={(event) => setForm((current) => ({ ...current, from_date: event.target.value }))}
            title="From date"
          /></label>
          <label>End date<input
            type="date"
            value={form.to_date}
            onChange={(event) => setForm((current) => ({ ...current, to_date: event.target.value }))}
            title="To date"
          /></label>
          <label>Reason<input
            value={form.reason}
            onChange={(event) => setForm((current) => ({ ...current, reason: event.target.value }))}
            placeholder="Reason"
          /></label>
          <button disabled={loading || !form.from_date || !form.to_date || !form.reason.trim()} title="Submit leave">
            <Send size={16} />
            <span>Submit request</span>
          </button>
        </form>
      )}

      <div className="manual-section-heading"><h3>{user.role==='student'?'Request history':'Department requests'}</h3><button className="manual-refresh" onClick={loadLeaves} disabled={loading} title="Refresh leave requests"><RefreshCw size={17}/>Refresh</button></div>
      {user.role === 'faculty' && <div className="leave-review-sections" role="group" aria-label="Leave review sections">
        {sections.map(([status, label]) => <button type="button" key={status} aria-pressed={reviewSection === status} onClick={() => setReviewSection(status)}>
          {label} ({sectionLeaves(status).length})
        </button>)}
      </div>}
      {user.role === 'faculty' && reviewSection !== 'pending' && <p>Requests you have {reviewSection === 'approved' ? 'accepted' : 'rejected'} in your department.</p>}
      {feedback && <p role={feedback.type === 'error' ? 'alert' : 'status'}>{feedback.text}</p>}
      <div className="leave-list">
        {visibleLeaves.length === 0 && (
          <p className="leave-empty">
            {loading ? 'Loading leave requests…' : user.role === "faculty" ? (reviewSection === 'pending' ? "No pending leave requests in your scope." : `No requests ${reviewSection === 'approved' ? 'accepted' : 'rejected'} by you.`) : "No leave requests yet."}
          </p>
        )}
        {visibleLeaves.map((leave) => (
          <article className="leave-row" key={leave.leave_id}>
            <span className="leave-record-icon" aria-hidden="true"><CalendarDays size={21}/></span>
            <div className="leave-row-main">
              <strong>#{leave.leave_id} {leave.student_name || leave.student_id}</strong>
              <span>{leave.from_date} to {leave.to_date}</span>
              <p>{leave.reason}</p>
              {leave.status === 'rejected' && <p className="rejection-reason"><strong>Rejection reason:</strong> {leave.rejection_reason || 'No reason recorded for this earlier request.'}</p>}
            </div>
            <span className={`leave-status status-${leave.status}`}>{leave.status}</span>
            {user.role === "student" && <button className="icon-action" onClick={() => setDeleteTarget(leave)} disabled={loading || !leave.can_delete} title={leave.can_delete ? `Delete leave #${leave.leave_id}` : 'Past leave requests cannot be deleted'} aria-label={`Delete leave #${leave.leave_id}`}><Trash2 size={17} /></button>}
            {user.role === "faculty" && leave.status === "pending" && (
              <div className="leave-actions">
                <button onClick={() => reviewLeave(leave.leave_id, "approved")} disabled={loading} title="Approve leave">
                  <Check size={16} />
                </button>
                <button onClick={() => { setRejectionError(''); setRejectionReason(''); setRejectTarget(leave); }} disabled={loading} title="Reject leave">
                  <X size={16} />
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
      {rejectTarget && <dialog ref={rejectDialog} aria-labelledby="reject-leave-title" onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!loading) cancelReject(); }}>
        <form onSubmit={event => { event.preventDefault(); reviewLeave(rejectTarget.leave_id, 'rejected'); }}>
          <h2 id="reject-leave-title">Reject leave request #{rejectTarget.leave_id}</h2>
          <label>Rejection reason<textarea autoFocus required maxLength={1000} value={rejectionReason} onChange={event => setRejectionReason(event.target.value)} disabled={loading} style={{ width: '100%', minHeight: '100px', marginTop: '12px' }} /></label>
          {rejectionError && <p role="alert">{rejectionError}</p>}
          <div className="dialog-actions"><button type="button" disabled={loading} onClick={cancelReject}>Cancel</button><button type="submit" className="danger-button" disabled={loading || !rejectionReason.trim()}>{loading ? 'Rejecting?' : 'Reject request'}</button></div>
        </form>
      </dialog>}
      {deleteTarget && <dialog ref={deleteDialog} className="leave-delete-dialog" aria-labelledby="leave-delete-title" aria-describedby="leave-delete-description" onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!loading) cancelDelete(); }}>
        <h2 id="leave-delete-title">Delete leave request?</h2>
        <p id="leave-delete-description">Do you want to delete leave request #{deleteTarget.leave_id} ({deleteTarget.from_date} to {deleteTarget.to_date})?</p>
        <div className="dialog-actions">
          <button type="button" autoFocus disabled={loading} onClick={cancelDelete}>No</button>
          <button type="button" className="danger-button" disabled={loading} onClick={() => deleteLeave(deleteTarget)}>{loading ? 'Deleting?' : 'Yes'}</button>
        </div>
      </dialog>}
    </section>
  );
}
