import { useEffect, useRef, useState } from 'react';
import api from '../api/client';
import Avatar, { cropSize } from './Avatar';
import { Camera, Upload, X, Trash2, RotateCcw } from 'lucide-react';

export default function AvatarEditor({ avatar, initialError = '', onSave, onClose }) {
  const [preview, setPreview] = useState(avatar);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  const dialog = useRef(null);
  const drag = useRef(null);
  const frame = useRef(null);
  useEffect(() => { dialog.current.showModal(); }, []);
  function crop(values) { setPreview(p => ({ ...p, crop: { ...p.crop, ...values } })); }
  function position(key, value) {
    const img = frame.current?.querySelector('img');
    if (!img?.naturalWidth) return;
    const size = cropSize(img.naturalWidth / img.naturalHeight, preview.crop.zoom);
    crop({ [key]: value, ...((key === 'x' ? size.width : size.height) <= 1.001 ? { zoom: 1.2 } : {}) });
  }
  function startDrag(e) {
    if (!preview || busy || e.button !== 0) return;
    const img = frame.current.querySelector('img');
    if (!img?.naturalWidth) return;
    const zoom = Math.max(1.2, preview.crop.zoom);
    const size = cropSize(img.naturalWidth / img.naturalHeight, zoom);
    const width = frame.current.getBoundingClientRect().width;
    drag.current = { x: e.clientX, y: e.clientY, crop: { ...preview.crop, zoom }, dx: (size.width - 1) * width, dy: (size.height - 1) * width };
    crop({ zoom });
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function moveDrag(e) {
    const d = drag.current;
    if (!d) return;
    const clamp = n => Math.max(0, Math.min(1, n));
    crop({ x: clamp(d.crop.x - (e.clientX - d.x) / d.dx), y: clamp(d.crop.y - (e.clientY - d.y) / d.dy) });
  }
  async function upload(event) {
    const file = event.target.files[0];
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) { setError('Choose a JPEG, PNG or WebP image up to 5 MB.'); return; }
    const reader = new FileReader();
    reader.onload = () => { setPreview({ src: reader.result, crop: { x: .5, y: .5, zoom: 1.2 } }); setError(''); };
    reader.onerror = () => setError('This file could not be read. Please choose it again.');
    reader.readAsDataURL(file);
  }
  async function save(remove = false) {
    setBusy(true); setError('');
    try {
      let response;
      if (remove) response = await api.delete('/auth/me/avatar');
      else {
        const body = new FormData();
        body.append('image', await (await fetch(preview.src)).blob(), 'profile');
        body.append('crop', JSON.stringify(preview.crop));
        response = await api.put('/auth/me/avatar', body);
      }
      onSave(response.data.avatar); onClose();
    } catch (e) { setError(e.response?.data?.error || 'Could not save profile photo.'); }
    finally { setBusy(false); }
  }
  return <dialog ref={dialog} className="avatar-dialog" aria-labelledby="avatar-title" onCancel={e => { e.preventDefault(); if (!busy) onClose(); }}>
    <header className="avatar-editor-header"><span className="avatar-editor-icon"><Camera size={20} /></span><div><h2 id="avatar-title">Profile photo</h2><p>A little more you.</p></div><button className="avatar-close" aria-label="Close photo editor" onClick={onClose} disabled={busy}><X size={20} /></button></header>
    <div className="avatar-preview-area"><div ref={frame} className="crop-preview" onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }}><Avatar avatar={preview} /></div><p>{preview ? 'Drag your photo to frame it just right' : 'Add a photo to personalize your account'}</p></div>
    <div className="avatar-upload-row"><label className="upload-label"><Upload size={16} />{preview ? 'Replace photo' : 'Upload photo'}<input aria-label="Upload profile photo" type="file" accept="image/jpeg,image/png,image/webp" onChange={upload} disabled={busy} /></label><span>JPG, PNG or WebP · Up to 5 MB</span></div>
    {preview && <div className="crop-controls">{[['zoom', 'Zoom', 1, 3], ['x', 'Horizontal position', 0, 1], ['y', 'Vertical position', 0, 1]].map(([key, label, min, max]) => <label key={key}><span>{label}</span><input type="range" min={min} max={max} step="0.01" value={preview.crop[key]} onChange={e => key === 'zoom' ? crop({ zoom: Number(e.target.value) }) : position(key, Number(e.target.value))} disabled={busy} /></label>)}<button className="avatar-reset" onClick={() => crop({ x: .5, y: .5, zoom: 1.2 })} disabled={busy}><RotateCcw size={13} />Reset adjustments</button></div>}
    {error && <p role="alert">{error}</p>}
    <div className="dialog-actions">{avatar && <button className="avatar-remove" aria-label="Remove photo" title="Remove photo" onClick={() => save(true)} disabled={busy}><Trash2 size={17} /></button>}<button onClick={onClose} disabled={busy}>Cancel</button><button className="primary-button" onClick={() => save()} disabled={busy || !preview}>{busy ? 'Saving…' : 'Save'}</button></div>
  </dialog>;
}
