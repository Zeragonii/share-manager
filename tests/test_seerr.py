from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BillingTier, Customer, Package, RequestsPlatformSettings, Subscription
from app.services.seerr import effective_policy, match_customer, policy_values, quota_drift, cache_usage


class FakeSeerr:
    def __init__(self, users):
        self.users = users
    def list_users(self):
        return self.users
    def user(self, user_id):
        for row in self.users:
            if int(row["id"]) == int(user_id):
                return row
        raise RuntimeError("missing")


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_highest_priority_assigned_package_controls_seerr_policy():
    db = make_db()
    c = Customer(name="Customer", plex_username="plexuser")
    low = Package(name="Basic", seerr_manage_quotas=True, seerr_policy_priority=10, seerr_movie_limit=5, seerr_movie_days=30, seerr_tv_limit=10, seerr_tv_days=30)
    high = Package(name="Premium", seerr_manage_quotas=True, seerr_policy_priority=20, seerr_movie_limit=20, seerr_movie_days=30, seerr_tv_limit=40, seerr_tv_days=30)
    low_tier = BillingTier(package=low, name="Monthly", price=10)
    high_tier = BillingTier(package=high, name="Yearly", price=100)
    db.add_all([c, low, high, low_tier, high_tier]); db.flush()
    db.add_all([Subscription(customer=c, billing_tier=low_tier, status="active"), Subscription(customer=c, billing_tier=high_tier, status="suspended")]); db.commit()
    assert effective_policy(db, c.id).id == high.id
    assert policy_values(high)["tvQuotaLimit"] == 40


def test_cancelled_package_does_not_supply_seerr_policy():
    db = make_db()
    c = Customer(name="Customer", plex_username="plexuser")
    p = Package(name="Old", seerr_manage_quotas=True, seerr_policy_priority=99, seerr_movie_limit=999)
    t = BillingTier(package=p, name="Old", price=1)
    db.add_all([c,p,t]); db.flush(); db.add(Subscription(customer=c,billing_tier=t,status="cancelled")); db.commit()
    assert effective_policy(db, c.id) is None


def test_plex_username_is_primary_seerr_identity_key():
    c = Customer(name="Customer", email="same@example.com", plex_username="PlexUser")
    users = [{"id": 1, "plexUsername": "plexuser", "email": "other@example.com"}, {"id": 2, "plexUsername": "other", "email": "same@example.com"}]
    user, method = match_customer(FakeSeerr(users), c, users)
    assert user["id"] == 1
    assert method == "plex_username"


def test_quota_drift_compares_package_values():
    p = Package(name="Plex", seerr_manage_quotas=True, seerr_movie_limit=10, seerr_movie_days=30, seerr_tv_limit=20, seerr_tv_days=30)
    assert quota_drift({"movieQuotaLimit":10,"movieQuotaDays":30,"tvQuotaLimit":20,"tvQuotaDays":30}, p) == {}
    assert "movieQuotaLimit" in quota_drift({"movieQuotaLimit":5,"movieQuotaDays":30,"tvQuotaLimit":20,"tvQuotaDays":30}, p)


def test_usage_cache_counts_tv_seasons_not_tv_requests():
    c = Customer(name="Customer")
    quota = {"movie":{"limit":10,"remaining":7}, "tv":{"limit":20,"remaining":15}}
    requests = [{"type":"movie"}, {"type":"tv","seasons":[1,2,3]}, {"type":"tv","seasons":[1,2]}]
    cache_usage(c, quota, requests)
    assert c.seerr_movie_used == 3
    assert c.seerr_tv_used == 5
    assert c.seerr_movie_requests_total == 1
    assert c.seerr_tv_seasons_total == 5
    assert c.seerr_request_count == 3
