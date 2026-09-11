from pathlib import Path


def test_portal_navigation_has_news_menu_and_support_heartbeat():
    nav = Path('app/templates/_portal_navigation.html').read_text()
    assert '/portal/news' in nav
    assert 'portal-mobile-menu' in nav
    assert '/portal/api/tickets/summary' in nav
    assert 'data-portal-support-count' in nav
    assert '10000' in nav


def test_news_page_and_request_menu_wiring():
    news = Path('app/templates/portal_news.html').read_text()
    dashboard = Path('app/templates/portal_dashboard.html').read_text()
    assert 'News & upcoming events' in news
    assert 'upcoming_banners' in news
    assert 'js-portal-local-time' in news
    assert 'portal-request-mobile-card' not in dashboard
