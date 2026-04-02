from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Any
import csv

from .client import ZabbixJsonRpcClient


@dataclass(slots=True)
class ReportRow:
    host_name: str
    downtime_seconds: int
    uptime_seconds: int
    availability: float
    mtta_seconds: int
    mttr_seconds: int


class ReportService:
    def __init__(self, client: ZabbixJsonRpcClient) -> None:
        self.client = client

    @staticmethod
    def chunked(values: list[str], size: int) -> Iterable[list[str]]:
        for index in range(0, len(values), size):
            yield values[index:index + size]

    @staticmethod
    def format_seconds(seconds: int) -> str:
        if seconds <= 0:
            return "00:00:00"
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if days > 0:
            return f"{days} days, {hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not intervals:
            return []

        ordered = sorted(intervals, key=lambda item: item[0])
        merged: list[tuple[int, int]] = [ordered[0]]

        for start, end in ordered[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))

        return merged

    @staticmethod
    def _fmt_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _first_ack_clock(self, event: dict[str, object], problem_start: int, max_clock: int) -> int | None:
        raw_acks = event.get("acknowledges", [])
        if not isinstance(raw_acks, list):
            return None

        first_ack: int | None = None
        for ack in raw_acks:
            if not isinstance(ack, dict):
                continue
            ack_clock = int(str(ack.get("clock", 0)))
            if ack_clock < problem_start or ack_clock > max_clock:
                continue
            if first_ack is None or ack_clock < first_ack:
                first_ack = ack_clock
        return first_ack

    def generate_group_report(
        self,
        group_id: str,
        start_ts: int,
        end_ts: int,
        trigger_names: list[str] | None = None,
    ) -> dict[str, object]:
        group_name, group_tree_ids = self.client.get_group_tree_ids(group_id)
        hosts = self.client.get_hosts(group_tree_ids)
        host_ids: list[str] = []
        host_names: dict[str, str] = {}
        host_triggers_filtered: dict[str, list[str]] = {}
        host_triggers_all: dict[str, list[str]] = {}
        trigger_descriptions: dict[str, str] = {}
        skipped_hosts: list[dict[str, str]] = []

        selected_trigger_names = [name.strip() for name in (trigger_names or []) if name.strip()]
        selected_trigger_names_norm = {name.lower() for name in selected_trigger_names}

        for host in hosts:
            host_id = str(host["hostid"])
            host_name = str(host.get("name", host_id))

            # Host desabilitado não entra no cálculo
            if int(str(host.get("status", 0))) != 0:
                skipped_hosts.append({
                    "host_id": host_id,
                    "host_name": host_name,
                    "reason": "host_disabled",
                })
                continue

            host_ids.append(host_id)
            host_names[host_id] = host_name
            host_trigger_ids_filtered: list[str] = []
            host_trigger_ids_all: list[str] = []
            for trigger in host.get("triggers", []):
                trigger_id = str(trigger["triggerid"])
                trigger_desc = str(trigger.get("description", ""))
                host_trigger_ids_all.append(trigger_id)
                if selected_trigger_names_norm and trigger_desc.lower() not in selected_trigger_names_norm:
                    trigger_descriptions[trigger_id] = trigger_desc
                    continue
                host_trigger_ids_filtered.append(trigger_id)
                trigger_descriptions[trigger_id] = trigger_desc

            host_triggers_filtered[host_id] = host_trigger_ids_filtered
            host_triggers_all[host_id] = host_trigger_ids_all

        all_trigger_ids_filtered: list[str] = sorted({trigger_id for ids in host_triggers_filtered.values() for trigger_id in ids})
        all_trigger_ids_all: list[str] = sorted({trigger_id for ids in host_triggers_all.values() for trigger_id in ids})

        trigger_is_enabled: dict[str, bool] = {}
        skipped_triggers: list[dict[str, str]] = []

        for trigger_batch in self.chunked(all_trigger_ids_all, 200):
            details = self.client.get_trigger_details(trigger_batch)
            for detail in details:
                trigger_id = str(detail.get("triggerid", ""))
                if not trigger_id:
                    continue

                trigger_desc = trigger_descriptions.get(trigger_id, str(detail.get("description", "")))
                trigger_disabled = int(str(detail.get("status", 0))) != 0
                items = detail.get("items", [])
                has_disabled_item = False
                if isinstance(items, list):
                    for item in items:
                        if int(str(item.get("status", 0))) != 0:
                            has_disabled_item = True
                            break

                enabled = (not trigger_disabled) and (not has_disabled_item)
                trigger_is_enabled[trigger_id] = enabled

                if not enabled:
                    reason = "trigger_disabled" if trigger_disabled else "item_disabled"
                    skipped_triggers.append(
                        {
                            "trigger_id": trigger_id,
                            "trigger_description": trigger_desc,
                            "reason": reason,
                        }
                    )

        enabled_trigger_ids_all = [trigger_id for trigger_id in all_trigger_ids_all if trigger_is_enabled.get(trigger_id, True)]

        if not enabled_trigger_ids_all:
            return {
                "group_id": group_id,
                "group_name": group_name,
                "rows": [
                    ReportRow(
                        host_name=host_names.get(host_id, host_id),
                        downtime_seconds=0,
                        uptime_seconds=max(end_ts - start_ts, 1),
                        availability=100.0,
                        mtta_seconds=0,
                        mttr_seconds=0,
                    )
                    for host_id in host_ids
                ],
                "start_ts": start_ts,
                "end_ts": end_ts,
                "total_period": max(end_ts - start_ts, 1),
                "selected_triggers": selected_trigger_names,
                "matched_triggers": [],
                "problem_stats": {
                    "problems_started": 0,
                    "problems_resolved": 0,
                    "problems_with_ack": 0,
                    "mtta_seconds": 0,
                    "mtta_fmt": "00:00:00",
                    "mttr_seconds": 0,
                    "mttr_fmt": "00:00:00",
                    "ack_supported": False,
                },
            }

        events: list[dict[str, object]] = []
        previous_events: list[dict[str, object]] = []
        ack_supported = True
        for trigger_batch in self.chunked(enabled_trigger_ids_all, 200):
            if ack_supported:
                try:
                    events.extend(
                        self.client.get_events(
                            trigger_batch,
                            start_ts,
                            end_ts,
                            sortorder="ASC",
                            include_acknowledges=True,
                        )
                    )
                except Exception as exc:
                    # Fallback para ambientes em que event.get não aceita selectAcknowledges
                    msg = str(exc).lower()
                    if "selectacknowledges" in msg or "invalid parameter" in msg:
                        ack_supported = False
                        events.extend(self.client.get_events(trigger_batch, start_ts, end_ts, sortorder="ASC"))
                    else:
                        raise
            else:
                events.extend(self.client.get_events(trigger_batch, start_ts, end_ts, sortorder="ASC"))
            previous_events.extend(self.client.get_events(trigger_batch, time_till=start_ts, sortorder="DESC"))

        events_by_trigger: dict[str, list[dict[str, object]]] = {}
        for event in events:
            events_by_trigger.setdefault(str(event["objectid"]), []).append(event)

        for trigger_id in list(events_by_trigger.keys()):
            events_by_trigger[trigger_id].sort(key=lambda ev: int(str(ev.get("clock", 0))))

        prev_state: dict[str, dict[str, object]] = {}
        for event in previous_events:
            trigger_id = str(event["objectid"])
            prev_state.setdefault(trigger_id, event)

        total_period = max(end_ts - start_ts, 1)
        rows: list[ReportRow] = []
        audit_events_rows: list[dict[str, object]] = []
        audit_trigger_intervals_rows: list[dict[str, object]] = []
        audit_merged_intervals_rows: list[dict[str, object]] = []
        mtta_samples: list[int] = []
        mttr_samples: list[int] = []
        problems_started = 0
        problems_resolved = 0
        problems_with_ack = 0

        for host_id in host_ids:
            intervals: list[tuple[int, int]] = []
            relevant_trigger_ids_filtered: list[str] = []
            relevant_trigger_ids_all: list[str] = []
            host_name = host_names.get(host_id, host_id)
            host_mtta_samples: list[int] = []
            host_mttr_samples: list[int] = []

            for trigger_id in host_triggers_filtered.get(host_id, []):
                if not trigger_is_enabled.get(trigger_id, True):
                    continue
                has_events = trigger_id in events_by_trigger
                prev_value_raw: Any = prev_state.get(trigger_id, {}).get("value", 0)
                starts_in_problem = int(str(prev_value_raw)) == 1
                if has_events or starts_in_problem:
                    relevant_trigger_ids_filtered.append(trigger_id)

            for trigger_id in host_triggers_all.get(host_id, []):
                if not trigger_is_enabled.get(trigger_id, True):
                    continue
                has_events = trigger_id in events_by_trigger
                prev_value_raw: Any = prev_state.get(trigger_id, {}).get("value", 0)
                starts_in_problem = int(str(prev_value_raw)) == 1
                if has_events or starts_in_problem:
                    relevant_trigger_ids_all.append(trigger_id)

            # Downtime/Uptime continuam baseados nas triggers selecionadas
            for trigger_id in relevant_trigger_ids_filtered:
                prev_value_raw: Any = prev_state.get(trigger_id, {}).get("value", 0)
                down_start = start_ts if int(str(prev_value_raw)) == 1 else None
                trigger_desc = trigger_descriptions.get(trigger_id, "")

                for event in events_by_trigger.get(trigger_id, []):
                    clock = int(str(event.get("clock", 0)))
                    value = int(str(event.get("value", 0)))
                    if value == 1 and down_start is None:
                        down_start = clock
                    elif value == 0 and down_start is not None:
                        if clock > down_start:
                            intervals.append((down_start, clock))
                            audit_trigger_intervals_rows.append(
                                {
                                    "host_id": host_id,
                                    "host_name": host_name,
                                    "trigger_id": trigger_id,
                                    "trigger_description": trigger_desc,
                                    "interval_start": self._fmt_ts(down_start),
                                    "interval_end": self._fmt_ts(clock),
                                    "duration_seconds": clock - down_start,
                                    "duration_fmt": self.format_seconds(clock - down_start),
                                    "problem_event_id": str(event.get("eventid", "")),
                                }
                            )
                        down_start = None

                if down_start is not None and end_ts > down_start:
                    intervals.append((down_start, end_ts))
                    audit_trigger_intervals_rows.append(
                        {
                            "host_id": host_id,
                            "host_name": host_name,
                            "trigger_id": trigger_id,
                            "trigger_description": trigger_desc,
                            "interval_start": self._fmt_ts(down_start),
                            "interval_end": self._fmt_ts(end_ts),
                            "duration_seconds": end_ts - down_start,
                            "duration_fmt": self.format_seconds(end_ts - down_start),
                            "problem_event_id": "",
                        }
                    )

            # MTTA/MTTR agora baseados em TODOS os alertas do host (triggers habilitadas),
            # independentemente do filtro de triggers informado pelo usuário.
            for trigger_id in relevant_trigger_ids_all:
                prev_value_raw: Any = prev_state.get(trigger_id, {}).get("value", 0)
                down_start = start_ts if int(str(prev_value_raw)) == 1 else None
                trigger_desc = trigger_descriptions.get(trigger_id, "")
                current_problem_started_in_period = False
                current_problem_eventid = ""

                for event in events_by_trigger.get(trigger_id, []):
                    clock = int(str(event.get("clock", 0)))
                    value = int(str(event.get("value", 0)))
                    eventid = str(event.get("eventid", ""))
                    first_ack_clock = self._first_ack_clock(event, clock, end_ts)
                    mtta_seconds = ""

                    audit_events_rows.append(
                        {
                            "host_id": host_id,
                            "host_name": host_name,
                            "trigger_id": trigger_id,
                            "trigger_description": trigger_desc,
                            "event_id": eventid,
                            "event_time": self._fmt_ts(clock),
                            "event_value": value,
                            "prev_state_value": int(str(prev_value_raw)),
                            "first_ack_time": self._fmt_ts(first_ack_clock) if first_ack_clock else "",
                            "mtta_seconds": "",
                        }
                    )

                    if value == 1 and down_start is None:
                        down_start = clock
                        current_problem_started_in_period = True
                        current_problem_eventid = eventid
                        problems_started += 1

                        if first_ack_clock is not None:
                            mtta_value = max(0, first_ack_clock - clock)
                            mtta_samples.append(mtta_value)
                            host_mtta_samples.append(mtta_value)
                            problems_with_ack += 1
                            mtta_seconds = str(mtta_value)
                            audit_events_rows[-1]["mtta_seconds"] = mtta_seconds
                    elif value == 0 and down_start is not None:
                        if current_problem_started_in_period and clock >= down_start:
                            problems_resolved += 1
                            mttr_value = max(0, clock - down_start)
                            mttr_samples.append(mttr_value)
                            host_mttr_samples.append(mttr_value)

                        down_start = None
                        current_problem_started_in_period = False
                        current_problem_eventid = ""

            merged_intervals = self._merge_intervals(intervals)
            for merged_start, merged_end in merged_intervals:
                audit_merged_intervals_rows.append(
                    {
                        "host_id": host_id,
                        "host_name": host_name,
                        "interval_start": self._fmt_ts(merged_start),
                        "interval_end": self._fmt_ts(merged_end),
                        "duration_seconds": merged_end - merged_start,
                        "duration_fmt": self.format_seconds(merged_end - merged_start),
                    }
                )
            downtime = sum((end - start) for start, end in merged_intervals)

            downtime = min(downtime, total_period)
            uptime = total_period - downtime
            host_mtta_avg = int(sum(host_mtta_samples) / len(host_mtta_samples)) if host_mtta_samples else 0
            host_mttr_avg = int(sum(host_mttr_samples) / len(host_mttr_samples)) if host_mttr_samples else 0
            rows.append(
                ReportRow(
                    host_name=host_names.get(host_id, host_id),
                    downtime_seconds=downtime,
                    uptime_seconds=uptime,
                    availability=(uptime / total_period) * 100.0,
                    mtta_seconds=host_mtta_avg,
                    mttr_seconds=host_mttr_avg,
                )
            )

        mtta_avg = int(sum(mtta_samples) / len(mtta_samples)) if mtta_samples else 0
        mttr_avg = int(sum(mttr_samples) / len(mttr_samples)) if mttr_samples else 0

        return {
            "group_id": group_id,
            "group_name": group_name,
            "rows": rows,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "total_period": total_period,
            "selected_triggers": selected_trigger_names,
            "matched_triggers": sorted(
                {
                    trigger_descriptions.get(trigger_id, trigger_id)
                    for trigger_id in all_trigger_ids_filtered
                    if trigger_is_enabled.get(trigger_id, True)
                }
            ),
            "problem_stats": {
                "problems_started": problems_started,
                "problems_resolved": problems_resolved,
                "problems_with_ack": problems_with_ack,
                "mtta_seconds": mtta_avg,
                "mtta_fmt": self.format_seconds(mtta_avg),
                "mttr_seconds": mttr_avg,
                "mttr_fmt": self.format_seconds(mttr_avg),
                "ack_supported": ack_supported,
            },
            "audit": {
                "events": audit_events_rows,
                "trigger_intervals": audit_trigger_intervals_rows,
                "merged_intervals": audit_merged_intervals_rows,
                "skipped_hosts": skipped_hosts,
                "skipped_triggers": skipped_triggers,
            },
        }

    def export_csv(self, report: dict[str, object], output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        start_ts = int(str(report.get("start_ts", 0)))
        end_ts = int(str(report.get("end_ts", 0)))
        start_label = datetime.fromtimestamp(start_ts).strftime("%d/%m/%Y %H:%M")
        end_label = datetime.fromtimestamp(end_ts).strftime("%d/%m/%Y %H:%M")

        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["Período", f"{start_label} a {end_label}"])
            problem_stats = report.get("problem_stats", {})
            if isinstance(problem_stats, dict):
                writer.writerow(["Base da disponibilidade", "Triggers filtradas pelo usuário"])
                writer.writerow(["Base do MTTA/MTTR", "Todos os alertas do host (triggers habilitadas)"])
                writer.writerow(["MTTA", str(problem_stats.get("mtta_fmt", "00:00:00"))])
                writer.writerow(["MTTR", str(problem_stats.get("mttr_fmt", "00:00:00"))])
                writer.writerow(["Problemas iniciados", str(problem_stats.get("problems_started", 0))])
                writer.writerow(["Problemas resolvidos", str(problem_stats.get("problems_resolved", 0))])
                writer.writerow([])
            writer.writerow(["Host", "Downtime", "Uptime", "Disponibilidade (%)", "MTTA", "MTTR"])
            report_rows = report.get("rows", [])
            if not isinstance(report_rows, list):
                report_rows = []

            for row in report_rows:
                assert isinstance(row, ReportRow)
                writer.writerow(
                    [
                        row.host_name,
                        self.format_seconds(row.downtime_seconds),
                        self.format_seconds(row.uptime_seconds),
                        f"{row.availability:.1f}",
                        self.format_seconds(row.mtta_seconds),
                        self.format_seconds(row.mttr_seconds),
                    ]
                )

        return output_path

    def export_calculation_csv(self, report: dict[str, object], output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        start_ts = int(str(report.get("start_ts", 0)))
        end_ts = int(str(report.get("end_ts", 0)))
        start_label = datetime.fromtimestamp(start_ts).strftime("%d/%m/%Y %H:%M")
        end_label = datetime.fromtimestamp(end_ts).strftime("%d/%m/%Y %H:%M")

        audit = report.get("audit", {})
        events_rows = audit.get("events", []) if isinstance(audit, dict) else []
        trigger_intervals_rows = audit.get("trigger_intervals", []) if isinstance(audit, dict) else []
        merged_intervals_rows = audit.get("merged_intervals", []) if isinstance(audit, dict) else []
        skipped_hosts_rows = audit.get("skipped_hosts", []) if isinstance(audit, dict) else []
        skipped_triggers_rows = audit.get("skipped_triggers", []) if isinstance(audit, dict) else []

        if not isinstance(events_rows, list):
            events_rows = []
        if not isinstance(trigger_intervals_rows, list):
            trigger_intervals_rows = []
        if not isinstance(merged_intervals_rows, list):
            merged_intervals_rows = []
        if not isinstance(skipped_hosts_rows, list):
            skipped_hosts_rows = []
        if not isinstance(skipped_triggers_rows, list):
            skipped_triggers_rows = []

        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            selected_triggers_raw = report.get("selected_triggers", [])
            selected_triggers_text = ""
            if isinstance(selected_triggers_raw, list):
                selected_triggers_text = " | ".join(str(item) for item in selected_triggers_raw)

            writer.writerow(["BASE DE CALCULO SLA - AUDITORIA"]) 
            writer.writerow(["Grupo", str(report.get("group_name", report.get("group_id", "")))])
            writer.writerow(["Periodo", f"{start_label} a {end_label}"])
            writer.writerow(["Triggers selecionadas", selected_triggers_text])
            writer.writerow([])

            writer.writerow(["EVENTOS USADOS"])
            writer.writerow(["Host ID", "Host", "Trigger ID", "Trigger", "PrevState", "EventTime", "EventValue"])
            for row in events_rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow([
                    row.get("host_id", ""),
                    row.get("host_name", ""),
                    row.get("trigger_id", ""),
                    row.get("trigger_description", ""),
                    row.get("prev_state_value", ""),
                    row.get("event_time", ""),
                    row.get("event_value", ""),
                ])
            writer.writerow([])

            writer.writerow(["INTERVALOS POR TRIGGER (ANTES DA UNIAO)"])
            writer.writerow(["Host ID", "Host", "Trigger ID", "Trigger", "Inicio", "Fim", "Duracao(s)", "Duracao"])
            for row in trigger_intervals_rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow([
                    row.get("host_id", ""),
                    row.get("host_name", ""),
                    row.get("trigger_id", ""),
                    row.get("trigger_description", ""),
                    row.get("interval_start", ""),
                    row.get("interval_end", ""),
                    row.get("duration_seconds", ""),
                    row.get("duration_fmt", ""),
                ])
            writer.writerow([])

            writer.writerow(["INTERVALOS FINAIS POR HOST (APOS UNIAO)"])
            writer.writerow(["Host ID", "Host", "Inicio", "Fim", "Duracao(s)", "Duracao"])
            for row in merged_intervals_rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow([
                    row.get("host_id", ""),
                    row.get("host_name", ""),
                    row.get("interval_start", ""),
                    row.get("interval_end", ""),
                    row.get("duration_seconds", ""),
                    row.get("duration_fmt", ""),
                ])
            writer.writerow([])

            writer.writerow(["ITENS IGNORADOS POR VALIDACAO"])
            writer.writerow(["Tipo", "ID", "Nome/Descricao", "Motivo"])

            for row in skipped_hosts_rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow([
                    "host",
                    row.get("host_id", ""),
                    row.get("host_name", ""),
                    row.get("reason", ""),
                ])

            for row in skipped_triggers_rows:
                if not isinstance(row, dict):
                    continue
                writer.writerow([
                    "trigger",
                    row.get("trigger_id", ""),
                    row.get("trigger_description", ""),
                    row.get("reason", ""),
                ])

        return output_path
