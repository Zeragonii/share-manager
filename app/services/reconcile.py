from sqlalchemy.orm import Session, joinedload
from ..models import AuditLog, BillingTier, Customer, Integration, Subscription
from ..integrations.plex import PlexIntegration

ACTIVE_STATES = {"active", "grace"}

def reconcile_customer(db: Session, customer: Customer) -> list[str]:
    messages = []
    if customer.exempt or not customer.plex_username:
        return ["Skipped: exempt or no Plex user linked"]

    plex_integrations = db.query(Integration).filter(Integration.kind == "plex", Integration.enabled == True).all()  # noqa: E712
    for integration in plex_integrations:
        desired = set()
        subs = (
            db.query(Subscription)
            .options(joinedload(Subscription.billing_tier).joinedload(BillingTier.package))
            .filter(Subscription.customer_id == customer.id)
            .all()
        )
        for sub in subs:
            if sub.status not in ACTIVE_STATES or customer.status not in ACTIVE_STATES:
                continue
            package = sub.billing_tier.package
            for entitlement in package.entitlements:
                if entitlement.integration_id == integration.id and entitlement.resource_type == "library":
                    desired.add(entitlement.resource_name)

        client = PlexIntegration(integration.base_url, integration.secret)
        client.apply_libraries(customer.plex_username, sorted(desired))
        detail = f"Applied {len(desired)} Plex libraries via {integration.name}: {', '.join(sorted(desired)) or 'none'}"
        db.add(AuditLog(action="plex.reconcile", target_type="customer", target_id=str(customer.id), detail=detail))
        messages.append(detail)
    db.commit()
    return messages
