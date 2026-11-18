import { UserRound } from 'lucide-react';
import { useState } from 'react';
export function cropSize(ratio, zoom) {
  return { width: Math.max(1, ratio) * zoom, height: Math.max(1, 1 / ratio) * zoom };
}
export default function Avatar({ avatar }) {
  const [image, setImage] = useState(null);
  const crop = avatar?.crop || { x: .5, y: .5, zoom: 1 };
  const ready = Boolean(avatar && image?.src === avatar.src);
  const size = cropSize(ready ? image.ratio : 1, crop.zoom);
  return <span className="avatar-image">{avatar ? <img src={avatar.src} alt="Profile" draggable="false" onLoad={e => setImage({ src: avatar.src, ratio: e.currentTarget.naturalWidth / e.currentTarget.naturalHeight })} style={{ position: 'absolute', maxWidth: 'none', width: `${size.width * 100}%`, height: `${size.height * 100}%`, left: `${(1 - size.width) * crop.x * 100}%`, top: `${(1 - size.height) * crop.y * 100}%`, opacity: ready ? 1 : 0 }} /> : <UserRound aria-hidden="true" />}</span>;
}
