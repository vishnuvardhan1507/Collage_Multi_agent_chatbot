import { useEffect, useRef, useState } from 'react';
import { LayoutDashboard, Users, BookOpen, CalendarDays, Building2, FileText, Activity, LogOut, Plus, Pencil, RefreshCw, X, ShieldCheck, Search, Bell, GraduationCap, ArrowRight, Trash2, BarChart3 } from 'lucide-react';
import api from '../api/client';
import { useAuth } from '../context/AuthContext';
import './admin.css';

const titles={accounts:'Accounts',courses:'Courses & assignments',prerequisites:'Prerequisites',course_syllabi:'Syllabus outlines',course_timetable:'Timetable',classrooms:'Classrooms',hostel_rooms:'Hostel rooms',mess_plans:'Mess plans',mess_menus:'Weekly menus',transport_services:'Transport service',bus_routes:'Bus routes',bus_stops:'Ordered stops',bus_locality_aliases:'Locality aliases'};
const groups=[['overview','Overview',LayoutDashboard],['accounts','Accounts',Users],['courses','Academics',BookOpen],['course_timetable','Timetable',CalendarDays],['classrooms','Facilities',Building2],['documents','Policies',FileText],['monitoring','Monitoring',Activity]];
const academic=['courses','prerequisites','course_syllabi'];
const facilities=['classrooms','hostel_rooms','mess_plans','mess_menus','transport_services','bus_routes','bus_stops','bus_locality_aliases'];
const numeric=new Set(['semester','credits','capacity','has_ac','sharing_capacity','occupied_beds','monthly_room_fee','monthly_mess_fee','monthly_round_trip_fare','projector_count','computer_count','arrive_before_minutes','stop_sequence','planning_radius_km','approximate_radius_km','weekday','is_active','timetable_id']);
const nullable=new Set(['faculty_id','advisor_faculty_id','breakfast','lunch','evening_snacks','dinner','has_ac','projector_count','computer_count','stop_sequence']);
const labels={username:'Full name',is_active:'Account status',has_ac:'Air conditioning',aliases_json:'Aliases',units_json:'Syllabus units',prerequisite_ids:'Prerequisite courses',effective_from:'Effective from (IST)',effective_to:'Effective through (IST)'};
const descriptions={overview:'A quick snapshot of your institution.',accounts:'Manage student and faculty accounts.',courses:'Manage courses and teaching assignments.',prerequisites:'Maintain course prerequisites.',course_syllabi:'Maintain syllabus outlines and learning outcomes.',course_timetable:'Create and manage course schedules.',classrooms:'Manage classrooms and their resources.',hostel_rooms:'Manage hostel capacity, occupancy and room fees.',mess_plans:'Manage mess plans and monthly charges.',mess_menus:'Maintain day-wise meals for each mess plan.',transport_services:'Manage campus transport settings.',bus_routes:'Manage routes and monthly fares.',bus_stops:'Maintain boarding points and timings.',bus_locality_aliases:'Connect locality names to their routes and stops.',documents:'Review and publish institutional policies.',monitoring:'Monitor delivery, worker health and administrative activity.'};
const addLabels={accounts:'Account',courses:'Course',course_timetable:'Schedule',classrooms:'Classroom',hostel_rooms:'Room',mess_plans:'Mess plan',mess_menus:'Daily menu',bus_routes:'Route',bus_stops:'Stop',bus_locality_aliases:'Alias',transport_services:'Service',course_syllabi:'Syllabus',prerequisites:'Prerequisites'};
const label=k=>labels[k]||k.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
const display=value=>value===null?'Unknown':typeof value==='object'?JSON.stringify(value):String(value);
const weekdays=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];

function Dialog({title,onClose,children,busy}) {
  const ref=useRef(null);
  useEffect(()=>{const focus=document.activeElement;const dialog=ref.current;dialog.showModal();return()=>{dialog.close();requestAnimationFrame(()=>focus?.focus());};},[]);
  return <dialog ref={ref} className="admin-dialog" onCancel={e=>{e.preventDefault();if(!busy)onClose();}}>
    <header><h2>{title}</h2><button type="button" className="admin-icon" aria-label="Close editor" disabled={busy} onClick={onClose}><X size={20}/></button></header>{children}
  </dialog>;
}

function Preview({content}) {
  // React text nodes escape HTML. Raw HTML and executable links are never rendered.
  return <article className="admin-preview">{content.split('\n').map((line,i)=>{
    if(line.startsWith('### '))return <h4 key={i}>{line.slice(4)}</h4>;
    if(line.startsWith('## '))return <h3 key={i}>{line.slice(3)}</h3>;
    if(line.startsWith('# '))return <h2 key={i}>{line.slice(2)}</h2>;
    return <p key={i}>{line||'\u00a0'}</p>;
  })}</article>;
}

export default function Admin(){
  const {user,logout}=useAuth();
  const [page,setPage]=useState('overview'),[catalog,setCatalog]=useState(null),[rows,setRows]=useState([]),[summary,setSummary]=useState({}),[monitor,setMonitor]=useState(null);
  const [search,setSearch]=useState(''),[roleFilter,setRoleFilter]=useState('all'),[error,setError]=useState(''),[notice,setNotice]=useState(''),[loading,setLoading]=useState(false),[busy,setBusy]=useState(false);
  const [editor,setEditor]=useState(null),[original,setOriginal]=useState(null),[reason,setReason]=useState(''),[impact,setImpact]=useState(null),[confirm,setConfirm]=useState(null),[editError,setEditError]=useState('');
  const [courses,setCourses]=useState([]),[faculty,setFaculty]=useState([]),[docName,setDocName]=useState('handbook.md'),[doc,setDoc]=useState(null),[preview,setPreview]=useState(false);
  const requestVersion=useRef(0), searchIntent=useRef('');
  const [filters,setFilters]=useState({}),[globalSearch,setGlobalSearch]=useState(''),[searchResults,setSearchResults]=useState([]),[searchBusy,setSearchBusy]=useState(false),[searchError,setSearchError]=useState('');
  const [deletion,setDeletion]=useState(null),[deleteReason,setDeleteReason]=useState(''),[deleteError,setDeleteError]=useState('');
  function go(target,query=''){searchIntent.current=query;setPage(target);setGlobalSearch('');if(target===page)setSearch(query);}
  const err=ex=>ex.response?.data?.error||'The request could not be completed. Please try again.';
  async function load(){
    const version=++requestVersion.current;setLoading(true);setError('');
    try{
      if(page==='overview'){const r=await api.get('/admin/overview');if(version===requestVersion.current)setSummary(r.data);}
      else if(page==='monitoring'){const r=await api.get('/admin/monitoring');if(version===requestVersion.current)setMonitor(r.data);}
      else if(page==='documents'){const r=await api.get('/admin/documents/'+docName);if(version===requestVersion.current){setDoc(r.data);setReason('');}}
      else {const r=await api.get('/admin/records/'+page);if(version===requestVersion.current)setRows(r.data.records);}
    }catch(ex){if(version===requestVersion.current)setError(err(ex));}finally{if(version===requestVersion.current)setLoading(false);}
  }
  useEffect(()=>{api.get('/admin/catalog').then(r=>setCatalog(r.data)).catch(ex=>setError(err(ex)));},[]);
  useEffect(()=>{load();setSearch(searchIntent.current);searchIntent.current='';setFilters({});setEditor(null);setNotice('');},[page,docName]);
  useEffect(()=>{
    let active=true;setSearchResults([]);setSearchError('');
    if(page!=='overview'||globalSearch.trim().length<2){setSearchBusy(false);return;}
    setSearchBusy(true);const timer=setTimeout(()=>api.get('/admin/search',{params:{q:globalSearch.trim()}}).then(r=>{if(active)setSearchResults(r.data.results);}).catch(()=>{if(active)setSearchError('Search is unavailable. Please try again.');}).finally(()=>{if(active)setSearchBusy(false);}),250);
    return()=>{active=false;clearTimeout(timer);};
  },[globalSearch,page]);
  async function openEditor(row){
    setEditError('');setReason('');setImpact(null);setOriginal(row||null);
    const base=row?{...row}:{_revision:0};
    if(page==='accounts'&&!row)Object.assign(base,{role:'student',department:catalog.departments[0],semester:1,is_active:1});
    if(page==='course_syllabi'){
      base.aliases_json=row?JSON.parse(row.aliases_json):[];base.units_json=row?JSON.parse(row.units_json):[{unit:1,title:'',topics:[],learning_outcome:''}];
    }
    if(page==='prerequisites'&&!base.prerequisite_ids)base.prerequisite_ids=[];
    setEditor(base);
    try{
      const [c,f]=await Promise.all([api.get('/admin/records/courses'),api.get('/admin/records/accounts')]);setCourses(c.data.records);setFaculty(f.data.records.filter(x=>x.role==='faculty'&&x.is_active));
      if(page==='courses'&&row){const r=await api.get('/admin/courses/'+encodeURIComponent(row.course_id)+'/impact');setImpact(r.data);}
    }catch(ex){setEditError(err(ex));}
  }
  function update(key,value){setEditor(prev=>({...prev,[key]:value}));}
  function fields(){
    if(page==='accounts')return ['user_id','username','role','department','email',...(editor.role==='student'?['semester','advisor_faculty_id']:['designation','office_hours']),'is_active'];
    const spec=catalog?.resources[page];return spec?[...spec.keys,...spec.fields]:[];
  }
  function input(key){
    const value=editor[key]??'';
    const frozen=!!original&&((catalog?.resources[page]?.keys||['user_id']).includes(key)||['role','department'].includes(key));
    const select=opts=><select id={'admin-'+key} value={value} disabled={frozen||busy} onChange={e=>update(key,numeric.has(key)&&e.target.value!==''?Number(e.target.value):e.target.value)}>{opts.map(([v,l])=><option key={v} value={v}>{l}</option>)}</select>;
    if(key==='role')return select([['student','Student'],['faculty','Faculty']]);
    if(key==='department')return select(catalog.departments.map(x=>[x,x]));
    if(key==='is_active')return select([[1,'Active'],[0,'Inactive']]);
    if(key==='has_ac')return select([['','Unknown'],[1,'Yes'],[0,'No']]);
    if(key==='weekday')return select([['','Select day'],...weekdays.map((v,i)=>[i,v])]);
    if(['faculty_id','advisor_faculty_id'].includes(key))return select([['','Unassigned'],...faculty.filter(f=>!editor.department||f.department===editor.department).map(f=>[f.user_id,`${f.user_id} · ${f.username}`])]);
    if(key==='course_id'&&page!=='courses')return select([['','Select course'],...courses.map(c=>[c.course_id,`${c.course_id} · ${c.course_name}`])]);
    if(key==='prerequisite_ids')return <select id={'admin-'+key} multiple value={value||[]} disabled={busy} onChange={e=>update(key,Array.from(e.target.selectedOptions,x=>x.value))}>{courses.filter(c=>c.course_id!==editor.course_id).map(c=><option key={c.course_id} value={c.course_id}>{c.course_id} · {c.course_name}</option>)}</select>;
    if(key==='aliases_json')return <input id={'admin-'+key} value={Array.isArray(value)?value.join(', '):value} onChange={e=>update(key,e.target.value)} placeholder="Comma-separated course aliases"/>;
    if(key==='units_json')return <div className="admin-units">{value.map((u,i)=><fieldset key={i}><legend>Unit {i+1}</legend>{['title','topics','learning_outcome'].map(f=><label key={f}>{label(f)}<textarea value={f==='topics'&&Array.isArray(u[f])?u[f].join(', '):u[f]} onChange={e=>update(key,value.map((unit,j)=>i===j?{...unit,[f]:e.target.value}:unit))}/></label>)}<button type="button" disabled={value.length===1} onClick={()=>update(key,value.filter((_,j)=>j!==i).map((u,j)=>({...u,unit:j+1})))}>Remove unit</button></fieldset>)}<button type="button" onClick={()=>update(key,[...value,{unit:value.length+1,title:'',topics:[],learning_outcome:''}])}>Add unit</button></div>;
    const type=numeric.has(key)?'number':key==='email'?'email':key.startsWith('effective_')?'date':/(start_time|end_time|pickup|dropoff|campus_arrival|campus_departure)/.test(key)?'time':'text';
    return <input id={'admin-'+key} type={type} step={['planning_radius_km','approximate_radius_km'].includes(key)?'0.1':undefined} value={value} disabled={frozen||busy} onChange={e=>update(key,type==='number'&&e.target.value!==''?Number(e.target.value):e.target.value)} placeholder={nullable.has(key)?'Unknown / not recorded':''}/>;
  }
  async function save(confirmed=false){
    setBusy(true);setEditError('');
    try{
      const data={...editor,reason,confirmed};
      if(page==='accounts'){
        if(data.role==='student'){delete data.designation;delete data.office_hours;}else{delete data.semester;delete data.advisor_faculty_id;}
      }
      for(const k of Object.keys(data))if(data[k]===''&&nullable.has(k))data[k]=null;
      if(page==='course_timetable'&&!original)data.timetable_id=null;
      if(page==='course_syllabi'){data.aliases_json=JSON.stringify(Array.isArray(data.aliases_json)?data.aliases_json:data.aliases_json.split(',').map(x=>x.trim()).filter(Boolean));data.units_json=JSON.stringify(data.units_json.map(u=>({...u,topics:Array.isArray(u.topics)?u.topics:u.topics.split(/[,\n]/).map(x=>x.trim()).filter(Boolean)})));}
      const r=await api.post('/admin/records/'+page,data);setNotice(r.data.message+(r.data.setup_email?' '+r.data.setup_email:''));setEditor(null);setConfirm(null);await load();
    }catch(ex){setEditError(err(ex));setConfirm(null);}finally{setBusy(false);}
  }
  function submit(e){
    e.preventDefault();
    if(original&&((page==='accounts'&&editor.is_active!==original.is_active)||(page==='courses'&&editor.faculty_id!==original.faculty_id)||page==='course_timetable')){
      setConfirm('Confirm this change? '+(page==='accounts'?'Deactivation blocks sign-in and cancels pending actions.':page==='courses'?'Teaching access changes immediately for this course.':'A future period replaces this schedule; historical sessions remain.'));
    }else save();
  }
  async function publish(){
    setBusy(true);setError('');
    try{const r=await api.post('/admin/documents/'+docName,{content:doc.content,revision:doc.revision,reason,confirmed:true});setNotice(`${r.data.message} Search status: ${r.data.index_status.replaceAll('_',' ')}.`);setConfirm(null);await load();}catch(ex){setError(err(ex));setConfirm(null);}finally{setBusy(false);}
  }
  async function resend(uid){setBusy(true);try{const r=await api.post('/admin/accounts/'+encodeURIComponent(uid)+'/setup-email');setNotice(r.data.message);}catch(ex){setError(err(ex));}finally{setBusy(false);}}
  const visible=rows.filter(row=>(roleFilter==='all'||page!=='accounts'||row.role===roleFilter)&&JSON.stringify(row).toLowerCase().includes(search.toLowerCase())&&Object.entries(filters).every(([key,value])=>!value||(key==='period'?`${row.effective_from}|${row.effective_to}`===value:String(row[key])===value)));
  const columns=rows.length?(page==='accounts'?['user_id','username','role','department','is_active']:[...(catalog?.resources[page]?.keys||[]),...(catalog?.resources[page]?.fields||[])]).filter(k=>!k.endsWith('_json')):[];
  function filterSelect(key,title,options){return <label>{title}<select aria-label={title} value={filters[key]||''} onChange={e=>setFilters(prev=>({...prev,[key]:e.target.value}))}><option value="">All {title.toLowerCase()}</option>{options.map(([value,name])=><option key={value} value={value}>{name}</option>)}</select></label>;}
  function requestDelete(row){setDeletion(row);setDeleteReason('');setDeleteError('');}
  async function removeRecord(){
    setBusy(true);setDeleteError('');
    try{let r;if(page==='documents'){r=await api.delete('/admin/documents/'+docName,{data:{revision:deletion.revision,reason:deleteReason,confirmed:true}});}else{const keys=page==='accounts'?['user_id']:catalog.resources[page].keys;const data=Object.fromEntries(keys.map(k=>[k,deletion[k]]));r=await api.delete('/admin/records/'+page,{data:{...data,_revision:deletion._revision,reason:deleteReason,confirmed:true}});}setDeletion(null);setEditor(null);setNotice(r.data.message);await load();}
    catch(ex){setDeleteError(err(ex));}finally{setBusy(false);}
  }
  const canDelete=page==='accounts'||Boolean(catalog?.resources[page]?.deletable);
  const deleteLabel=page==='accounts'?'Deactivate account':page==='documents'?'Delete document':page==='prerequisites'?'Remove prerequisite links':'Delete record';
  const deletionName=deletion?(page==='documents'?docName:(page==='accounts'?['user_id']:catalog.resources[page].keys).map(k=>deletion[k]).join(' / ')):'';
  return <div className="admin-shell"><aside className="admin-nav"><div className="admin-brand"><ShieldCheck/><div>NexusIQ<span>Administration</span></div></div><nav aria-label="Administration">{groups.map(([key,title,Icon])=><button key={key} className={(page===key||(key==='courses'&&academic.includes(page))||(key==='classrooms'&&facilities.includes(page)))?'selected':''} onClick={()=>go(key)}><Icon size={19}/>{title}</button>)}</nav><div className="admin-profile"><strong>{user.name}</strong><small>{user.user_id}</small><button onClick={logout}><LogOut size={17}/>Log out</button></div></aside>
    <main className="admin-main"><header className="admin-page-header"><div className="admin-page-title"><p>INSTITUTION MANAGEMENT</p><h1>{titles[page]||label(page)}</h1><span>{descriptions[page]}</span></div><div className="admin-header-tools">
      {(page==='overview'||catalog?.resources[page]||page==='accounts')&&<div className="admin-header-search"><Search size={18}/><input aria-label={page==='overview'?'Search institution':'Search records'} placeholder={page==='overview'?'Search students, courses, rooms...':`Search ${titles[page]?.toLowerCase()||'records'}...`} value={page==='overview'?globalSearch:search} onChange={e=>page==='overview'?setGlobalSearch(e.target.value):setSearch(e.target.value)} onKeyDown={e=>{if(e.key==='Escape')setGlobalSearch('');}}/>
      {page==='overview'&&globalSearch.trim().length>=2&&<div className="admin-search-results" aria-label="Institution search results">{searchBusy?<p>Searching...</p>:searchError?<p role="alert">{searchError}</p>:searchResults.length?searchResults.map(r=><button key={r.resource+r.id} onClick={()=>go(r.resource,r.id)}><strong>{r.title}</strong><span>{r.id} &middot; {r.detail}</span></button>):<p>No matching accounts, courses or classrooms.</p>}</div>}</div>}
      <button className="admin-icon" onClick={load} disabled={loading||busy} aria-label="Refresh current page"><RefreshCw size={18}/></button><button className="admin-icon admin-bell" aria-label={`Open monitoring${summary.attention_count?`, ${summary.attention_count} failed emails`:''}`} onClick={()=>go('monitoring')}><Bell size={20}/>{summary.attention_count>0&&<span className="admin-alert-dot"/>}</button><span className="admin-avatar" aria-label={`Signed in as ${user.name}`} title={user.name}>{user.name.split(/\s+/).map(n=>n[0]).slice(0,2).join('').toUpperCase()}</span>
    </div></header>
      {error&&<p role="alert" className="admin-error">{error}</p>}{notice&&<p role="status" className="admin-notice">{notice}</p>}
      {(academic.includes(page)||facilities.includes(page))&&<nav className="admin-subnav" aria-label="Management sections">{(academic.includes(page)?academic:facilities).map(x=><button key={x} className={x===page?'selected':''} onClick={()=>go(x)}>{titles[x]}</button>)}</nav>}
      {loading?<p role="status" className="admin-empty">Loading records...</p>:page==='overview'?<>
        <div className="admin-stats admin-overview-stats">{[['classrooms','Classrooms',Building2,'blue'],['courses','Courses',BookOpen,'purple'],['faculty','Faculty',Users,'green'],['students','Students',GraduationCap,'orange']].map(([key,title,Icon,color])=><article key={key}><span className={`admin-stat-icon ${color}`}><Icon size={29}/></span><div><span>{title}</span><strong>{summary[key]??'?'}</strong></div></article>)}</div>
        <div className="admin-overview-grid"><section className="admin-card admin-schedule-card"><div className="admin-section-heading"><h2><CalendarDays/>Today's Schedule</h2><button className="admin-text-button" onClick={()=>go('course_timetable')}>View timetable <ArrowRight size={16}/></button></div><p className="admin-date-caption">{summary.as_of?new Date(summary.as_of).toLocaleDateString('en-IN',{timeZone:'Asia/Kolkata',weekday:'long',day:'numeric',month:'long',year:'numeric'}):''}</p><div className="admin-schedule-list">{summary.schedule?.length?summary.schedule.map(row=><div className="admin-schedule-row" key={row.timetable_id}><span className="admin-time-chip">{row.start_time} &ndash; {row.end_time}</span><div><strong>{row.course_id}</strong><small>{row.course_name}</small></div><span className="admin-schedule-faculty">{row.faculty_name||'Faculty unassigned'}</span></div>):<p className="admin-empty">No recorded sessions for today.</p>}</div></section>
        <section className="admin-card"><div className="admin-section-heading"><h2><BarChart3/>Classroom bookings</h2></div><div className="admin-utilization"><div className="admin-donut" style={{'--usage':`${summary.utilization?.percentage??0}%`}} role="img" aria-label={`${summary.utilization?.booked??0} of ${summary.classrooms??0} classrooms booked now`}><div><strong>{summary.utilization?.percentage==null?'?':`${summary.utilization.percentage}%`}</strong><span>Booked now</span></div></div><div className="admin-chart-key"><p><i/>Booked ({summary.utilization?.booked??0})</p><p><i/>Unbooked ({summary.utilization?.unbooked??0})</p></div></div><p className="admin-hint">Based on recorded bookings at {summary.as_of?new Date(summary.as_of).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit'}):'?'} IST. Physical occupancy is not tracked.</p></section></div>
        <div className="admin-secondary-counts"><span>Hostel rooms <strong>{summary.hostel_rooms??'?'}</strong></span><span>Bus routes <strong>{summary.bus_routes??'?'}</strong></span><span>Inactive accounts <strong>{summary.inactive_accounts??'?'}</strong></span></div>
      </>:page==='monitoring'?<>{monitor&&<><section className="admin-card"><h2>Worker health</h2><p>Workers run separately from the web server. A stale heartbeat means the process may be stopped or busy.</p>{monitor.workers.length?monitor.workers.map(w=><p key={w.name}><strong>{w.name}</strong> ? {Date.now()-new Date(w.last_seen).getTime()>180000?'Stale':'Recently active'} ? Last seen {new Date(w.last_seen).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})} IST</p>):<p>No worker heartbeats recorded yet.</p>}</section><section className="admin-card"><h2>Email delivery</h2><div className="admin-stats">{monitor.email_counts.map(r=><article key={r.status}><span>{r.status}</span><strong>{r.count}</strong></article>)}</div><h3>Research jobs</h3>{monitor.research_jobs.map(r=><p key={r.status}>{r.status}: {r.count}</p>)}<h3>Collection checkpoints</h3><DataTable rows={monitor.collection_status}/><h3>Recent delivery status</h3><DataTable rows={monitor.recent_email_status}/></section><section className="admin-card"><h2>Audit trail</h2><DataTable rows={monitor.audit}/></section></>}</>:page==='documents'?<section className="admin-card"><label>Document<select value={docName} onChange={e=>setDocName(e.target.value)}>{catalog?.documents.map(n=><option key={n}>{n}</option>)}</select></label>{doc&&<>{doc.exists===false&&<p className="admin-notice">This document has been removed. You can publish new content to recreate it.</p>}<div className="admin-toolbar"><button onClick={()=>setPreview(!preview)}>{preview?'Edit document':'Preview document'}</button><span>HTML is displayed as text for safety.</span></div>{preview?<Preview content={doc.content}/>:<textarea className="admin-document" aria-label="Policy Markdown" value={doc.content} onChange={e=>setDoc({...doc,content:e.target.value})}/>}<label>Publication reason<input value={reason} onChange={e=>setReason(e.target.value)} maxLength={500}/></label><button className="admin-primary" disabled={!reason.trim()||busy} onClick={()=>setConfirm('Publish this document for student and faculty answers?')}>Publish</button> <button className="admin-danger" disabled={busy||doc.exists===false} onClick={()=>requestDelete({revision:doc.revision})}><Trash2 size={16}/>Delete document</button><h3>Publication history</h3><DataTable rows={doc.history}/></>}</section>:<section className="admin-card admin-records-card"><div className="admin-toolbar admin-records-toolbar"><div className="admin-filters">
        {page==='accounts'&&<label>Account type<select aria-label="Filter account type" value={roleFilter} onChange={e=>setRoleFilter(e.target.value)}><option value="all">All accounts</option><option value="student">Students</option><option value="faculty">Faculty</option></select></label>}
        {page==='classrooms'&&<>{filterSelect('building','Buildings',[...new Set(rows.map(r=>r.building))].filter(Boolean).sort().map(x=>[x,x]))}{filterSelect('has_ac','Air conditioning',[['1','AC'],['0','Non-AC'],['null','Unknown']])}</>}
        {page==='course_timetable'&&<>{filterSelect('period','Effective periods',[...new Set(rows.map(r=>`${r.effective_from}|${r.effective_to}`))].sort().map(x=>[x,x.split('|').join(' to ')]))}{filterSelect('course_id','Courses',[...new Set(rows.map(r=>r.course_id))].sort().map(x=>[x,x]))}{filterSelect('weekday','Weekdays',weekdays.map((day,i)=>[String(i),day]))}</>}
        {!['accounts','classrooms','course_timetable'].includes(page)&&<span className="admin-record-count">{visible.length} {visible.length===1?'record':'records'}</span>}
        </div><button className="admin-primary" disabled={!catalog||busy} onClick={()=>openEditor(null)}><Plus size={17}/>Add {addLabels[page]||'record'}</button></div>
        <div className="admin-table-wrap"><table><thead><tr><th scope="col" className="admin-serial">S.No.</th>{columns.map(k=><th key={k}>{k==='has_ac'?'AC':label(k)}</th>)}<th>Actions</th></tr></thead><tbody>{visible.map((row,i)=><tr key={i}><td className="admin-serial">{i+1}</td>{columns.map(k=><td key={k}>{k==='weekday'?weekdays[row[k]]:k==='is_active'?<span className={`admin-badge ${row[k]?'positive':'neutral'}`}>{row[k]?'Active':'Inactive'}</span>:k==='has_ac'?<span className={`admin-badge ${row[k]===null?'neutral':row[k]?'positive':'negative'}`}>{row[k]===null?'Unknown':row[k]?'Yes':'No'}</span>:display(row[k])}</td>)}<td><div className="admin-row-actions"><button className="admin-icon" aria-label={`Edit ${row[columns[0]]}`} onClick={()=>openEditor(row)}><Pencil size={16}/></button>{canDelete&&<button className="admin-icon admin-danger" disabled={busy||(page==='accounts'&&!row.is_active)} aria-label={`${page==='accounts'?'Deactivate':'Delete'} ${row[columns[0]]}`} onClick={()=>requestDelete(row)}><Trash2 size={16}/></button>}{page==='accounts'&&row.is_active===1&&<button disabled={busy} onClick={()=>resend(row.user_id)}>Setup email</button>}</div></td></tr>)}</tbody></table>{!visible.length&&<p className="admin-empty">No records match this selection.</p>}</div><p className="admin-table-caption">{visible.length} of {rows.length} records{page==='course_timetable'?' - Schedule changes start tomorrow or later.':''}</p></section>}
    </main>
    {editor&&<Dialog title={(original?'Edit ':'Add ')+(titles[page]||page)} onClose={()=>{setEditor(null);setConfirm(null);}} busy={busy}><form onSubmit={submit}><div className="admin-editor-body">{editError&&<p role="alert" className="admin-error">{editError}</p>}{impact&&<aside className="admin-impact"><strong>Assignment impact</strong><p>{impact.enrolled_students} enrolled students; {impact.weekly_sessions.length} current/future session definitions.</p><p>{impact.notice}</p></aside>}<div className="admin-field-grid">{fields().map(k=>k==='timetable_id'&&!original?null:<div key={k} className={k==='units_json'?'wide':''}><label htmlFor={'admin-'+k}>{label(k)}</label>{input(k)}</div>)}</div><label>Reason for change<input required maxLength={500} value={reason} onChange={e=>setReason(e.target.value)}/></label></div><footer>{original&&canDelete&&<button type="button" className="admin-danger admin-delete-edit" disabled={busy} onClick={()=>requestDelete(original)}><Trash2 size={16}/>{deleteLabel}</button>}<button type="button" disabled={busy} onClick={()=>setEditor(null)}>Cancel</button><button className="admin-primary" disabled={busy||!reason.trim()}>{busy?'Saving…':'Save changes'}</button></footer></form></Dialog>}
    {deletion&&<Dialog title={deleteLabel} busy={busy} onClose={()=>setDeletion(null)}><div className="admin-editor-body"><p>{page==='accounts'?`Deactivate ${deletionName}? Sign-in and pending actions will be blocked; academic records and history will be retained.`:page==='documents'?`Delete ${deletionName}? It will no longer be used for chatbot answers. Its revision history will be retained.`:page==='prerequisites'?`Remove all prerequisite links for ${deletionName}? The course itself will remain.`:`Delete ${deletionName}? This cannot be undone. Records with bookings, attendance or other dependencies cannot be deleted.`}</p>{deleteError&&<p role="alert" className="admin-error">{deleteError}</p>}<label>Reason for deletion<input autoFocus maxLength={500} value={deleteReason} onChange={e=>setDeleteReason(e.target.value)}/></label></div><footer><button disabled={busy} onClick={()=>setDeletion(null)}>Cancel</button><button className="admin-danger admin-danger-solid" disabled={busy||!deleteReason.trim()} onClick={removeRecord}>{busy?'Processing...':deleteLabel}</button></footer></Dialog>}
    {confirm&&<Dialog title="Confirm change" busy={busy} onClose={()=>setConfirm(null)}><div className="admin-editor-body"><p>{confirm}</p></div><footer><button disabled={busy} onClick={()=>setConfirm(null)}>Cancel</button><button className="admin-primary" disabled={busy} onClick={()=>page==='documents'?publish():save(true)}>{busy?'Saving…':'Confirm'}</button></footer></Dialog>}
  </div>;
}

function DataTable({rows}){if(!rows?.length)return <p className="admin-empty">No activity recorded.</p>;const columns=Object.keys(rows[0]);return <div className="admin-table-wrap"><table><thead><tr><th scope="col" className="admin-serial">S.No.</th>{columns.map(k=><th key={k}>{label(k)}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}><td className="admin-serial">{i+1}</td>{columns.map(k=><td key={k}>{display(r[k])}</td>)}</tr>)}</tbody></table></div>;}
