import asyncio
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Customer, SupportTicketAttachment
from app.services.attachments import save_pending_upload, attach_pending, CUSTOMER_TICKET_ATTACHMENT_LIMIT
from app.services.tickets import create_ticket


def db_session():
    engine=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def customer(db):
    c=Customer(name='Attach Customer',status='active',portal_enabled=True,portal_username='attach')
    db.add(c);db.commit();db.refresh(c);return c


def png_upload(name='screen.png'):
    return UploadFile(filename=name,file=BytesIO(b'\x89PNG\r\n\x1a\n'+b'x'*128))


def test_customer_attachment_is_staged_then_attached(tmp_path):
    db=db_session(); c=customer(db); token='draft-1'
    row=asyncio.run(save_pending_upload(db,png_upload(),storage_dir=str(tmp_path),uploader_type='customer',uploader_customer_id=c.id,draft_token=token,actor='test'))
    assert (tmp_path/'_pending'/row.storage_filename).exists()
    with patch('app.services.tickets.notify_event'):
        ticket=create_ticket(db,c,category='technical',subject='Screenshot',description='See attached',notifications_enabled=False,attachment_ids=[row.id],draft_token=token,attachment_dir=str(tmp_path))
    db.refresh(row)
    assert row.ticket_id==ticket.id and row.message_id is not None
    assert (tmp_path/ticket.reference/row.storage_filename).exists()


def test_customer_draft_limit_is_five(tmp_path):
    db=db_session(); c=customer(db); token='draft-2'
    for i in range(CUSTOMER_TICKET_ATTACHMENT_LIMIT):
        asyncio.run(save_pending_upload(db,png_upload(f'{i}.png'),storage_dir=str(tmp_path),uploader_type='customer',uploader_customer_id=c.id,draft_token=token,actor='test'))
    try:
        asyncio.run(save_pending_upload(db,png_upload('sixth.png'),storage_dir=str(tmp_path),uploader_type='customer',uploader_customer_id=c.id,draft_token=token,actor='test'))
        assert False, 'sixth customer attachment should be rejected'
    except ValueError as exc:
        assert 'maximum of 5' in str(exc)


def test_content_must_match_extension(tmp_path):
    db=db_session(); c=customer(db)
    bad=UploadFile(filename='fake.png',file=BytesIO(b'not a png at all'))
    try:
        asyncio.run(save_pending_upload(db,bad,storage_dir=str(tmp_path),uploader_type='customer',uploader_customer_id=c.id,draft_token='x',actor='test'))
        assert False, 'mismatched file contents should be rejected'
    except ValueError as exc:
        assert 'contents do not match' in str(exc) or 'Unsupported' in str(exc)
