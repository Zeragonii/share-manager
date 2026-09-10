from __future__ import annotations
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from ..models import AuditLog, CustomerNotificationPreference, SupportTicket, SupportTicketMessage
from .notifications import notify_event, send_customer_direct_push

TICKET_CATEGORIES = {
    "technical": "Technical issue",
    "playback": "Playback / streaming",
    "account": "Account / access",
    "subscription": "Subscription / billing",
    "request": "Request / suggestion",
    "other": "Other",
}
TICKET_STATUSES = {
    "open": "Open",
    "reviewed": "Reviewed",
    "in_progress": "In Progress",
    "resolved": "Resolved",
    "closed": "Closed",
}
TICKET_PRIORITIES = {"low": "Low", "normal": "Normal", "high": "High", "critical": "Critical"}


def ticket_ref(ticket_id: int) -> str:
    return f"TKT-{ticket_id:06d}"


def create_ticket(db: Session, customer, *, category: str, subject: str, description: str, notifications_enabled: bool) -> SupportTicket:
    category = (category or "").strip().lower(); subject = (subject or "").strip(); description = (description or "").strip()
    if category not in TICKET_CATEGORIES: raise ValueError("Choose a valid support category")
    if not subject or len(subject) > 160: raise ValueError("Subject is required and must be 160 characters or fewer")
    if not description or len(description) > 10000: raise ValueError("Description is required and must be 10,000 characters or fewer")
    since = datetime.utcnow() - timedelta(hours=1)
    if db.query(SupportTicket).filter(SupportTicket.customer_id == customer.id, SupportTicket.created_at >= since).count() >= 5:
        raise ValueError("Too many tickets created recently. Please wait before opening another ticket.")
    row = SupportTicket(customer_id=customer.id, category=category, subject=subject, status="open", priority="normal", notifications_enabled=notifications_enabled, admin_unread=True)
    db.add(row); db.flush(); row.reference = ticket_ref(row.id)
    db.add(SupportTicketMessage(ticket_id=row.id, author_type="customer", author_label=customer.name, body=description, visible_to_customer=True))
    db.add(AuditLog(actor=f"portal:{customer.portal_username}", action="ticket.created", target_type="support_ticket", target_id=row.reference, detail=f"{TICKET_CATEGORIES[category]}: {subject}"))
    db.commit(); db.refresh(row)
    notify_event(db, event="ticket.created", title=f"New support ticket {row.reference}", message=f"{customer.name}: {subject}", target_type="support_ticket", target_id=row.reference, event_key=f"ticket:{row.id}:created", data={"customer_id": customer.id, "url": f"/tickets/{row.reference}"})
    return row


def add_customer_reply(db: Session, ticket: SupportTicket, customer, body: str) -> SupportTicketMessage:
    body=(body or "").strip()
    if ticket.status == "closed": raise ValueError("Closed tickets cannot be replied to")
    if not body or len(body)>10000: raise ValueError("Reply is required and must be 10,000 characters or fewer")
    since=datetime.utcnow()-timedelta(hours=1)
    if db.query(SupportTicketMessage).filter(SupportTicketMessage.ticket_id==ticket.id, SupportTicketMessage.author_type=="customer", SupportTicketMessage.created_at>=since).count() >= 30:
        raise ValueError("Too many replies recently. Please wait before replying again.")
    if ticket.status == "resolved": ticket.status="open"; ticket.resolved_at=None
    msg=SupportTicketMessage(ticket_id=ticket.id, author_type="customer", author_label=customer.name, body=body, visible_to_customer=True)
    db.add(msg); ticket.admin_unread=True; ticket.updated_at=datetime.utcnow()
    db.add(AuditLog(actor=f"portal:{customer.portal_username}", action="ticket.customer_reply", target_type="support_ticket", target_id=ticket.reference, detail="Customer replied"))
    db.commit(); db.refresh(msg)
    notify_event(db,event="ticket.customer_reply",title=f"Reply on {ticket.reference}",message=f"{customer.name} replied: {ticket.subject}",target_type="support_ticket",target_id=ticket.reference,event_key=f"ticket:{ticket.id}:customer:{msg.id}",data={"customer_id":customer.id,"url":f"/tickets/{ticket.reference}"})
    return msg


def add_admin_reply(db: Session, ticket: SupportTicket, body: str, actor: str) -> SupportTicketMessage:
    body=(body or "").strip()
    if ticket.status == "closed": raise ValueError("Reopen the ticket before replying")
    if not body or len(body)>10000: raise ValueError("Reply is required and must be 10,000 characters or fewer")
    msg=SupportTicketMessage(ticket_id=ticket.id, author_type="admin", author_label="Support", body=body, visible_to_customer=True)
    db.add(msg); ticket.customer_unread=True; ticket.admin_unread=False; ticket.updated_at=datetime.utcnow()
    db.add(AuditLog(actor=actor, action="ticket.admin_reply", target_type="support_ticket", target_id=ticket.reference, detail="Admin replied")); db.commit(); db.refresh(msg)
    if ticket.notifications_enabled:
        send_customer_direct_push(db,customer_id=ticket.customer_id,event="ticket.admin_reply",title=f"Support replied · {ticket.reference}",message=ticket.subject,url=f"/portal/tickets/{ticket.reference}",event_key=f"ticket:{ticket.id}:admin:{msg.id}")
    return msg


def add_internal_note(db: Session, ticket: SupportTicket, body: str, actor: str) -> SupportTicketMessage:
    body=(body or "").strip()
    if not body or len(body)>10000: raise ValueError("Internal note is required and must be 10,000 characters or fewer")
    msg=SupportTicketMessage(ticket_id=ticket.id, author_type="internal", author_label=actor, body=body, visible_to_customer=False)
    db.add(msg); ticket.updated_at=datetime.utcnow(); db.add(AuditLog(actor=actor, action="ticket.internal_note", target_type="support_ticket", target_id=ticket.reference, detail="Internal note added")); db.commit(); db.refresh(msg); return msg


def change_status(db: Session, ticket: SupportTicket, status: str, actor: str) -> None:
    if status not in TICKET_STATUSES: raise ValueError("Invalid ticket status")
    old=ticket.status
    if old==status: return
    now=datetime.utcnow(); ticket.status=status; ticket.updated_at=now
    ticket.resolved_at = now if status=="resolved" else (None if old=="resolved" else ticket.resolved_at)
    ticket.closed_at = now if status=="closed" else None
    ticket.customer_unread=True
    db.add(AuditLog(actor=actor, action="ticket.status", target_type="support_ticket", target_id=ticket.reference, detail=f"{old} -> {status}")); db.commit()
    if ticket.notifications_enabled:
        send_customer_direct_push(db,customer_id=ticket.customer_id,event="ticket.status_changed",title=f"Ticket {ticket.reference} · {TICKET_STATUSES[status]}",message=ticket.subject,url=f"/portal/tickets/{ticket.reference}",event_key=f"ticket:{ticket.id}:status:{status}:{int(now.timestamp())}")


def change_priority(db: Session, ticket: SupportTicket, priority: str, actor: str) -> None:
    if priority not in TICKET_PRIORITIES: raise ValueError("Invalid priority")
    old=ticket.priority; ticket.priority=priority; ticket.updated_at=datetime.utcnow(); db.add(AuditLog(actor=actor, action="ticket.priority", target_type="support_ticket", target_id=ticket.reference, detail=f"{old} -> {priority}")); db.commit()
