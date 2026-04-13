from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
import tempfile

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .client import RetryPolicy, ZabbixJsonRpcClient
from .report import ReportService, ReportRow

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
REPORT_DIR = Path(tempfile.gettempdir()) / "sla-modern-pro"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="SLA Modern Pro", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

REPORT_CACHE: dict[str, dict[str, Any]] = {}


def _default_datetime_values() -> dict[str, str]:
    now = datetime.now()
    start = datetime(now.year, now.month, 1, 0, 0)
    end = now.replace(hour=23, minute=59, second=0, microsecond=0)
    return {
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end": end.strftime("%Y-%m-%dT%H:%M"),
    }


def _build_client(url: str, user: str, password: str) -> ZabbixJsonRpcClient:
    return ZabbixJsonRpcClient(
        url=url,
        user=user,
        password=password,
        timeout_seconds=120,
        retry_policy=RetryPolicy(attempts=6, base_delay_seconds=2, max_delay_seconds=30),
    )


def _parse_trigger_names(raw: str) -> list[str]:
    separators = ["\n", ";", ","]
    normalized = raw or ""
    for sep in separators:
        normalized = normalized.replace(sep, "|")
    return [part.strip() for part in normalized.split("|") if part.strip()]


def _base_context(request: Request, **extra: Any) -> dict[str, Any]:
    defaults = _default_datetime_values()
    context = {
        "request": request,
        "title": "SLA Modern Pro",
        "groups": [],
        "message": None,
        "error": None,
        "report": None,
        "download_id": None,
        "download_calc_id": None,
        "download_analytics_id": None,
        "defaults": defaults,
        "form": {
            "url": "https://zabbix.suporte.compwire.com.br/api_jsonrpc.php",
            "user": "",
            "password": "",
            "group_id": "",
            "trigger_names": "",
            "start": defaults["start"],
            "end": defaults["end"],
        },
    }
    context.update(extra)
    return context


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context=_base_context(request))


@app.post("/groups", response_class=HTMLResponse)
def load_groups(
    request: Request,
    url: str = Form(...),
    user: str = Form(...),
    password: str = Form(...),
    group_id: str = Form(""),
    trigger_names: str = Form(""),
    start: str = Form(...),
    end: str = Form(...),
) -> HTMLResponse:
    form = {
        "url": url,
        "user": user,
        "password": password,
        "group_id": group_id,
        "trigger_names": trigger_names,
        "start": start,
        "end": end,
    }
    try:
        client = _build_client(url, user, password)
        groups = client.get_hostgroups()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_base_context(
                request,
                groups=groups,
                message=f"{len(groups)} grupos carregados.",
                form=form,
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_base_context(request, error=str(exc), form=form),
        )


@app.post("/report", response_class=HTMLResponse)
def generate_report(
    request: Request,
    url: str = Form(...),
    user: str = Form(...),
    password: str = Form(...),
    group_id: str = Form(...),
    trigger_names: str = Form(""),
    start: str = Form(...),
    end: str = Form(...),
) -> HTMLResponse:
    form = {
        "url": url,
        "user": user,
        "password": password,
        "group_id": group_id,
        "trigger_names": trigger_names,
        "start": start,
        "end": end,
    }
    try:
        start_ts = int(datetime.fromisoformat(start).timestamp())
        end_ts = int(datetime.fromisoformat(end).timestamp())
        selected_trigger_names = _parse_trigger_names(trigger_names)
        client = _build_client(url, user, password)
        service = ReportService(client)
        report = service.generate_group_report(group_id, start_ts, end_ts, selected_trigger_names)
        report_id = uuid4().hex
        csv_path = REPORT_DIR / f"report_{report_id}.csv"
        calc_path = REPORT_DIR / f"report_{report_id}_base_calculo.csv"
        analytics_path = REPORT_DIR / f"report_{report_id}_analytics.csv"
        service.export_csv(report, csv_path)
        service.export_calculation_csv(report, calc_path)
        service.export_analytics_csv(report, analytics_path)
        REPORT_CACHE[report_id] = {
            "path": csv_path,
            "calc_path": calc_path,
            "analytics_path": analytics_path,
            "created_at": datetime.now(),
            "group_id": group_id,
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
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_base_context(
                request,
                message=f"Relatório gerado para o grupo {group_id}.",
                report={
                    "group_id": group_id,
                    "group_name": str(report.get("group_name", "")),
                    "start": start,
                    "end": end,
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
                form=form,
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_base_context(request, error=str(exc), form=form),
        )


@app.get("/download/{report_id}")
def download_report(report_id: str) -> FileResponse:
    item = REPORT_CACHE.get(report_id)
    if not item:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    path = Path(item["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(path, filename=path.name, media_type="text/csv")


@app.get("/download-calculation/{report_id}")
def download_calculation_report(report_id: str) -> FileResponse:
    item = REPORT_CACHE.get(report_id)
    if not item:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    calc_path = Path(item.get("calc_path", ""))
    if not calc_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo de base de cálculo não encontrado")
    return FileResponse(calc_path, filename=calc_path.name, media_type="text/csv")


@app.get("/download-analytics/{report_id}")
def download_analytics_report(report_id: str) -> FileResponse:
    item = REPORT_CACHE.get(report_id)
    if not item:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    analytics_path = Path(item.get("analytics_path", ""))
    if not analytics_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo de analytics não encontrado")
    return FileResponse(analytics_path, filename=analytics_path.name, media_type="text/csv")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def run() -> None:
    uvicorn.run("sla_modern_pro.web:app", host="0.0.0.0", port=8000, reload=True)
