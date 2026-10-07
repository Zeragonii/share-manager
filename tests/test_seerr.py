from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BillingTier, Customer, Package, RequestsPlatformSettings, Subscription
from app.services.seerr import (
    SEERR_REQUEST,
    SEERR_REQUEST_4K,
    SEERR_REQUEST_4K_MOVIE,
    SEERR_REQUEST_4K_TV,
    SEERR_REQUEST_MOVIE,
    SEERR_REQUEST_TV,
    cache_usage,
    effective_policy,
    match_customer,
    policy_permissions,
    policy_values,
    quota_drift,
)


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



def test_seerr_username_is_exact_fallback_when_plex_username_field_is_blank():
    c = Customer(name="DStride", email="different@example.com", plex_username="DStride")
    users = [
        {"id": 17, "plexUsername": None, "username": "dstride", "email": "seerr@example.com"},
        {"id": 18, "plexUsername": "someoneelse", "username": "someoneelse", "email": "different@example.com"},
    ]
    user, method = match_customer(FakeSeerr(users), c, users)
    assert user["id"] == 17
    assert method == "username"


def test_seerr_username_fallback_refuses_ambiguous_matches():
    c = Customer(name="Duplicate", plex_username="SameName")
    users = [
        {"id": 1, "plexUsername": None, "username": "samename", "email": "one@example.com"},
        {"id": 2, "plexUsername": None, "username": "SameName", "email": "two@example.com"},
    ]
    user, method = match_customer(FakeSeerr(users), c, users)
    assert user is None
    assert method is None

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


def test_list_users_follows_take_skip_pagination():
    from app.integrations.seerr import SeerrIntegration

    client = SeerrIntegration("http://seerr.test", "key")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("params")))
        skip = kwargs["params"]["skip"]
        if skip == 0:
            return {
                "pageInfo": {"page": 1, "pages": 2, "results": 88, "pageSize": 50},
                "results": [{"id": i} for i in range(1, 51)],
            }
        if skip == 50:
            return {
                "pageInfo": {"page": 2, "pages": 2, "results": 88, "pageSize": 50},
                "results": [{"id": i} for i in range(51, 89)],
            }
        raise AssertionError(f"unexpected skip {skip}")

    client._request = fake_request
    users = client.list_users()

    assert len(users) == 88
    assert users[0]["id"] == 1
    assert users[-1]["id"] == 88
    assert calls == [
        ("GET", "/user", {"take": 50, "skip": 0}),
        ("GET", "/user", {"take": 50, "skip": 50}),
    ]


def test_list_users_stops_on_short_page_without_pageinfo():
    from app.integrations.seerr import SeerrIntegration

    client = SeerrIntegration("http://seerr.test", "key")
    client._request = lambda method, path, **kwargs: {
        "results": [{"id": i} for i in range(1, 11)]
    }
    users = client.list_users()
    assert len(users) == 10


def test_policy_values_minus_one_is_unmanaged_and_zero_is_explicit():
    p = Package(
        name="Blocked movies",
        seerr_manage_quotas=True,
        seerr_movie_limit=0,
        seerr_movie_days=30,
        seerr_tv_limit=-1,
        seerr_tv_days=30,
    )
    assert policy_values(p) == {"movieQuotaLimit": 0, "movieQuotaDays": 30}
    assert quota_drift(
        {"movieQuotaLimit": 0, "movieQuotaDays": 30, "tvQuotaLimit": 99, "tvQuotaDays": 7},
        p,
    ) == {}


def test_zero_movie_limit_blocks_movie_permission_but_preserves_tv_from_generic():
    baseline = SEERR_REQUEST | SEERR_REQUEST_4K
    p = Package(name="TV only", seerr_manage_quotas=True, seerr_movie_limit=0, seerr_tv_limit=-1)
    desired = policy_permissions(baseline, baseline, p)
    assert not (desired & SEERR_REQUEST)
    assert not (desired & SEERR_REQUEST_MOVIE)
    assert desired & SEERR_REQUEST_TV
    assert not (desired & SEERR_REQUEST_4K)
    assert not (desired & SEERR_REQUEST_4K_MOVIE)
    assert desired & SEERR_REQUEST_4K_TV


def test_zero_both_limits_removes_all_request_permissions_only():
    unrelated = 1 << 20
    baseline = unrelated | SEERR_REQUEST | SEERR_REQUEST_MOVIE | SEERR_REQUEST_TV | SEERR_REQUEST_4K | SEERR_REQUEST_4K_MOVIE | SEERR_REQUEST_4K_TV
    p = Package(name="No requests", seerr_manage_quotas=True, seerr_movie_limit=0, seerr_tv_limit=0)
    desired = policy_permissions(baseline, baseline, p)
    assert desired == unrelated


def test_minus_one_limits_do_not_change_request_permissions():
    baseline = SEERR_REQUEST | SEERR_REQUEST_4K | (1 << 20)
    p = Package(name="Unmanaged", seerr_manage_quotas=True, seerr_movie_limit=-1, seerr_tv_limit=-1)
    assert policy_permissions(baseline, baseline, p) == baseline
