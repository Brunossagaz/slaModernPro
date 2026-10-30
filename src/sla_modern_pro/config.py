"""Configuração lida de variáveis de ambiente (nada sensível fica no código-fonte).

Veja `.env.example` na raiz do projeto para a lista completa.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    # URL sugerida no formulário (só preenchimento; não é segredo, mas é dado interno)
    default_zabbix_url: str = ""
    # Hosts do Zabbix que a aplicação aceita chamar (evita SSRF). Aceita curinga: *.empresa.com.br
    # Vazio = qualquer host (só recomendado em uso estritamente local).
    zabbix_allowed_hosts: list[str] = field(default_factory=list)
    # Permite http:// para o Zabbix (a senha trafegaria sem criptografia)
    allow_insecure_zabbix: bool = False
    # Servidores específicos liberados para http:// (ex.: Zabbix local na rede Docker).
    # Os demais continuam exigindo HTTPS. Aceita curinga.
    insecure_zabbix_hosts: list[str] = field(default_factory=list)
    # Valores aceitos no cabeçalho Host (protege contra DNS rebinding)
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1"])
    # Força o atributo Secure no cookie mesmo sem ver HTTPS na requisição (ex.: atrás de proxy TLS)
    cookie_secure: bool = False
    session_idle_minutes: int = 120
    session_max_hours: int = 12
    max_concurrent_reports: int = 2
    login_max_failures: int = 5
    login_failure_window_minutes: int = 10
    login_block_minutes: int = 15

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            default_zabbix_url=os.environ.get("SLA_ZABBIX_DEFAULT_URL", "").strip(),
            zabbix_allowed_hosts=_env_list("SLA_ZABBIX_ALLOWED_HOSTS"),
            allow_insecure_zabbix=_env_bool("SLA_ALLOW_INSECURE_ZABBIX"),
            insecure_zabbix_hosts=_env_list("SLA_INSECURE_ZABBIX_HOSTS"),
            allowed_hosts=_env_list("SLA_ALLOWED_HOSTS", "localhost,127.0.0.1"),
            cookie_secure=_env_bool("SLA_COOKIE_SECURE"),
            session_idle_minutes=_env_int("SLA_SESSION_IDLE_MINUTES", 120),
            session_max_hours=_env_int("SLA_SESSION_MAX_HOURS", 12),
            max_concurrent_reports=max(1, _env_int("SLA_MAX_CONCURRENT_REPORTS", 2)),
            login_max_failures=max(1, _env_int("SLA_LOGIN_MAX_FAILURES", 5)),
            login_failure_window_minutes=max(1, _env_int("SLA_LOGIN_FAILURE_WINDOW_MINUTES", 10)),
            login_block_minutes=max(1, _env_int("SLA_LOGIN_BLOCK_MINUTES", 15)),
        )


settings = Settings.from_env()
