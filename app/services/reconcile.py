from sqlalchemy.orm import Session, joinedload
from ..models import AuditLog, BillingTier, Customer, Integration, Subscription
from ..integrations.plex import PlexIntegration

ACTIVE_STATES = {"active", "grace"}


def _result_detail(integration_name: str, result: dict) -> tuple[str, str]:
    libraries = result.get("libraries", [])
    names = ", ".join(libraries) or "none"
    count = len(libraries)
    state = result.get("state", "applied")

    if state == "invited":
        return (
            "plex.invite",
            f"Sent Plex invitation via {integration_name} with {count} libraries: {names}",
        )
    if state == "pending":
        return (
            "plex.invite.pending",
            f"Plex invitation via {integration_name} is awaiting acceptance with {count} libraries: {names}",
        )
    if state == "removed":
        return (
            "plex.reconcile",
            f"Removed Plex server access via {integration_name}",
        )
    return (
        "plex.reconcile",
        f"Applied and verified {count} Plex libraries via {integration_name}: {names}",
    )


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
        result = client.apply_libraries(
            customer.plex_username,
            sorted(desired),
            plex_user_id=customer.plex_user_id,
            email=customer.email,
        )
        action, detail = _result_detail(integration.name, result)
        db.add(AuditLog(action=action, target_type="customer", target_id=str(customer.id), detail=detail))
        messages.append(detail)
    db.commit()
    return messages
