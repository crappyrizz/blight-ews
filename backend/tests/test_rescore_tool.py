"""Tests for tools/rescore.py: login, re-score, and plain-words answers. No network: a fake server."""
import httpx
import pytest

from tools import rescore as rs

BASE = "https://example.test"


def fake_server(login_status=200, rescore_status=200, sms="submitted"):
    seen = {}

    def handler(request):
        if request.url.path == "/auth/login":
            seen["login"] = request.read()
            if login_status != 200:
                return httpx.Response(login_status, json={"detail": "Incorrect phone number or password"})
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
        seen["auth"] = request.headers.get("Authorization")
        if rescore_status != 200:
            return httpx.Response(rescore_status, json={"detail": "x"})
        return httpx.Response(200, json={"node_id": "demo-01", "risk_level": "high", "risk_score": 0.92,
                                         "risk_basis": "learned_24h", "high_risk_sms": sms, "scores": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.seen = seen
    return client


def test_logs_in_then_rescores_with_the_bearer_token():
    client = fake_server()
    token = rs.login(client, BASE, "0700000001", "pw")
    result = rs.rescore(client, BASE, token, "demo-01")
    assert client.seen["auth"] == "Bearer tok"
    assert result["risk_level"] == "high"


def test_a_wrong_password_is_said_plainly():
    with pytest.raises(rs.RescoreError, match="wrong phone number or password"):
        rs.login(fake_server(login_status=401), BASE, "0700000001", "bad")


@pytest.mark.parametrize("status, words", [(401, "did not accept the login token"), (403, "not an admin"),
                                           (404, "no node called demo-01")])
def test_rescore_refusals_are_said_plainly(status, words):
    with pytest.raises(rs.RescoreError, match=words):
        rs.rescore(fake_server(rescore_status=status), BASE, "tok", "demo-01")


@pytest.mark.parametrize("sms, words", [("submitted", "sent"), ("dry_run", "SMS_DRY_RUN"),
                                        ("error", "AT_API_KEY"), (None, "did not CHANGE")])
def test_every_sms_outcome_is_explained(sms, words):
    lines = rs.describe({"node_id": "demo-01", "risk_level": "high", "risk_score": 0.92,
                         "risk_basis": "learned_24h", "high_risk_sms": sms})
    assert lines[0] == "demo-01: risk HIGH (score 0.92, from learned_24h)"
    assert words in lines[1]
