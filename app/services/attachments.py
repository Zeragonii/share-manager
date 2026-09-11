from __future__ import annotations
import hashlib
import os
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import UploadFile
from sqlalchemy.orm import Session
from ..models import AuditLog, SupportTicket, SupportTicketAttachment, SupportTicketMessage

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
CUSTOMER_TICKET_ATTACHMENT_LIMIT = 5
ALLOWED_EXTENSIONS = {'.jpg','.jpeg','.png','.webp','.gif','.pdf','.txt','.log'}
MIME_BY_KIND = {
    'jpeg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif','pdf':'application/pdf','text':'text/plain'
}


def attachment_root(path: str) -> Path:
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / '_pending').mkdir(parents=True, exist_ok=True)
    return root


def _detect_kind(head: bytes, sample: bytes, suffix: str) -> tuple[str,str]:
    if head.startswith(b'\xff\xd8\xff'): return 'jpeg', 'image/jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'): return 'png', 'image/png'
    if head.startswith((b'GIF87a', b'GIF89a')): return 'gif', 'image/gif'
    if head.startswith(b'%PDF-'): return 'pdf', 'application/pdf'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP': return 'webp', 'image/webp'
    if suffix in {'.txt','.log'}:
        try:
            sample.decode('utf-8')
            return 'text', 'text/plain'
        except UnicodeDecodeError:
            pass
    raise ValueError('Unsupported attachment type. Allowed: JPG, PNG, WebP, GIF, PDF, TXT and LOG.')


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or '').suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError('Unsupported attachment type. Allowed: JPG, PNG, WebP, GIF, PDF, TXT and LOG.')
    return '.jpg' if suffix == '.jpeg' else suffix


async def save_pending_upload(db: Session, upload: UploadFile, *, storage_dir: str, uploader_type: str,
                              uploader_customer_id: int | None, ticket: SupportTicket | None = None,
                              draft_token: str | None = None, actor: str = 'system') -> SupportTicketAttachment:
    original = os.path.basename((upload.filename or 'attachment').strip())[:255] or 'attachment'
    suffix = _safe_suffix(original)
    if uploader_type == 'customer':
        if ticket:
            count = db.query(SupportTicketAttachment).filter(
                SupportTicketAttachment.ticket_id == ticket.id,
                SupportTicketAttachment.uploader_type == 'customer',
            ).count()
        else:
            if not draft_token:
                raise ValueError('Missing upload draft token')
            count = db.query(SupportTicketAttachment).filter(
                SupportTicketAttachment.uploader_customer_id == uploader_customer_id,
                SupportTicketAttachment.draft_token == draft_token,
                SupportTicketAttachment.uploader_type == 'customer',
            ).count()
        if count >= CUSTOMER_TICKET_ATTACHMENT_LIMIT:
            raise ValueError('This ticket already has the maximum of 5 customer attachments.')

    root = attachment_root(storage_dir)
    stored_name = f'{uuid.uuid4().hex}{suffix}'
    pending = root / '_pending' / stored_name
    digest = hashlib.sha256(); size = 0; first = b''; sample = b''
    try:
        with pending.open('wb') as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise ValueError('Attachment is too large. Maximum size is 15 MB per file.')
                if len(sample) < 65536: sample += chunk[:65536-len(sample)]
                if not first: first = chunk[:32]
                digest.update(chunk); out.write(chunk)
        if size <= 0: raise ValueError('Attachment is empty.')
        kind, mime = _detect_kind(first, sample, suffix)
        # Do not let a misleading extension turn an executable/blob into an inline response.
        expected = {'.jpg':'jpeg','.png':'png','.webp':'webp','.gif':'gif','.pdf':'pdf','.txt':'text','.log':'text'}[suffix]
        if kind != expected:
            raise ValueError('The file contents do not match the filename/type.')
        row = SupportTicketAttachment(
            ticket_id=ticket.id if ticket else None, message_id=None, draft_token=draft_token,
            uploader_type=uploader_type, uploader_customer_id=uploader_customer_id,
            original_filename=original, storage_filename=stored_name, mime_type=mime,
            size_bytes=size, sha256=digest.hexdigest(), created_at=datetime.utcnow(), attached_at=None,
        )
        db.add(row); db.commit(); db.refresh(row)
        db.add(AuditLog(actor=actor, action='ticket.attachment.uploaded', target_type='support_ticket',
                        target_id=ticket.reference if ticket else 'draft', detail=f'{original} ({size} bytes)'))
        db.commit()
        return row
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def attach_pending(db: Session, *, ticket: SupportTicket, message: SupportTicketMessage,
                   attachment_ids: list[int], storage_dir: str, uploader_type: str,
                   uploader_customer_id: int | None = None, draft_token: str | None = None) -> list[SupportTicketAttachment]:
    ids = list(dict.fromkeys(int(x) for x in attachment_ids if x))
    if not ids: return []
    q = db.query(SupportTicketAttachment).filter(SupportTicketAttachment.id.in_(ids), SupportTicketAttachment.message_id.is_(None), SupportTicketAttachment.uploader_type == uploader_type)
    if uploader_type == 'customer':
        q = q.filter(SupportTicketAttachment.uploader_customer_id == uploader_customer_id)
    if draft_token:
        q = q.filter(SupportTicketAttachment.ticket_id.is_(None), SupportTicketAttachment.draft_token == draft_token)
    else:
        q = q.filter(SupportTicketAttachment.ticket_id == ticket.id)
    rows = q.all()
    if len(rows) != len(ids):
        raise ValueError('One or more attachments are invalid or no longer available.')
    if draft_token and any(r.draft_token != draft_token for r in rows):
        raise ValueError('One or more attachments do not belong to this ticket draft.')
    if uploader_type == 'customer':
        existing = db.query(SupportTicketAttachment).filter(SupportTicketAttachment.ticket_id == ticket.id, SupportTicketAttachment.uploader_type == 'customer', SupportTicketAttachment.message_id.is_not(None)).count()
        if existing + len(rows) > CUSTOMER_TICKET_ATTACHMENT_LIMIT:
            raise ValueError('Customers can upload a maximum of 5 attachments per ticket.')
    root = attachment_root(storage_dir); dest_dir = root / (ticket.reference or f'ticket-{ticket.id}'); dest_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        src = root / '_pending' / row.storage_filename; dest = dest_dir / row.storage_filename
        if not src.exists(): raise ValueError(f'Pending attachment {row.original_filename} is missing from storage.')
        shutil.move(str(src), str(dest))
        row.ticket_id=ticket.id; row.message_id=message.id; row.draft_token=None; row.attached_at=datetime.utcnow()
    db.flush()
    return rows


def attachment_path(row: SupportTicketAttachment, storage_dir: str) -> Path:
    root = attachment_root(storage_dir)
    if row.message_id and row.ticket and row.ticket.reference:
        return root / row.ticket.reference / row.storage_filename
    return root / '_pending' / row.storage_filename


def cleanup_pending(db: Session, storage_dir: str, *, now: datetime | None = None) -> int:
    now = now or datetime.utcnow(); cutoff = now - timedelta(hours=24)
    rows = db.query(SupportTicketAttachment).filter(SupportTicketAttachment.message_id.is_(None), SupportTicketAttachment.created_at < cutoff).all()
    removed=0
    for row in rows:
        try: attachment_path(row, storage_dir).unlink(missing_ok=True)
        except OSError: pass
        db.delete(row); removed += 1
    db.commit(); return removed
