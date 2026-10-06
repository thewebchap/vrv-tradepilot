import httpx
import pytest

import outlook


@pytest.fixture
def graph(monkeypatch):
    """Fake Graph: returns queued responses and records requests. No real account needed."""
    calls, responses, sleeps = [], [], []
    monkeypatch.setattr(outlook, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(outlook.time, "sleep", sleeps.append)

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return responses.pop(0)

    monkeypatch.setattr(outlook.httpx, "request", fake_request)
    return calls, responses, sleeps


def test_429_is_retried_after_retry_after(graph):
    calls, responses, sleeps = graph
    responses += [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"value": [{"id": "b"}, {"id": "a"}]})]
    assert [m["id"] for m in outlook.list_recent_messages("2026-10-04T00:00:00Z")] == ["a", "b"]  # oldest first
    assert len(calls) == 2 and sleeps == [7]


def test_reply_is_not_retried_after_server_error(graph):
    calls, responses, _ = graph
    responses += [httpx.Response(500, json={"error": {"code": "x", "message": "y"}})]
    with pytest.raises(outlook.OutlookError):
        outlook.reply_to_message("msg-1", "Status: GOOD TO GO")
    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "POST" and url.endswith("/me/messages/msg-1/reply") and "GOOD TO GO" in body["comment"]


# --- delegated authentication (fake MSAL app, no real tenant needed) ----------------------

TENANT = "11111111-2222-3333-4444-555555555555"
ACCOUNT = {"home_account_id": f"oid.{TENANT}", "username": "ops@vrv.example"}


class FakeMsal:
    def __init__(self, accounts=(), silent=None, device_result=None, flow=None):
        self.accounts, self.silent, self.device_result = list(accounts), silent, device_result
        self.flow = flow or {"user_code": "ABC123", "message": "To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABC123"}
        self.device_flows = 0

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent_with_error(self, scopes, account):
        assert scopes == ["Mail.ReadWrite", "Mail.Send"]
        return self.silent

    def initiate_device_flow(self, scopes):
        self.device_flows += 1
        return self.flow

    def acquire_token_by_device_flow(self, flow):
        return self.device_result


@pytest.fixture
def auth(monkeypatch, tmp_path):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", TENANT)
    monkeypatch.setattr(outlook, "TOKEN_CACHE_FILE", tmp_path / ".token_cache.json")

    def use(fake):
        monkeypatch.setattr(outlook, "_app", fake)
        return fake

    return use


def test_cached_login_needs_no_interaction(auth, capsys):
    fake = auth(FakeMsal(accounts=[ACCOUNT], silent={"access_token": "cached"}))
    assert outlook.get_access_token() == "cached"
    assert fake.device_flows == 0 and capsys.readouterr().out == ""


@pytest.mark.parametrize("error", ["interaction_required", "login_required", "invalid_grant", "consent_required"])
def test_silent_failure_falls_back_to_device_code(auth, capsys, error):
    fake = auth(FakeMsal(accounts=[ACCOUNT], silent={"error": error, "error_description": "AADSTS50058: ..."},
                         device_result={"access_token": "fresh", "id_token_claims": {"preferred_username": "ops@vrv.example"}}))  # fmt: skip
    assert outlook.get_access_token() == "fresh"
    assert fake.device_flows == 1
    out = capsys.readouterr().out
    assert "microsoft.com/devicelogin" in out and "ABC123" in out and "fresh" not in out  # token never printed


def test_first_run_uses_device_code(auth):
    fake = auth(FakeMsal(device_result={"access_token": "new"}))
    assert outlook.get_access_token() == "new" and fake.device_flows == 1


def test_cached_account_from_other_tenant_is_ignored(auth):
    other = {"home_account_id": "oid.99999999-0000-0000-0000-000000000000", "username": "me@hotmail.com"}
    fake = auth(FakeMsal(accounts=[other], silent={"access_token": "wrong-tenant"}, device_result={"access_token": "new"}))
    assert outlook.get_access_token() == "new" and fake.device_flows == 1


@pytest.mark.parametrize(
    "result,hint",
    [
        ({"error": "authorization_declined", "error_description": "user declined"}, "cancelled"),
        ({"error": "invalid_grant", "error_description": "AADSTS65001: consent"}, "administrator"),
        ({"error": "invalid_request", "error_description": "AADSTS50020: user account from other tenant"}, "different organization"),
    ],
)
def test_device_login_errors_are_explained(auth, result, hint):
    auth(FakeMsal(device_result=result))
    with pytest.raises(outlook.OutlookError, match=hint):
        outlook.get_access_token()


def test_public_client_flow_disabled_is_explained(auth):
    auth(FakeMsal(flow={"error": "invalid_client", "error_description": "AADSTS7000218: client_assertion required"}))
    with pytest.raises(outlook.OutlookError, match="Allow public client flows"):
        outlook.get_access_token()


def test_consumers_tenant_is_rejected(monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "consumers")
    monkeypatch.setattr(outlook, "_app", None)
    with pytest.raises(outlook.OutlookError, match="Directory \\(tenant\\) ID"):
        outlook.get_access_token()


def test_unreadable_token_cache_does_not_crash(monkeypatch, tmp_path):
    bad = tmp_path / ".token_cache.json"
    bad.write_text("not json")
    monkeypatch.setattr(outlook, "TOKEN_CACHE_FILE", bad)
    outlook._load_cache()  # logs a warning, falls back to signing in


def test_unknown_tenant_is_explained(monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", TENANT)
    monkeypatch.setattr(outlook, "_app", None)

    def no_such_tenant(*args, **kwargs):
        raise ValueError("Unable to get authority configuration")

    monkeypatch.setattr(outlook.msal, "PublicClientApplication", no_such_tenant)
    with pytest.raises(outlook.OutlookError, match="was not found"):
        outlook.get_access_token()
