import { Check, RefreshCw, Send, X } from "lucide-react";
import { useEffect, useState } from "react";
import api from "../api/client.js";


export default function LeavePanel({ user, onNotice }) {
  const [leaves, setLeaves] = useState([]);
  const [loading, setLoading] = useState(false);
  const [form, setForm] = useState({ from_date: "", to_date: "", reason: "" });

  async function loadLeaves() {
    setLoading(true);
    try {
      const status = user.role === "faculty" ? "?status=pending" : "";
      const response = await api.get(`/leaves${status}`);
      setLeaves(response.data.leaves || []);
    } catch (error) {
      onNotice({ type: "error", text: error.response?.data?.error || "Could not load leave requests." });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadLeaves();
  }, [user.role]);

  async function submitLeave(event) {
    event.preventDefault();
    if (!form.from_date || !form.to_date || !form.reason.trim()) return;
    setLoading(true);
    try {
      await api.post("/leaves", form);
      setForm({ from_date: "", to_date: "", reason: "" });
      onNotice({ type: "success", text: "Leave request submitted as pending." });
      await loadLeaves();
    } catch (error) {
      onNotice({ type: "error", text: error.response?.data?.error || "Could not submit leave request." });
    } finally {
      setLoading(false);
    }
  }

  async function reviewLeave(leaveId, status) {
    setLoading(true);
    try {
      await api.patch(`/leaves/${leaveId}/review`, { status });
      onNotice({ type: "success", text: `Leave request ${status}.` });
      await loadLeaves();
    } catch (error) {
      onNotice({ type: "error", text: error.response?.data?.error || "Could not review leave request." });
    } finally {
      setLoading(false);
    }
  }

  const title = user.role === "faculty" ? "Pending Leave Review" : "Leave Request";

  return (
    <section className="leave-panel">
      <div className="leave-panel-header">
        <div>
          <span className="eyebrow">{user.role === "faculty" ? "Faculty action" : "Student action"}</span>
          <h2>{title}</h2>
        </div>
        <button className="icon-action" onClick={loadLeaves} disabled={loading} title="Refresh leave requests">
          <RefreshCw size={17} />
        </button>
      </div>

      {user.role === "student" && (
        <form className="leave-form" onSubmit={submitLeave}>
          <input
            type="date"
            value={form.from_date}
            onChange={(event) => setForm((current) => ({ ...current, from_date: event.target.value }))}
            title="From date"
          />
          <input
            type="date"
            value={form.to_date}
            onChange={(event) => setForm((current) => ({ ...current, to_date: event.target.value }))}
            title="To date"
          />
          <input
            value={form.reason}
            onChange={(event) => setForm((current) => ({ ...current, reason: event.target.value }))}
            placeholder="Reason"
          />
          <button disabled={loading || !form.from_date || !form.to_date || !form.reason.trim()} title="Submit leave">
            <Send size={16} />
            <span>Submit</span>
          </button>
        </form>
      )}

      <div className="leave-list">
        {leaves.length === 0 && (
          <p className="leave-empty">
            {user.role === "faculty" ? "No pending leave requests in your scope." : "No leave requests yet."}
          </p>
        )}
        {leaves.slice(0, 4).map((leave) => (
          <article className="leave-row" key={leave.leave_id}>
            <div className="leave-row-main">
              <strong>#{leave.leave_id} {leave.student_name || leave.student_id}</strong>
              <span>{leave.from_date} to {leave.to_date}</span>
              <p>{leave.reason}</p>
            </div>
            <span className={`leave-status status-${leave.status}`}>{leave.status}</span>
            {user.role === "faculty" && leave.status === "pending" && (
              <div className="leave-actions">
                <button onClick={() => reviewLeave(leave.leave_id, "approved")} disabled={loading} title="Approve leave">
                  <Check size={16} />
                </button>
                <button onClick={() => reviewLeave(leave.leave_id, "rejected")} disabled={loading} title="Reject leave">
                  <X size={16} />
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
