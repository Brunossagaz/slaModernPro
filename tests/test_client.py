from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sla_modern_pro import client as client_module
from sla_modern_pro.client import ZabbixApiError, ZabbixJsonRpcClient, parse_version


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    """Imita o Zabbix: versão configurável e se aceita ou não o campo "auth" no corpo."""

    def __init__(self, version, accepts_body_auth=True):
        self.version = version
        self.accepts_body_auth = accepts_body_auth
        self.posts = []

    def post(self, url, json, headers=None, timeout=None):
        self.posts.append({"payload": json, "headers": dict(headers or {})})
        method = json["method"]
        if "auth" in json and not self.accepts_body_auth:
            return FakeResponse({"error": {"code": -32600, "data": 'unexpected parameter "auth".'}})
        if method == "apiinfo.version":
            if self.version is None:
                return FakeResponse({"error": {"code": -32601, "data": "Method not found."}})
            return FakeResponse({"result": self.version})
        if method == "user.login":
            return FakeResponse({"result": "tok123"})
        return FakeResponse({"result": [{"groupid": "1", "name": "G"}]})

    def close(self):
        pass


def _client(monkeypatch, session):
    monkeypatch.setattr(client_module.requests, "Session", lambda: session)
    return ZabbixJsonRpcClient("http://zbx/api_jsonrpc.php", "u", "p")


def test_parse_version():
    assert parse_version("7.4.0") == (7, 4, 0)
    assert parse_version("6.4.0rc1") == (6, 4, 0)
    with pytest.raises(ValueError):
        parse_version("desconhecida")


def test_zabbix_74_uses_bearer_header_and_never_sends_auth_field(monkeypatch):
    session = FakeSession("7.4.0", accepts_body_auth=False)
    client = _client(monkeypatch, session)
    assert client.get_hostgroups() == [{"groupid": "1", "name": "G"}]
    client.logout()

    authed = [post for post in session.posts if post["payload"]["method"] in {"hostgroup.get", "user.logout"}]
    assert [post["headers"].get("Authorization") for post in authed] == ["Bearer tok123", "Bearer tok123"]
    assert all("auth" not in post["payload"] for post in session.posts)


def test_zabbix_70_production_also_uses_bearer(monkeypatch):
    client = _client(monkeypatch, FakeSession("7.0.3"))
    assert client.use_bearer_auth is True


def test_old_zabbix_keeps_auth_field_in_body(monkeypatch):
    session = FakeSession("6.0.25")
    client = _client(monkeypatch, session)
    client.get_hostgroups()
    last = session.posts[-1]
    assert last["payload"]["auth"] == "tok123"
    assert "Authorization" not in last["headers"]


def test_unknown_version_falls_back_to_body_auth(monkeypatch):
    session = FakeSession(None)
    client = _client(monkeypatch, session)
    assert client.api_version is None and client.use_bearer_auth is False
    client.get_hostgroups()
    assert session.posts[-1]["payload"]["auth"] == "tok123"


def test_login_error_is_still_an_api_error(monkeypatch):
    class BadLogin(FakeSession):
        def post(self, url, json, headers=None, timeout=None):
            if json["method"] == "user.login":
                return FakeResponse({"error": {"data": "Incorrect user name or password"}})
            return super().post(url, json, headers, timeout)

    with pytest.raises(ZabbixApiError):
        _client(monkeypatch, BadLogin("7.4.0"))
