from __future__ import annotations

from datetime import datetime, timedelta
from itertools import islice
from math import ceil
from pathlib import Path
from typing import Any
from uuid import uuid4
import os
import tempfile

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .client import RetryPolicy, ZabbixApiError, ZabbixJsonRpcClient
from .config import settings
from .forms import ConnectionForm, SlaReportForm, StateReportForm, describe_validation_error
from .report import ReportService, ReportRow
from .security import (
    SESSION_COOKIE,
    cross_site_request,
    host_allowed,
    login_limiter,
    redact,
    report_slots,
    security_headers,
    sessions,
)
from .state_report import NO_DATA, STATE_TONES, StateReportService, StateRow, parse_item_patterns

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
REPORT_DIR = Path(tempfile.gettempdir()) / "sla-modern-pro"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Sem /docs, /redoc e /openapi.json: a aplicação é só a interface web
app = FastAPI(title="SLA Modern Pro", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

REPORT_CACHE: dict[str, dict[str, Any]] = {}
MAX_INTERVALS_ON_SCREEN = 2000
# Os CSVs ficam no tmpfs do container, que conta no limite de memória: descarta os antigos.
REPORT_TTL = timedelta(hours=6)


class UserFacingError(Exception):
    """Erro com mensagem pronta para a tela."""


@app.middleware("http")
async def security_middleware(request: Request, call_next: Any) -> Response:
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return PlainTextResponse("Host não permitido.", status_code=400)
    if cross_site_request(
        request.method, request.headers.get("origin"), request.headers.get("sec-fetch-site"), host
    ):
        return PlainTextResponse("Requisição de outro site bloqueada.", status_code=403)

    response = await call_next(request)
    for name, value in security_headers(request.url.scheme == "https").items():
        response.headers.setdefault(name, value)
    if not request.url.path.startswith("/static/"):
        # Páginas e CSVs têm dados do cliente: nada de cache no navegador ou em proxies
        response.headers["Cache-Control"] = "no-store"
    return response


def _prune_report_cache() -> None:
    cutoff = datetime.now() - REPORT_TTL
    for report_id, entry in list(REPORT_CACHE.items()):
        if entry.get("created_at", cutoff) > cutoff:
            continue
        _drop_report(report_id)


def _drop_report(report_id: str) -> None:
    entry = REPORT_CACHE.pop(report_id, None) or {}
    for key, value in entry.items():
        if key.endswith("path"):
            Path(value).unlink(missing_ok=True)


def _default_datetime_values() -> dict[str, str]:
    now = datetime.now()
    start = datetime(now.year, now.month, 1, 0, 0)
    end = now.replace(hour=23, minute=59, second=0, microsecond=0)
    return {
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end": end.strftime("%Y-%m-%dT%H:%M"),
    }


def _build_client(url: str, user: str, password: str, interactive: bool = False) -> ZabbixJsonRpcClient:
    if interactive:
        # Conexão pela interface: falha rápido em vez do retry longo usado nos relatórios
        return ZabbixJsonRpcClient(
            url=url,
            user=user,
            password=password,
            timeout_seconds=20,
            retry_policy=RetryPolicy(attempts=2, base_delay_seconds=1, max_delay_seconds=1),
        )
    return ZabbixJsonRpcClient(
        url=url,
        user=user,
        password=password,
        timeout_seconds=120,
        retry_policy=RetryPolicy(attempts=6, base_delay_seconds=2, max_delay_seconds=30),
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "desconhecido"


def _resolve_credentials(request: Request, form: ConnectionForm) -> tuple[str, str, str]:
    """Usa a senha digitada ou, se vazia, a da sessão ativa (que fica só no servidor)."""
    if form.password:
        return form.url, form.user, form.password
    session = sessions.get(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise UserFacingError("Sessão expirada ou inexistente: informe a senha e conecte novamente.")
    if session.url != form.url or session.user != form.user:
        raise UserFacingError("URL ou usuário diferentes da conexão ativa: informe a senha para conectar com eles.")
    return session.url, session.user, session.password


def _login(request: Request, url: str, user: str, password: str, interactive: bool = False) -> ZabbixJsonRpcClient:
    keys = login_limiter.keys(_client_ip(request), url, user)
    wait_seconds = login_limiter.blocked_for(keys)
    if wait_seconds:
        raise UserFacingError(
            f"Muitas tentativas de login com falha. Tente novamente em {ceil(wait_seconds / 60)} min."
        )
    try:
        client = _build_client(url, user, password, interactive=interactive)
    except ZabbixApiError:
        login_limiter.record_failure(keys)
        raise
    login_limiter.record_success(keys)
    return client


def _open_session(request: Request, url: str, user: str, password: str) -> str:
    """Reaproveita a sessão se as credenciais são as mesmas; se mudaram, gera um id novo
    (evita fixação de sessão) e leva junto os relatórios da sessão anterior."""
    old_id = request.cookies.get(SESSION_COOKIE)
    old_session = sessions.get(old_id)
    if old_session and old_session.url == url and old_session.user == user and old_session.password == password:
        return str(old_id)
    new_id = sessions.create(url, user, password)
    if old_session:
        for entry in REPORT_CACHE.values():
            if entry.get("session_id") == old_id:
                entry["session_id"] = new_id
        sessions.delete(old_id)
    return new_id


def _set_session_cookie(request: Request, response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=settings.session_max_hours * 3600,
        path="/",
        httponly=True,
        secure=settings.cookie_secure or request.url.scheme == "https",
        samesite="strict",
    )


def _error_message(exc: Exception, password: str = "") -> str:
    if isinstance(exc, ValidationError):
        return describe_validation_error(exc)
    return redact(str(exc), password)


def _form_values(raw: dict[str, Any], mode: str) -> dict[str, Any]:
    # A senha nunca volta para o HTML
    return {
        "url": str(raw.get("url", "")),
        "user": str(raw.get("user", "")),
        "group_id": str(raw.get("group_id", "")),
        "trigger_names": str(raw.get("trigger_names", "")),
        "item_keys": str(raw.get("item_keys", "")),
        "max_gap_minutes": str(raw.get("max_gap_minutes", "10")),
        "mode": mode,
        "start": str(raw.get("start", "")),
        "end": str(raw.get("end", "")),
    }


def _base_context(request: Request, session_id: str | None = None, **extra: Any) -> dict[str, Any]:
    defaults = _default_datetime_values()
    # Na resposta do login o cookie novo ainda não veio do navegador: usa o id recém-criado
    session = sessions.get(session_id or request.cookies.get(SESSION_COOKIE))
    context = {
        "request": request,
        "title": "SLA Modern Pro",
        "message": None,
        "error": None,
        "report": None,
        "download_id": None,
        "download_calc_id": None,
        "download_analytics_id": None,
        "state_report": None,
        "state_download_id": None,
        "defaults": defaults,
        "connected": session is not None,
        "form": {
            "url": session.url if session else settings.default_zabbix_url,
            "user": session.user if session else "",
            "group_id": "",
            "trigger_names": "",
            "item_keys": "",
            "max_gap_minutes": "10",
            "mode": "sla",
            "start": defaults["start"],
            "end": defaults["end"],
        },
    }
    context.update(extra)
    return context


async def _read_form(request: Request) -> dict[str, Any]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _render(request: Request, session_id: str | None = None, **extra: Any) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request, name="index.html", context=_base_context(request, session_id, **extra)
    )
    if session_id:
        _set_session_cookie(request, response, session_id)
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _render(request)


@app.post("/api/groups")
async def api_groups(request: Request) -> JSONResponse:
    raw = await _read_form(request)
    password = raw.get("password", "")
    try:
        form = ConnectionForm.model_validate(raw)
        url, user, password = _resolve_credentials(request, form)
        groups, session_id = await _run_blocking(_load_groups, request, url, user, password)
    except Exception as exc:
        return JSONResponse({"error": _error_message(exc, password)}, status_code=400)
    response = JSONResponse(
        {"groups": [{"groupid": str(g["groupid"]), "name": str(g["name"])} for g in groups]}
    )
    _set_session_cookie(request, response, session_id)
    return response


def _load_groups(request: Request, url: str, user: str, password: str) -> tuple[list[dict[str, Any]], str]:
    with _login(request, url, user, password, interactive=True) as client:
        groups = client.get_hostgroups()
    return groups, _open_session(request, url, user, password)


@app.post("/api/logout")
def api_logout(request: Request) -> JSONResponse:
    session_id = request.cookies.get(SESSION_COOKIE)
    for report_id, entry in list(REPORT_CACHE.items()):
        if session_id and entry.get("session_id") == session_id:
            _drop_report(report_id)
    sessions.delete(session_id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


async def _run_blocking(func: Any, *args: Any) -> Any:
    # Chamadas ao Zabbix são bloqueantes: rodam no pool de threads para não travar o servidor
    return await run_in_threadpool(func, *args)


def _acquire_report_slot() -> None:
    if not report_slots.acquire(blocking=False):
        raise UserFacingError(
            f"Já há {settings.max_concurrent_reports} relatório(s) em execução. Aguarde terminar e tente de novo."
        )


@app.post("/report", response_class=HTMLResponse)
async def generate_report(request: Request) -> HTMLResponse:
    raw = await _read_form(request)
    password = raw.get("password", "")
    try:
        form = SlaReportForm.model_validate(raw)
        url, user, password = _resolve_credentials(request, form)
        return await _run_blocking(_generate_sla, request, form, url, user, password, raw)
    except Exception as exc:
        return _render(request, error=_error_message(exc, password), form=_form_values(raw, "sla"))


def _generate_sla(
    request: Request, form: SlaReportForm, url: str, user: str, password: str, raw: dict[str, Any]
) -> HTMLResponse:
    _acquire_report_slot()
    try:
        with _login(request, url, user, password) as client:
            service = ReportService(client)
            report = service.generate_group_report(form.group_id, form.start_ts, form.end_ts, form.trigger_list())
            session_id = _open_session(request, url, user, password)
            _prune_report_cache()
            report_id = uuid4().hex
            csv_path = REPORT_DIR / f"report_{report_id}.csv"
            calc_path = REPORT_DIR / f"report_{report_id}_base_calculo.csv"
            analytics_path = REPORT_DIR / f"report_{report_id}_analytics.csv"
            service.export_csv(report, csv_path)
            service.export_calculation_csv(report, calc_path)
            service.export_analytics_csv(report, analytics_path)
    finally:
        report_slots.release()
    REPORT_CACHE[report_id] = {
        "path": csv_path,
        "calc_path": calc_path,
        "analytics_path": analytics_path,
        "created_at": datetime.now(),
        "group_id": form.group_id,
        "session_id": session_id,
    }

    rows: list[dict[str, str]] = []
    report_rows = report.get("rows", [])
    if isinstance(report_rows, list):
        for row in report_rows:
            if not isinstance(row, ReportRow):
                continue
            rows.append(
                {
                    "host_name": row.host_name,
                    "downtime": service.format_seconds(row.downtime_seconds),
                    "uptime": service.format_seconds(row.uptime_seconds),
                    "availability": f"{row.availability:.1f}%",
                    "mtta": service.format_seconds(row.mtta_seconds),
                    "mttr": service.format_seconds(row.mttr_seconds),
                }
            )
    return _render(
        request,
        session_id=session_id,
        message=f"Relatório gerado para o grupo {form.group_id}.",
        report={
            "group_id": form.group_id,
            "group_name": str(report.get("group_name", "")),
            "start": raw.get("start", ""),
            "end": raw.get("end", ""),
            "rows": rows,
            "total_period": int(str(report.get("total_period", 0))),
            "selected_triggers": report.get("selected_triggers", []),
            "matched_triggers": report.get("matched_triggers", []),
            "problem_stats": report.get("problem_stats", {}),
            "alarm_analytics": report.get("alarm_analytics", {}),
            "audit": report.get("audit", {}),
        },
        download_id=report_id,
        download_calc_id=report_id,
        download_analytics_id=report_id,
        form=_form_values(raw, "sla"),
    )


@app.post("/state-report", response_class=HTMLResponse)
async def generate_state_report(request: Request) -> HTMLResponse:
    raw = await _read_form(request)
    password = raw.get("password", "")
    try:
        form = StateReportForm.model_validate(raw)
        url, user, password = _resolve_credentials(request, form)
        return await _run_blocking(_generate_state, request, form, url, user, password, raw)
    except Exception as exc:
        return _render(request, error=_error_message(exc, password), form=_form_values(raw, "state"))


def _generate_state(
    request: Request, form: StateReportForm, url: str, user: str, password: str, raw: dict[str, Any]
) -> HTMLResponse:
    start_ts, end_ts = form.start_ts, form.end_ts
    max_gap_seconds = form.max_gap_minutes * 60
    _acquire_report_slot()
    try:
        with _login(request, url, user, password) as client:
            service = StateReportService(client)
            report = service.generate_state_report(
                form.group_id, start_ts, end_ts, parse_item_patterns(form.item_keys), max_gap_seconds
            )
            session_id = _open_session(request, url, user, password)
            _prune_report_cache()
            report_id = uuid4().hex
            state_path = REPORT_DIR / f"state_{report_id}.csv"
            intervals_path = REPORT_DIR / f"state_{report_id}_intervalos.csv"
            service.export_csv(report, state_path)
            service.export_intervals_csv(report, intervals_path)
    finally:
        report_slots.release()
    REPORT_CACHE[report_id] = {
        "state_path": state_path,
        "state_intervals_path": intervals_path,
        "created_at": datetime.now(),
        "group_id": form.group_id,
        "session_id": session_id,
    }

    states = [str(state) for state in report.get("states", [])]  # type: ignore[union-attr]
    total_period = int(str(report.get("total_period", 0)))
    totals = report.get("totals", {})
    totals = totals if isinstance(totals, dict) else {}

    def _cell(state: str, seconds: int, total: int, samples: int = 0) -> dict[str, Any]:
        pct = seconds / total * 100.0 if total else 0.0
        return {
            "state": state,
            "tone": STATE_TONES.get(state, "other"),
            "fmt": service.format_seconds(seconds),
            "pct": pct,
            "pct_fmt": f"{pct:.2f}%",
            "seconds": seconds,
            "samples": samples,
        }

    rows: list[dict[str, Any]] = []
    report_rows = report.get("rows", [])
    for row in report_rows if isinstance(report_rows, list) else []:
        if not isinstance(row, StateRow):
            continue
        rows.append(
            {
                "host_name": row.host_name,
                "item_name": row.item_name,
                "cells": [
                    _cell(state, row.durations.get(state, 0), row.total_seconds, row.samples.get(state, 0))
                    for state in states
                ],
                "coverage": f"{row.coverage:.2f}%",
            }
        )

    grand_total = sum(int(v) for v in totals.values())
    effective_end_ts = int(str(report.get("effective_end_ts", end_ts)))
    return _render(
        request,
        session_id=session_id,
        message=f"Relatório de status gerado para o grupo {form.group_id}.",
        state_report={
            "group_id": form.group_id,
            "group_name": str(report.get("group_name", "")),
            "start": raw.get("start", ""),
            "end": raw.get("end", ""),
            "effective_end": datetime.fromtimestamp(effective_end_ts).strftime("%Y-%m-%dT%H:%M"),
            "clipped": effective_end_ts < end_ts,
            "total_period_fmt": service.format_seconds(total_period),
            "max_gap_fmt": service.format_seconds(max_gap_seconds),
            "states": states,
            "no_data_state": NO_DATA,
            "totals": [_cell(state, int(totals.get(state, 0)), grand_total) for state in states],
            "rows": rows,
            # Só os que aparecem na tela; a lista completa vai no CSV de intervalos
            "intervals": list(islice(service.iter_interval_rows(report), MAX_INTERVALS_ON_SCREEN)),
            "interval_count": int(str(report.get("interval_count", 0))),
            "max_intervals": MAX_INTERVALS_ON_SCREEN,
            "warnings": report.get("warnings", []),
            "skipped": report.get("skipped", []),
        },
        state_download_id=report_id,
        form=_form_values(raw, "state"),
    )


def _cached_file(request: Request, report_id: str, key: str, missing_detail: str) -> FileResponse:
    item = REPORT_CACHE.get(report_id)
    # Só quem gerou o relatório (mesma sessão) pode baixá-lo; 404 para não revelar que existe
    session_id = request.cookies.get(SESSION_COOKIE)
    if not item or not session_id or item.get("session_id") != session_id or sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    path = Path(item.get(key, ""))
    if not item.get(key) or not path.exists():
        raise HTTPException(status_code=404, detail=missing_detail)
    return FileResponse(path, filename=path.name, media_type="text/csv")


@app.get("/download/{report_id}")
def download_report(request: Request, report_id: str) -> FileResponse:
    return _cached_file(request, report_id, "path", "Arquivo não encontrado")


@app.get("/download-calculation/{report_id}")
def download_calculation_report(request: Request, report_id: str) -> FileResponse:
    return _cached_file(request, report_id, "calc_path", "Arquivo de base de cálculo não encontrado")


@app.get("/download-analytics/{report_id}")
def download_analytics_report(request: Request, report_id: str) -> FileResponse:
    return _cached_file(request, report_id, "analytics_path", "Arquivo de analytics não encontrado")


@app.get("/download-state/{report_id}")
def download_state_report(request: Request, report_id: str) -> FileResponse:
    return _cached_file(request, report_id, "state_path", "Arquivo de tempo por estado não encontrado")


@app.get("/download-state-intervals/{report_id}")
def download_state_intervals(request: Request, report_id: str) -> FileResponse:
    return _cached_file(request, report_id, "state_intervals_path", "Arquivo de intervalos não encontrado")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def run() -> None:
    reload = os.environ.get("SLA_MODERN_PRO_RELOAD", "0") == "1"
    # Fora do Docker o padrão é só localhost: a aplicação não tem login próprio
    host = os.environ.get("SLA_MODERN_PRO_HOST", "127.0.0.1")
    port = int(os.environ.get("SLA_MODERN_PRO_PORT", "8000"))
    uvicorn.run("sla_modern_pro.web:app", host=host, port=port, reload=reload)
