"""Private, account-owned profile images; never exposed to agent databases."""
import base64
import io
import json
import math
import sqlite3
import warnings

from flask import current_app, g, jsonify, request
from PIL import Image, ImageOps, UnidentifiedImageError

from auth.access import authenticated


def register_avatar_routes(bp):
    @bp.route('/me/avatar', methods=['GET', 'PUT', 'DELETE'])
    @authenticated
    def avatar():
        user_id = g.current_user['user_id']
        if request.method == 'PUT':
            if request.content_length and request.content_length > 5 * 1024 * 1024 + 65536:
                return jsonify(error='Image must be no larger than 5 MB.'), 413
            upload = request.files.get('image')
            try:
                crop = json.loads(request.form.get('crop', '{}'))
                crop = {key: float(crop.get(key, default)) for key, default in [('x', .5), ('y', .5), ('zoom', 1)]}
                if not all(math.isfinite(v) for v in crop.values()) or not (0 <= crop['x'] <= 1 and 0 <= crop['y'] <= 1 and 1 <= crop['zoom'] <= 3):
                    raise ValueError()
                if not upload:
                    raise ValueError()
                raw = upload.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    return jsonify(error='Image must be no larger than 5 MB.'), 413
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as source:
                        if source.format not in {'JPEG', 'PNG', 'WEBP'} or source.width * source.height > 16_000_000:
                            raise ValueError()
                        source.load()
                        normalized = ImageOps.exif_transpose(source).convert('RGB')
                        normalized.thumbnail((2048, 2048))
                        clean = Image.new('RGB', normalized.size)
                        clean.paste(normalized)
                        output = io.BytesIO()
                        clean.save(output, format='JPEG', quality=90)
                        image = output.getvalue()
            except (ValueError, TypeError, AttributeError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                return jsonify(error='Choose a valid JPEG, PNG or WebP image up to 16 megapixels and valid crop settings.'), 400
        with sqlite3.connect(current_app.config['DATABASE_PATH']) as conn:
            if request.method == 'DELETE':
                conn.execute('DELETE FROM user_avatars WHERE user_id=?', (user_id,))
                return jsonify(avatar=None)
            if request.method == 'PUT':
                conn.execute('INSERT INTO user_avatars(user_id,image,crop) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET image=excluded.image,crop=excluded.crop', (user_id, image, json.dumps(crop)))
            row = conn.execute('SELECT image,crop FROM user_avatars WHERE user_id=?', (user_id,)).fetchone()
        return jsonify(avatar={'src': 'data:image/jpeg;base64,' + base64.b64encode(row[0]).decode(), 'crop': json.loads(row[1])} if row else None)
