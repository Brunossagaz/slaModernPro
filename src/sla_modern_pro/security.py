"""Controles de segurança da interface web: sessão, rate limiting, CSRF/Host e cabeçalhos."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import BoundedSemaphore, Lock
from urllib.parse import urlsplit
import secrets

from .config import settings

SESSION_COOKIE = "sla_session"

# Script só de arquivos próprios; estilos inline ficam liberados porque as barras de
# estado usam largura calculada em style="width: …%".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # same-origin: não envia a URL para outros sites. "no-referrer" faria o navegador mandar
    # "Origin: null" nos POST de formulário da própria app, que seriam barrados como CSRF.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def security_headers(is_https: bool) -> dict[str, str]:
    headers = dict(SECURITY_HEADERS)
    if is_https:
        # HSTS só faz sentido (e só é aceito pelo navegador) quando servido por HTTPS
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def host_allowed(host_header: str) -> bool:
    """Rejeita Host inesperado: protege contra DNS rebinding em serviço sem login local."""
    hostname = (urlsplit(f"//{host_header}").hostname or "").lower()
    return bool(hostname) and hostname in settings.allowed_hosts


def cross_site_request(method: str, origin: str | None, fetch_site: str | None, host_header: str) -> bool:
    """True se for um POST disparado por outro site (CSRF).

    Navegadores modernos sempre mandam Origin e/ou Sec-Fetch-Site em POST; clientes sem
    navegador (curl, testes) não mandam nenhum dos dois e não são vetor de CSRF.
    """
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return False
    if origin and origin != "null":
        return urlsplit(origin).netloc.lower() != host_header.lower()
    if fetch_site:
        # Sec-Fetch-Site é preenchido pelo navegador e não pode ser alterado por uma página
        return fetch_site not in {"same-origin", "none"}
    # "Origin: null" sem Sec-Fetch-Site (sandbox, file://, navegador antigo): na dúvida, bloqueia
    return origin == "null"


@dataclass(slots=True)
class ZabbixSession:
    """Credenciais do Zabbix guardadas só na memória do servidor, nunca devolvidas ao navegador."""

    url: str
    user: str
    password: str
    created_at: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ZabbixSession] = {}
        self._lock = Lock()

    def _expired(self, session: ZabbixSession, now: datetime) -> bool:
        return (
            now - session.last_seen > timedelta(minutes=settings.session_idle_minutes)
            or now - session.created_at > timedelta(hours=settings.session_max_hours)
        )

    def create(self, url: str, user: str, password: str) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._sessions[session_id] = ZabbixSession(url=url, user=user, password=password)
        return session_id

    def get(self, session_id: str | None) -> ZabbixSession | None:
        if not session_id:
            return None
        now = datetime.now()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._expired(session, now):
                self._sessions.pop(session_id, None)
                return None
            session.last_seen = now
            return session

    def delete(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def _prune(self) -> None:
        now = datetime.now()
        for session_id, session in list(self._sessions.items()):
            if self._expired(session, now):
                self._sessions.pop(session_id, None)


class LoginRateLimiter:
    """Bloqueio temporário após falhas seguidas de login no Zabbix (por IP e por usuário)."""

    def __init__(self) -> None:
        self._failures: dict[str, list[datetime]] = {}
        self._blocked_until: dict[str, datetime] = {}
        self._lock = Lock()

    @staticmethod
    def keys(client_ip: str, url: str, user: str) -> list[str]:
        return [f"ip:{client_ip}", f"user:{url.lower()}|{user.lower()}"]

    def blocked_for(self, keys: list[str]) -> int:
        """Segundos restantes de bloqueio (0 se liberado)."""
        now = datetime.now()
        with self._lock:
            remaining = [
                (until - now).total_seconds()
                for key in keys
                if (until := self._blocked_until.get(key)) and until > now
            ]
        return int(max(remaining)) + 1 if remaining else 0

    def record_failure(self, keys: list[str]) -> None:
        now = datetime.now()
        window = timedelta(minutes=settings.login_failure_window_minutes)
        with self._lock:
            for key in keys:
                recent = [ts for ts in self._failures.get(key, []) if now - ts <= window]
                recent.append(now)
                self._failures[key] = recent
                if len(recent) >= settings.login_max_failures:
                    self._blocked_until[key] = now + timedelta(minutes=settings.login_block_minutes)
                    self._failures[key] = []

    def record_success(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


sessions = SessionStore()
login_limiter = LoginRateLimiter()
# Cada relatório segura uma thread e faz muitas chamadas ao Zabbix: limita execuções simultâneas
report_slots = BoundedSemaphore(settings.max_concurrent_reports)


# Abaixo disso o "segredo" aparece por acaso em qualquer texto (ex.: senha "x" em "exceeded")
MIN_REDACT_LENGTH = 4


def redact(message: str, *secrets_to_hide: str) -> str:
    for secret in secrets_to_hide:
        if secret and len(secret) >= MIN_REDACT_LENGTH:
            message = message.replace(secret, "***")
    return message
