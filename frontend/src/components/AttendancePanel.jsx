import { useEffect, useRef, useState } from 'react';
import { BookOpen, RefreshCw, Save, Users } from 'lucide-react';
import api from '../api/client.js';
import './AttendancePanel.css';

export default function AttendancePanel({ refreshKey = 0 }) {
  const [day,setDay]=useState('');
  const [data,setData]=useState(null);
  const [selected,setSelected]=useState('');
  const [roster,setRoster]=useState(null);
  const [marks,setMarks]=useState({});
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);
  const [notice,setNotice]=useState('');
  const [reload,setReload]=useState(0);
  const lastRefresh=useRef(refreshKey);
  const dirty=!!roster?.students.some(s=>marks[s.student_id]!==s.present);
  useEffect(()=>{
    if(lastRefresh.current===refreshKey)return;
    lastRefresh.current=refreshKey;
    if(dirty||busy)setNotice('Attendance changed in chat. Your unsaved edits are preserved. Reload before saving.');
    else setReload(v=>v+1);
  },[refreshKey]);
  useEffect(()=>{
    let active=true;
    setLoading(true);setRoster(null);setSelected('');setNotice('');
    api.get('/attendance/sessions',{params:day?{date:day}:{}}).then(({data:result})=>{
      if(active){setData(result);if(!day)setDay(result.date);}
    }).catch(e=>{if(active){setData(null);setNotice(e.response?.data?.error||'Could not load sessions.');}})
      .finally(()=>{if(active)setLoading(false);});
    return()=>{active=false;};
  },[day,reload]);
  useEffect(()=>{
    if(!selected)return;
    let active=true;setLoading(true);setRoster(null);setNotice('');
    api.get(`/attendance/sessions/${selected}/${day}`).then(({data:result})=>{
      if(active){setRoster(result);setMarks(Object.fromEntries(result.students.map(s=>[s.student_id,s.present])));}
    }).catch(e=>{if(active)setNotice(e.response?.data?.error||'Could not load roster.');})
      .finally(()=>{if(active)setLoading(false);});
    return()=>{active=false;};
  },[selected,day]);
  async function save(){
    setBusy(true);setNotice('');
    try{
      const {data:result}=await api.put(`/attendance/sessions/${selected}/${day}`,{revision:roster.revision,marks});
      setRoster(r=>({...r,revision:result.revision,students:r.students.map(s=>({...s,present:marks[s.student_id]}))}));setNotice(result.message);
    }catch(e){setNotice(e.response?.data?.error||'Could not save attendance.');}
    finally{setBusy(false);}
  }
  const complete=roster?.students.length>0&&roster.students.every(s=>marks[s.student_id]===0||marks[s.student_id]===1);
  const session=data?.sessions.find(s=>String(s.timetable_id)===String(selected));
  const presentCount=roster?.students.filter(s=>marks[s.student_id]===1).length||0;
  const absentCount=roster?.students.filter(s=>marks[s.student_id]===0).length||0;
  return <section className="attendance-panel" aria-label="Attendance marking">
    <h3>Class attendance</h3><p>Mark your enrolled students for each class. Saturdays and Sundays are holidays.</p>
    <div className="attendance-controls"><label>Date<input aria-label="Attendance date" type="date" min="2026-10-19" max={data?.today} value={day} disabled={busy} onChange={e=>setDay(e.target.value)}/></label>
      <button type="button" className="attendance-refresh" aria-label="Reload attendance" title="Reload attendance" disabled={busy||loading} onClick={()=>setReload(v=>v+1)}><RefreshCw size={20} aria-hidden="true" /></button></div>
    {data?.holiday?<p role="status">Holiday—attendance not required.</p>:<label>Class session<select aria-label="Class session" disabled={busy||loading||!data} value={selected} onChange={e=>setSelected(e.target.value)}>
      <option value="">Select a class</option>{data?.sessions.map(s=><option key={s.timetable_id} value={s.timetable_id} disabled={!s.can_mark}>{s.start_time}–{s.end_time} · {s.course_id} {s.course_name}{!s.can_mark?' (not started)':''}{s.revision?' (recorded)':''}</option>)}</select></label>}
    {!loading&&data&&!data.holiday&&!data.sessions.length&&<p>No scheduled teaching sessions on this date.</p>}
    {loading&&<p role="status">Loading attendance…</p>}
    {roster&&<><div className="attendance-session-card"><span className="portal-icon"><BookOpen size={24}/></span><div><strong>{roster.course_id} {session?.course_name}</strong><p>{session?.start_time}–{session?.end_time} · {roster.students.length} students</p></div><span className="session-badge">Class session</span></div>
      <div className="attendance-roster">{roster.students.map(s=><fieldset key={s.student_id} disabled={busy}><legend className="sr-only">{s.name} · {s.student_id}</legend><div className="roster-student"><span className="student-initial" aria-hidden="true">{s.name?.charAt(0)}</span><div><strong>{s.name}</strong><small>{s.student_id}</small></div></div><div className="attendance-choices">
        {[['Present',1],['Absent',0]].map(([label,value])=><label key={label} className={marks[s.student_id]===value?'is-selected':''}><input type="radio" name={`attendance-${s.student_id}`} checked={marks[s.student_id]===value} onChange={()=>setMarks(m=>({...m,[s.student_id]:value}))}/>{label}</label>)}</div></fieldset>)}</div>
      <div className="attendance-totals"><span><Users size={18}/>{roster.students.length} students in this session</span><span className="count-present">{presentCount} present</span><span className="count-absent">{absentCount} absent</span>{!complete&&<span>{roster.students.length-presentCount-absentCount} unmarked</span>}</div>
      <div className="attendance-save-row"><button type="button" className="attendance-save" disabled={busy||!complete} onClick={save}><Save size={18}/>{busy?'Saving…':'Save attendance'}</button></div></>}
    {notice&&<p role="status">{notice}</p>}
  </section>;
}
