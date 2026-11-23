"""Pages cached before no-cache existed are evicted once, through /auth/me."""


def test_first_session_probe_evicts_the_cache_once(new_client):
    client = new_client()
    first = client.get("/auth/me")
    assert first.status_code == 401             # evicts for signed-out browsers too
    assert first.headers["clear-site-data"] == '"cache"'    # never storage or cookies
    assert "clear-site-data" not in client.get("/auth/me").headers


def test_other_requests_never_evict(new_client):
    client = new_client()
    for path in ("/studio.html", "/home.html", "/version"):
        assert "clear-site-data" not in client.get(path).headers
