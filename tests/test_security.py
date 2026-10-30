from pathlib import Path
from threading import BoundedSemaphore
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from sla_modern_pro import security, web
from sla_modern_pro.client import ZabbixApiError
from sla_modern_pro.csv_utils import sanitize_cell
from sla_modern_pro.forms import validate_zabbix_url

URL = "https://zabbix.test/api_jsonrpc.php"
PASSWORD = "S3nh@-Muito-Secreta"


class FakeZabbix:
    host_name = "cdp-prod"

    def __init__(self, password):
        if password != PASSWORD:
            raise ZabbixApiError("Incorrect user name or password or account is temporarily blocked.")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def get_hostgroups(self):
        return [{"groupid": "5", "name": "Clientes/ACME", "internal": "não deve vazar"}]

    def get_group_tree_ids(self, group_id):
        return "Clientes/ACME", [int(group_id)]

    def get_items(self, group_ids, key, include_valuemap=True):
        host = [{"hostid": "1", "name": self.host_name, "status": "0"}]
        return [{"itemid": "10", "name": "Status", "key_": key, "value_type": "1", "history": "90d", "status": "0", "hosts": host}]

    def get_history(self, item_ids, value_type, time_from=None, time_till=None, sortorder="ASC", limit=None):
        return [{"itemid": "10", "clock": str(time_from), "value": "GOOD"}]

    def get_hosts(self, group_ids):
        return [{"hostid": "1", "name": self.host_name, "status": "0", "triggers": [{"triggerid": "7", "description": "CPU alta"}]}]

    def get_trigger_details(self, trigger_ids):
        return [{"triggerid": "7", "status": "0", "priority": "4", "description": "CPU alta", "items": []}]

    def get_events(self, trigger_ids, time_from=None, time_till=None, sortorder="ASC", include_acknowledges=False):
        if time_from is None:
            return []
        return [
            {"eventid": "1", "objectid": "7", "clock": str(time_from + 60), "value": "1", "acknowledges": []},
            {"eventid": "2", "objectid": "7", "clock": str(time_from + 660), "value": "0", "acknowledges": []},
        ]


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(web, "_build_client", lambda url, user, password, interactive=False: FakeZabbix(password))
    monkeypatch.setattr(web.settings, "allowed_hosts", ["testserver"])
    monkeypatch.setattr(web.settings, "zabbix_allowed_hosts", [])
    monkeypatch.setattr(web, "sessions", security.SessionStore())
    monkeypatch.setattr(web, "login_limiter", security.LoginRateLimiter())
    monkeypatch.setattr(web, "report_slots", BoundedSemaphore(2))
    return TestClient(web.app)


def _report_form(**overrides):
    data = {
        "url": URL, "user": "cs", "password": PASSWORD, "group_id": "5",
        "item_keys": "cluster.status", "max_gap_minutes": "10",
        "start": "2026-09-01T00:00", "end": "2026-09-02T00:00",
    }
    data.update(overrides)
    return data


def _report_id(html):
    line = next(line for line in html.splitlines() if "/download-state/" in line)
    return line.split("/download-state/")[1].split('"')[0]


def test_security_headers_and_no_api_docs(app_client):
    response = app_client.get("/")
    headers = response.headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in headers  # só faz sentido em HTTPS
    assert "<script>" not in response.text  # sem script inline (CSP)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert app_client.get(path).status_code == 404


def test_hsts_only_over_https(app_client):
    https_client = TestClient(web.app, base_url="https://testserver")
    assert "max-age=" in https_client.get("/health").headers["strict-transport-security"]


def test_unknown_host_header_is_rejected(app_client):
    assert app_client.get("/", headers={"host": "attacker.example"}).status_code == 400


def test_cross_site_post_is_blocked(app_client):
    blocked = app_client.post("/api/groups", data=_report_form(), headers={"origin": "https://evil.example"})
    assert blocked.status_code == 403
    fetch_blocked = app_client.post("/api/groups", data=_report_form(), headers={"sec-fetch-site": "cross-site"})
    assert fetch_blocked.status_code == 403
    same_origin = app_client.post("/api/groups", data=_report_form(), headers={"origin": "http://testserver"})
    assert same_origin.status_code == 200
    # Envio de formulário da própria página: o Chrome pode mandar "Origin: null" com Sec-Fetch-Site same-origin
    own_form = app_client.post(
        "/state-report", data=_report_form(), headers={"origin": "null", "sec-fetch-site": "same-origin"}
    )
    assert own_form.status_code == 200
    sandboxed = app_client.post("/api/groups", data=_report_form(), headers={"origin": "null"})
    assert sandboxed.status_code == 403
    null_cross_site = app_client.post(
        "/api/groups", data=_report_form(), headers={"origin": "null", "sec-fetch-site": "cross-site"}
    )
    assert null_cross_site.status_code == 403


def test_password_is_never_sent_back_and_cookie_is_hardened(app_client):
    response = app_client.post("/state-report", data=_report_form())
    assert response.status_code == 200
    assert PASSWORD not in response.text
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=" in cookie

    groups = app_client.post("/api/groups", data=_report_form()).json()
    assert groups == {"groups": [{"groupid": "5", "name": "Clientes/ACME"}]}  # só os campos necessários


def test_session_reuses_password_server_side_and_logout_clears_it(app_client):
    app_client.post("/api/groups", data=_report_form())
    without_password = app_client.post("/state-report", data=_report_form(password=""))
    assert "Tempo total por estado" in without_password.text

    other_user = app_client.post("/state-report", data=_report_form(password="", user="outro"))
    assert "diferentes da conexão ativa" in other_user.text

    app_client.post("/api/logout")
    after_logout = app_client.post("/state-report", data=_report_form(password=""))
    assert "Sessão expirada" in after_logout.text


def test_download_requires_the_session_that_generated_it(app_client):
    report_id = _report_id(app_client.post("/state-report", data=_report_form()).text)
    assert app_client.get(f"/download-state/{report_id}").status_code == 200

    stranger = TestClient(web.app)
    assert stranger.get(f"/download-state/{report_id}").status_code == 404

    app_client.post("/api/logout")
    assert app_client.get(f"/download-state/{report_id}").status_code == 404


def test_login_is_blocked_after_repeated_failures(app_client, monkeypatch):
    monkeypatch.setattr(web.settings, "login_max_failures", 3)
    for _ in range(3):
        failed = app_client.post("/api/groups", data=_report_form(password="errada"))
        assert failed.status_code == 400
        assert "errada" not in failed.text
    blocked = app_client.post("/api/groups", data=_report_form())
    assert blocked.status_code == 400
    assert "Muitas tentativas" in blocked.json()["error"]


def test_inputs_are_validated_on_the_server(app_client):
    cases = {
        "group_id": ("1 OR 1=1", "Grupo: formato inválido"),
        "end": ("2028-01-01T00:00", "no máximo 400 dias"),
        "max_gap_minutes": ("0", "Tolerância sem dados: precisa ser no mínimo 1"),
        "start": ("ontem", "Início: data/hora inválida"),
        "item_keys": ("x\n" * 60, "no máximo 50 chaves"),
        "url": ("http://169.254.169.254/latest/meta-data", "HTTPS"),
    }
    for field, (value, expected) in cases.items():
        response = app_client.post("/state-report", data=_report_form(**{field: value}))
        assert expected in response.text, field


def test_zabbix_url_rules(monkeypatch):
    monkeypatch.setattr(security.settings, "zabbix_allowed_hosts", ["*.empresa.com.br"])
    monkeypatch.setattr(security.settings, "allow_insecure_zabbix", False)
    assert validate_zabbix_url("https://zbx.empresa.com.br/api_jsonrpc.php")
    for bad in (
        "https://outro.com/api_jsonrpc.php",  # fora da lista
        "http://zbx.empresa.com.br/api_jsonrpc.php",  # sem TLS
        "https://user:pw@zbx.empresa.com.br/api_jsonrpc.php",  # credencial na URL
        "https://zbx.empresa.com.br/admin",  # não é a API
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            validate_zabbix_url(bad)


def test_concurrent_report_limit(app_client, monkeypatch):
    slots = BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(web, "report_slots", slots)
    response = app_client.post("/state-report", data=_report_form())
    assert "relatório(s) em execução" in response.text


def test_csv_cells_cannot_become_formulas(app_client, monkeypatch):
    monkeypatch.setattr(FakeZabbix, "host_name", '=HYPERLINK("http://evil","x")')
    report_id = _report_id(app_client.post("/state-report", data=_report_form()).text)
    csv_text = app_client.get(f"/download-state/{report_id}").text
    assert "'=HYPERLINK" in csv_text
    assert "\n=HYPERLINK" not in csv_text and ";=HYPERLINK" not in csv_text
    assert sanitize_cell("-1+2") == "'-1+2"
    assert sanitize_cell("GOOD") == "GOOD"
    assert sanitize_cell(12) == 12


def test_error_messages_hide_the_password():
    assert security.redact(f"falha ao usar {PASSWORD}", PASSWORD) == "falha ao usar ***"
    # senha curtíssima não pode picotar a mensagem inteira
    assert security.redact("Max retries exceeded", "x") == "Max retries exceeded"


def test_sla_report_route_and_its_csv_exports(app_client, monkeypatch):
    monkeypatch.setattr(FakeZabbix, "host_name", "@SUM(1+1)")
    response = app_client.post("/report", data=_report_form())
    assert "Relatório gerado para o grupo 5" in response.text
    assert PASSWORD not in response.text
    report_id = next(l for l in response.text.splitlines() if "/download/" in l).split("/download/")[1].split('"')[0]
    for path in ("download", "download-calculation", "download-analytics"):
        exported = app_client.get(f"/{path}/{report_id}")
        assert exported.status_code == 200, path
        assert "'@SUM(1+1)" in exported.text, path
    assert TestClient(web.app).get(f"/download/{report_id}").status_code == 404


def test_http_is_allowed_only_for_listed_local_servers(monkeypatch):
    monkeypatch.setattr(security.settings, "zabbix_allowed_hosts", [])
    monkeypatch.setattr(security.settings, "allow_insecure_zabbix", False)
    monkeypatch.setattr(security.settings, "insecure_zabbix_hosts", ["zabbix-frontend"])

    assert validate_zabbix_url("http://zabbix-frontend:8080/api_jsonrpc.php")
    assert validate_zabbix_url("https://zbx.empresa.com.br/api_jsonrpc.php")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_zabbix_url("http://zbx.empresa.com.br/api_jsonrpc.php")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_zabbix_url("http://zabbix-frontend.evil.com/api_jsonrpc.php")
