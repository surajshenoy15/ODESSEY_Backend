import io
import re
from secrets import randbelow
from urllib.parse import quote

import qrcode

from app.core.config import settings
from app.core.security import utcnow


def registration_code():
    return f'ODY-{utcnow():%Y%m%d}-{randbelow(1_000_000):06d}'


def safe_filename(value):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip()).strip('-') or 'file'


def qr_public_url(token: str) -> str:
    return f"{settings.PUBLIC_APP_URL.rstrip('/')}/#/check-in?token={quote(token, safe='')}"


def qr_png_bytes(token):
    buffer = io.BytesIO()
    qrcode.make(qr_public_url(token)).save(buffer, format='PNG')
    return buffer.getvalue()


async def audit(db, actor_type, actor_id, action, entity_type, entity_id, reason=None, details=None):
    from app.models.entities import AuditLog
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id, action=action, entity_type=entity_type, entity_id=entity_id, reason=reason, details=details))
    await db.flush()


def ensure_editable(reg):
    if reg.status in {'APPROVED', 'REJECTED', 'CANCELLED'}:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='Registration fields are locked in the current status')
