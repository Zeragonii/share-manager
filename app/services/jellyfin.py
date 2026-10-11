"""Jellyfin library entitlement reconciliation, keyed by stored linked account IDs."""
from sqlalchemy.orm import joinedload
from ..models import (ACCESS_SUBSCRIPTION_STATES, AuditLog, BillingTier, Customer,
                      Integration, JellyfinAccount, Package, PackageEntitlement, Subscription)
from ..integrations.jellyfin import JellyfinIntegration


def reconcile_customer(db, customer: Customer):
    account = db.query(JellyfinAccount).filter(JellyfinAccount.customer_id == customer.id).first()
    if not account:
        return []
    integration = db.get(Integration, account.integration_id)
    if not integration or integration.kind != "jellyfin" or not integration.enabled:
        return []
    if account.is_administrator:
        return ["Jellyfin administrator left unmanaged"]
    desired = set()
    # Exemptions bypass automated entitlements; never strip manually-managed libraries.
    if customer.exempt:
        return ["Jellyfin exempt customer left unmanaged"]
    if not customer.archived and customer.status in ACCESS_SUBSCRIPTION_STATES:
        subscriptions = db.query(Subscription).options(
            joinedload(Subscription.billing_tier).joinedload(BillingTier.package).joinedload(Package.entitlements)
        ).filter(Subscription.customer_id == customer.id).all()
        for sub in subscriptions:
            if sub.status not in ACCESS_SUBSCRIPTION_STATES:
                continue
            for entitlement in sub.billing_tier.package.entitlements:
                if entitlement.integration_id == integration.id and entitlement.resource_type == "library":
                    desired.add(entitlement.resource_id)
    result = JellyfinIntegration(integration.base_url, integration.secret).apply_libraries(
        account.jellyfin_user_id, sorted(desired))
    if result["state"] == "applied":
        db.add(AuditLog(action="jellyfin.libraries.reconcile", target_type="customer",
                        target_id=str(customer.id), detail=f"Set {len(desired)} Jellyfin libraries for {account.username}"))
        db.commit()
    return [f"Jellyfin {account.username}: {result['state']} ({len(desired)} libraries)"]
