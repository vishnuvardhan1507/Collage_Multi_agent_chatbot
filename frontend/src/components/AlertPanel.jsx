import { useEffect, useState } from 'react';
import { Bell, CalendarDays, Plus, Trash2, Pencil, Users, Utensils, X } from 'lucide-react';
import api from '../api/client.js';
import './AlertPanel.css';

const scheduleDefaults={class_reminder:'07:00',menu_match:'07:00',attendance_missing:'18:00'};
const meals=['breakfast','lunch','evening_snacks','dinner'];
function formatNotificationTime(value){
  if(typeof value!=='string'||!value.trim())return 'Time unavailable';
  const timestamp=value.trim().replace(' ','T');
  const date=new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp)?timestamp:`${timestamp}Z`);
  if(Number.isNaN(date.getTime()))return 'Time unavailable';
  return `${date.toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:true})} IST`;
}
function initial(role){return {kind:role==='student'?'attendance_student':'classroom_free',config:role==='student'?{threshold:80,course_ids:null,match_mode:'any'}:{classroom_ids:[],booking_date:'',start_time:'',end_time:'',match_mode:'any'}};}
function selected(config,singular,plural){return config[plural]??(config[singular]?[config[singular]]:null);}
function configFor(kind,form){
  const timing=scheduleDefaults[kind]&&form.reminder_time!==undefined?{reminder_time:form.reminder_time}:{};
  if(kind==='class_reminder'||kind==='attendance_missing')return {course_ids:selected(form,'course_id','course_ids'),match_mode:form.match_mode||'any',...timing};
  if(kind==='attendance_student')return {threshold:Number(form.threshold),course_ids:selected(form,'course_id','course_ids'),match_mode:form.match_mode||'any'};
  if(kind==='attendance_faculty')return {threshold:Number(form.threshold),course_ids:selected(form,'course_id','course_ids'),student_ids:selected(form,'student_id','student_ids'),match_mode:form.match_mode||'any'};
  if(kind==='classroom_free')return {classroom_ids:selected(form,'classroom_id','classroom_ids')||[],booking_date:form.booking_date,start_time:form.start_time,end_time:form.end_time,match_mode:form.match_mode||'any'};
  if(kind==='menu_match')return {...timing,plans:form.plans,meals:form.meals,dishes:[...(form.dishes||(form.dish?[form.dish]:[])),...(form.dishDraft?.trim()?[form.dishDraft.trim()]:[])],match_mode:form.match_mode||'any'};
  if(kind==='leave_student')return {statuses:form.statuses};
  return {};
}
function Form({role,value,onChange,courses,reminderCourses,classrooms,editing}){
  const kind=value.kind;
  const setConfig=(key,val)=>onChange(v=>({...v,config:{...v.config,[key]:val}}));
  const kinds=role==='student'?[['attendance_student','Attendance'],['menu_match','Mess menu'],['leave_student','Leave decisions'],['class_reminder','Class reminders']]:[['classroom_free','Classroom availability'],['attendance_faculty','Student attendance'],['menu_match','Mess menu'],['leave_faculty','New department leave requests'],['attendance_missing','Unmarked attendance'],['class_reminder','Class reminders']];
  const choose=e=>{
    const next=e.target.value;const base=initial(role);base.kind=next;
    base.config=(next==='class_reminder'||next==='attendance_missing')?{course_ids:null,match_mode:'any'}:next==='attendance_student'?{threshold:80,course_ids:null,match_mode:'any'}:next==='attendance_faculty'?{threshold:80,course_ids:null,student_ids:null,match_mode:'any'}:next==='classroom_free'?{classroom_ids:[],booking_date:'',start_time:'',end_time:'',match_mode:'any'}:next==='menu_match'?{plans:['veg','non_veg'],meals:['lunch'],dishes:[],dishDraft:'',match_mode:'any'}:next==='leave_student'?{statuses:['approved','rejected']}:{};
    onChange(base);
  };
  const selectedDishes=value.config.dishes||(value.config.dish?[value.config.dish]:[]);
  const courseIds=selected(value.config,'course_id','course_ids');
  const studentIds=selected(value.config,'student_id','student_ids');
  const roomIds=selected(value.config,'classroom_id','classroom_ids')||[];
  const roster=courses.filter(c=>!courseIds||courseIds.includes(c.course_id)).flatMap(c=>c.students||[]).filter((s,i,a)=>a.findIndex(x=>x.student_id===s.student_id)===i);
  const check=(key,item)=>setConfig(key,(value.config[key]||[]).includes(item)?value.config[key].filter(x=>x!==item):[...(value.config[key]||[]),item]);
  const toggle=(singular,plural,current,item)=>{const next=current?.includes(item)?current.filter(x=>x!==item):[...(current||[]),item];onChange(v=>({...v,config:{...v.config,[singular]:undefined,[plural]:next.length?next:null}}));};
  const grouping=(label='When should it match?')=><fieldset><legend>{label}</legend><label><input type="radio" name={`${kind}-match`} checked={(value.config.match_mode||'any')==='any'} onChange={()=>setConfig('match_mode','any')}/>Any selected target (OR)</label><label><input type="radio" name={`${kind}-match`} checked={value.config.match_mode==='all'} onChange={()=>setConfig('match_mode','all')}/>All selected targets (AND)</label></fieldset>;
  function addDish(){const dish=value.config.dishDraft?.trim();if(!dish||selectedDishes.length>=10)return;const dishes=selectedDishes.some(d=>d.toLowerCase()===dish.toLowerCase())?selectedDishes:[...selectedDishes,dish];onChange(v=>({...v,config:{...v.config,dish:undefined,dishes,dishDraft:''}}));}
  const courseChoices=(items,current,onToggle,legend)=><fieldset><legend>{legend}</legend>{items.map(course=><label key={course.course_id}><input type="checkbox" checked={(current||[]).includes(course.course_id)} onChange={()=>onToggle(course.course_id)}/>{course.course_id} — {course.course_name}</label>)}</fieldset>;
  return <div className="alert-form"><label>Alert type<select value={kind} onChange={choose}>{kinds.map(([id,label])=><option value={id} key={id}>{label}</option>)}</select></label>
    {(kind==='attendance_student'||kind==='attendance_faculty')&&<><label>Notify below (%)<input type="number" min="1" max="99" step="any" required value={value.config.threshold??80} onChange={e=>setConfig('threshold',e.target.value)}/></label>{courseChoices(kind==='attendance_student'?reminderCourses:courses,courseIds,id=>{const next=courseIds?.includes(id)?courseIds.filter(x=>x!==id):[...(courseIds||[]),id];onChange(v=>({...v,config:{...v.config,course_id:undefined,course_ids:next.length?next:null,student_id:undefined,student_ids:null}}));},'Courses (none selected means all)')}{kind==='attendance_faculty'&&<fieldset><legend>Students (none selected means any enrolled student)</legend>{roster.map(student=><label key={student.student_id}><input type="checkbox" checked={(studentIds||[]).includes(student.student_id)} onChange={()=>toggle('student_id','student_ids',studentIds,student.student_id)}/>{student.student_id} — {student.name}</label>)}</fieldset>}{grouping()}</>}
    {(kind==='class_reminder'||kind==='attendance_missing')&&<>{courseChoices(reminderCourses,courseIds,id=>toggle('course_id','course_ids',courseIds,id),'Courses (none selected means all)')}{grouping()}</>}
    {kind==='classroom_free'&&<><fieldset><legend>Classrooms</legend>{classrooms.map(room=><label key={room}><input type="checkbox" checked={roomIds.includes(room)} onChange={()=>toggle('classroom_id','classroom_ids',roomIds,room)}/>{room}</label>)}</fieldset><label>Date<input type="date" value={value.config.booking_date||''} onChange={e=>setConfig('booking_date',e.target.value)}/></label><label>Start time<input type="time" value={value.config.start_time||''} onChange={e=>setConfig('start_time',e.target.value)}/></label><label>End time<input type="time" value={value.config.end_time||''} onChange={e=>setConfig('end_time',e.target.value)}/></label>{grouping()}</>}
    {kind==='menu_match'&&<><div className="dish-selector"><label>Dish<input maxLength={80} value={value.config.dishDraft||''} onChange={e=>setConfig('dishDraft',e.target.value)} placeholder="e.g. chicken" onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();addDish();}}}/></label><button type="button" className="dish-add" disabled={!value.config.dishDraft?.trim()||selectedDishes.length>=10} onClick={addDish}><Plus size={16}/>Add dish</button><p>{(value.config.match_mode||'any')==='all'?'Notify only when every selected dish appears in the same meal.':'Notify when any selected dish appears.'}</p><div className="dish-chips">{selectedDishes.map(dish=><span key={dish.toLowerCase()}>{dish}<button type="button" aria-label={`Remove dish ${dish}`} onClick={()=>onChange(v=>({...v,config:{...v.config,dish:undefined,dishes:selectedDishes.filter(d=>d!==dish)}}))}><X size={14}/></button></span>)}</div></div><fieldset><legend>Dish match</legend><label><input type="radio" name="dish-match" checked={(value.config.match_mode||'any')==='any'} onChange={()=>setConfig('match_mode','any')}/>Any dish (OR)</label><label><input type="radio" name="dish-match" checked={value.config.match_mode==='all'} onChange={()=>setConfig('match_mode','all')}/>All dishes in the same meal (AND)</label></fieldset><fieldset><legend>Mess plans</legend>{['veg','non_veg'].map(x=><label key={x}><input type="checkbox" checked={(value.config.plans||[]).includes(x)} onChange={()=>check('plans',x)}/>{x==='non_veg'?'Non-veg':'Veg'}</label>)}</fieldset><fieldset><legend>Meals</legend>{meals.map(x=><label key={x}><input type="checkbox" checked={(value.config.meals||[]).includes(x)} onChange={()=>check('meals',x)}/>{x.replace('_',' ')}</label>)}</fieldset></>}
    {editing&&scheduleDefaults[kind]&&<label>Reminder time (IST)<input type="time" required value={value.config.reminder_time??scheduleDefaults[kind]} onChange={e=>setConfig('reminder_time',e.target.value)}/></label>}
    {kind==='leave_student'&&<fieldset><legend>Notify for</legend>{['approved','rejected'].map(x=><label key={x}><input type="checkbox" checked={(value.config.statuses||[]).includes(x)} onChange={()=>check('statuses',x)}/>{x}</label>)}</fieldset>}
    {kind==='leave_faculty'&&<p>You will receive emails for new pending leave requests in your department.</p>}
  </div>;
}

export default function AlertPanel({user,onClose,refreshKey=0}){
  const [data,setData]=useState({alerts:[],recent:[]});const [form,setForm]=useState(initial(user.role));const [editing,setEditing]=useState(null);const [open,setOpen]=useState(false);const [busy,setBusy]=useState(false);const [notice,setNotice]=useState('');const [confirm,setConfirm]=useState(null);
  const load=()=>api.get('/alerts').then(({data})=>setData(data)).catch(e=>setNotice(e.response?.data?.error||'Could not load alerts.'));
  useEffect(()=>{load();},[refreshKey]);
  async function save(e){e.preventDefault();setBusy(true);setNotice('');try{const payload={kind:form.kind,config:configFor(form.kind,form.config)};if(editing)await api.patch(`/alerts/${editing}`,payload);else await api.post('/alerts',payload);setForm(initial(user.role));setEditing(null);setOpen(false);await load();}catch(e){setNotice(e.response?.data?.error||'Could not save alert.');}finally{setBusy(false);}}
  async function remove(id){setBusy(true);try{await api.delete(`/alerts/${id}`);setConfirm(null);await load();}catch(e){setNotice(e.response?.data?.error||'Could not delete alert.');}finally{setBusy(false);}}
  function edit(rule){setForm({kind:rule.kind,config:rule.config});setEditing(rule.id);setOpen(true);}
  return <section className="alert-panel" aria-label="Email alerts"><header><div><h2>Your alerts</h2></div>{onClose&&<button className="alert-close" onClick={onClose} aria-label="Close alerts"><X size={18}/></button>}</header><p className="alert-help">Matching alerts are delivered to your registered email.</p>
    <button type="button" className="alert-add" onClick={()=>{setForm(initial(user.role));setEditing(null);setOpen(v=>!v);}}><Plus size={17}/>Add alert</button>
    {open&&<form onSubmit={save}><Form editing={editing!==null} role={user.role} value={form} onChange={setForm} courses={data.assigned_courses||[]} reminderCourses={data.reminder_courses||[]} classrooms={data.classrooms||[]}/><div className="alert-actions"><button type="button" onClick={()=>{setOpen(false);setEditing(null);}}>Cancel</button><button disabled={busy}>{busy?'Saving…':editing?'Save changes':'Create alert'}</button></div></form>}
    {notice&&<p className="alert-error" role="alert">{notice}</p>}
    <h3>Active alerts</h3>{data.alerts.length?<div className="alert-list">{data.alerts.map(rule=><article key={rule.id} className={!rule.active?'alert-inactive':''}><div className={`alert-kind-icon ${rule.kind==='menu_match'?'menu-icon':''}`} aria-hidden="true">{rule.kind==='menu_match'?<Utensils size={22}/>:rule.kind.startsWith('attendance')?<Users size={22}/>:<CalendarDays size={22}/>}</div><div className="alert-rule-details"><strong>{rule.summary}</strong><small>Alert #{rule.id}</small>{rule.kind==='menu_match'&&<div className="alert-meal-chips">{(rule.config.meals||[]).map(meal=><span key={meal}>{meal.replace('_',' ')}</span>)}</div>}</div><div className="alert-rule-actions"><small className={`alert-state ${rule.active?'is-active':''}`}>{rule.active?'Active':rule.state}</small><span><button aria-label={`Edit alert ${rule.id}`} onClick={()=>edit(rule)}><Pencil size={17}/></button><button aria-label={`Delete alert ${rule.id}`} onClick={()=>setConfirm(rule.id)}><Trash2 size={17}/></button></span></div>{confirm===rule.id&&<div className="alert-confirm" role="group" aria-label={`Confirm deletion of alert ${rule.id}`}>Delete alert #{rule.id}?<button onClick={()=>remove(rule.id)} disabled={busy}>Delete</button><button onClick={()=>setConfirm(null)} disabled={busy}>Cancel</button></div>}</article>)}</div>:<p className="alert-empty">No active alerts.</p>}
    <h3>Recent notifications</h3>{data.recent.length?<div className="alert-recent">{data.recent.map(item=><article key={item.id}><strong>{item.subject}</strong><span>{item.system_generated?'Automatic subject update - ':''}{item.email_status||'queued'} · {formatNotificationTime(item.created_at)}</span></article>)}</div>:<div className="notification-empty"><Bell size={32} aria-hidden="true"/><strong>No notifications yet</strong><p>You’ll receive email notifications when your alerts match.</p></div>}
  </section>;
}
