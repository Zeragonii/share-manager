# Architecture

## Core domain

- **Customer**: the person receiving access.
- **Package**: what access is granted.
- **BillingTier**: how much/how often a package is billed.
- **Subscription**: connects a customer to a billing tier and lifecycle state.
- **Payment**: immutable-style ledger entry for money received.
- **Integration**: an external entitlement target/provider configuration.
- **PackageEntitlement**: generic resource mapping between a package and an integration.
- **AuditLog**: management/reconciliation history.

## Integration boundary

Plex-specific behaviour lives under `app/integrations/plex.py`. Core package/subscription tables do not contain Plex library columns. This is intentional so future integrations can expose their own resource types while reusing the same entitlement model.

## Desired-state reconciliation

`app/services/reconcile.py` calculates the desired resources from the customer's active subscriptions and asks the integration adapter to enforce them. This is preferred to one-shot event actions because a later reconciliation can repair drift or a previously failed API request.

## Payment-provider future

Payment processors should ultimately translate webhook/provider events into the same internal `Payment` and subscription-period concepts. The entitlement engine should not care whether a payment originated from manual entry, bank transfer, Stripe, PayPal, or another adapter.
