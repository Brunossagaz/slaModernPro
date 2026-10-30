"""Schemas de validação das entradas da interface web (validados sempre no servidor)."""
from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import settings

MAX_PERIOD_DAYS = 400
MAX_ITEM_PATTERNS = 50
MAX_TRIGGER_NAMES = 200
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _http_allowed(host: str) -> bool:
    if settings.allow_insecure_zabbix or host in _LOCAL_HOSTS:
        return True
    return any(fnmatchcase(host, pattern) for pattern in settings.insecure_zabbix_hosts)


def validate_zabbix_url(raw: str) -> str:
    """Só aceita a URL da API JSON-RPC do Zabbix, em HTTPS e (se configurado) em hosts permitidos.

    A aplicação faz requisições do servidor para essa URL, então sem essa checagem ela
    poderia ser usada para sondar a rede interna (SSRF).
    """
    url = raw.strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise ValueError("URL da API inválida: use https://<servidor>/api_jsonrpc.php")
    if parts.username or parts.password:
        raise ValueError("A URL da API não pode conter usuário ou senha.")
    if parts.scheme == "http" and not _http_allowed(host):
        raise ValueError("Use HTTPS na URL da API do Zabbix: por HTTP a senha trafega sem criptografia.")
    if not parts.path.rstrip("/").endswith("api_jsonrpc.php"):
        raise ValueError("A URL precisa apontar para a API do Zabbix (terminar em /api_jsonrpc.php).")
    if parts.query or parts.fragment:
        raise ValueError("A URL da API não pode ter parâmetros (?...) nem fragmento (#...).")
    allowed = settings.zabbix_allowed_hosts
    if allowed and not any(fnmatchcase(host, pattern) for pattern in allowed):
        raise ValueError(f"O servidor '{host}' não está na lista de servidores Zabbix permitidos.")
    return url


class ConnectionForm(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(max_length=2048)
    user: str = Field(min_length=1, max_length=255)
    # Senha não é normalizada (espaços podem fazer parte dela). Vazia = usar a sessão atual.
    password: str = Field(default="", max_length=1024)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_zabbix_url(value)

    @field_validator("user")
    @classmethod
    def _user(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe o usuário.")
        return value


class ReportForm(ConnectionForm):
    group_id: str = Field(pattern=r"^\d{1,20}$")
    start: datetime
    end: datetime
    trigger_names: str = Field(default="", max_length=20_000)

    @model_validator(mode="after")
    def _period(self) -> "ReportForm":
        if self.end <= self.start:
            raise ValueError("O fim do período precisa ser posterior ao início.")
        if (self.end - self.start).days > MAX_PERIOD_DAYS:
            raise ValueError(f"O período pode ter no máximo {MAX_PERIOD_DAYS} dias.")
        return self

    @field_validator("group_id", mode="before")
    @classmethod
    def _group(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @property
    def start_ts(self) -> int:
        return int(self.start.timestamp())

    @property
    def end_ts(self) -> int:
        return int(self.end.timestamp())


class SlaReportForm(ReportForm):
    def trigger_list(self) -> list[str]:
        normalized = self.trigger_names
        for sep in ("\n", ";", ","):
            normalized = normalized.replace(sep, "|")
        names = [part.strip() for part in normalized.split("|") if part.strip()]
        if len(names) > MAX_TRIGGER_NAMES:
            raise ValueError(f"Informe no máximo {MAX_TRIGGER_NAMES} triggers.")
        return names


class StateReportForm(ReportForm):
    item_keys: str = Field(default="", max_length=10_000, validate_default=True)
    max_gap_minutes: int = Field(default=10, ge=1, le=1440)

    @field_validator("item_keys")
    @classmethod
    def _keys(cls, value: str) -> str:
        from .state_report import parse_item_patterns

        patterns = parse_item_patterns(value)
        if not patterns:
            raise ValueError("Informe ao menos uma chave de item.")
        if len(patterns) > MAX_ITEM_PATTERNS:
            raise ValueError(f"Informe no máximo {MAX_ITEM_PATTERNS} chaves.")
        if any(len(pattern) > 255 for pattern in patterns):
            raise ValueError("Cada chave pode ter no máximo 255 caracteres.")
        return value


_FIELD_LABELS = {
    "url": "URL da API",
    "user": "Usuário",
    "password": "Senha",
    "group_id": "Grupo",
    "start": "Início",
    "end": "Fim",
    "trigger_names": "Triggers",
    "item_keys": "Chave do item",
    "max_gap_minutes": "Tolerância sem dados",
}


def _translate(error: dict[str, Any]) -> str:
    ctx = error.get("ctx") or {}
    kind = error.get("type", "")
    if kind == "value_error":
        return str(error.get("msg", "")).removeprefix("Value error, ")
    if kind == "missing":
        return "campo obrigatório."
    if kind == "string_pattern_mismatch":
        return "formato inválido."
    if kind == "string_too_short":
        return "campo obrigatório."
    if kind == "string_too_long":
        return f"no máximo {ctx.get('max_length')} caracteres."
    if kind in {"int_parsing", "int_from_float", "int_type"}:
        return "precisa ser um número inteiro."
    if kind == "greater_than_equal":
        return f"precisa ser no mínimo {ctx.get('ge')}."
    if kind == "less_than_equal":
        return f"precisa ser no máximo {ctx.get('le')}."
    if kind.startswith(("datetime", "date")):
        return "data/hora inválida."
    return "valor inválido."


def describe_validation_error(exc: ValidationError) -> str:
    messages = []
    for error in exc.errors():
        field_name = str(error["loc"][0]) if error.get("loc") else ""
        message = _translate(dict(error))
        label = _FIELD_LABELS.get(field_name)
        messages.append(f"{label}: {message}" if label else message)
    return " ".join(messages)
