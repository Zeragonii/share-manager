from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Customer, CustomerNotificationPreference, SupportTicket, SupportTicketMessage
from app.services.tickets import create_ticket, add_customer_reply, add_admin_reply, change_status


def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def customer(db):
    c = Customer(name="Test Customer", status="active", portal_enabled=True, portal_username="tester")
    db.add(c); db.commit(); db.refresh(c); return c


def test_ticket_creation_generates_reference_and_initial_message():
    db=db_session(); c=customer(db)
    with patch("app.services.tickets.notify_event"):
        ticket=create_ticket(db,c,category="technical",subject="Playback broken",description="It buffers forever",notifications_enabled=True)
    assert ticket.reference == f"TKT-{ticket.id:06d}"
    assert ticket.status == "open" and ticket.priority == "normal" and ticket.admin_unread is True
    message=db.query(SupportTicketMessage).filter_by(ticket_id=ticket.id).one()
    assert message.author_type == "customer" and message.body == "It buffers forever"


def test_customer_reply_reopens_resolved_ticket():
    db=db_session(); c=customer(db)
    with patch("app.services.tickets.notify_event"):
        ticket=create_ticket(db,c,category="playback",subject="Issue",description="Initial",notifications_enabled=True)
        change_status(db,ticket,"resolved","admin")
        add_customer_reply(db,ticket,c,"Still happening")
    assert ticket.status == "open"
    assert ticket.resolved_at is None
    assert ticket.admin_unread is True


def test_admin_reply_only_pushes_when_ticket_subscribed():
    db=db_session(); c=customer(db)
    with patch("app.services.tickets.notify_event"):
        ticket=create_ticket(db,c,category="account",subject="Access",description="Help",notifications_enabled=True)
    with patch("app.services.tickets.send_customer_direct_push") as push:
        add_admin_reply(db,ticket,"Fixed now","admin")
        assert push.call_count == 1
        ticket.notifications_enabled=False; db.commit()
        add_admin_reply(db,ticket,"One more thing","admin")
        assert push.call_count == 1


def test_closed_ticket_rejects_customer_reply():
    db=db_session(); c=customer(db)
    with patch("app.services.tickets.notify_event"):
        ticket=create_ticket(db,c,category="other",subject="Question",description="Hello",notifications_enabled=False)
    change_status(db,ticket,"closed","admin")
    try:
        add_customer_reply(db,ticket,c,"Again")
        assert False, "closed ticket should reject replies"
    except ValueError as exc:
        assert "Closed" in str(exc)
