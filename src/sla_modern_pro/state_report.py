from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from . import csv_utils
import re
import time

from .client import ZabbixJsonRpcClient
from .report import ReportService

NO_DATA = "SEM DADOS"

# Estados conhecidos do Cloudera Manager aparecem primeiro, nesta ordem;
# qualquer outro texto retornado pelo item entra depois, por tempo total.
KNOWN_STATE_ORDER = [
    "GOOD",
    "CONCERNING",
    "BAD",
    "STOP",
    "STOPPED",
    "DISABLED",
    "NOT_AVAILABLE",
    "HISTORY_NOT_AVAILABLE",
    "UNKNOWN",
]

STATE_TONES = {
    "GOOD": "good",
    "CONCERNING": "warn",
    "BAD": "bad",
    "STOP": "stop",
    "STOPPED": "stop",
    "DISABLED": "stop",
    NO_DATA: "nodata",
}

# Tipos de valor do Zabbix (item.value_type), que também são o parâmetro "history" do history.get
NUMERIC_VALUE_TYPES = {0, 3}

# history.get é paginado por lote de itens e janela de tempo. A primeira janela
# tem 1 dia; as seguintes se ajustam à densidade observada para trazer cerca de
# HISTORY_TARGET_VALUES por chamada (10 itens a cada 1 min ~ janela de 3,5 dias).
# Menos chamadas = relatório mais rápido; o teto evita respostas pesadas no frontend do Zabbix.
HISTORY_ITEM_BATCH = 10
HISTORY_WINDOW_SECONDS = 86400
HISTORY_TARGET_VALUES = 50_000
HISTORY_MIN_WINDOW_SECONDS = 3600
HISTORY_MAX_WINDOW_SECONDS = 31 * 86400

# Itens de texto livre podem devolver valores enormes; o estado vira um rótulo curto.
MAX_STATE_LENGTH = 80

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass(slots=True)
class StateRow:
    host_name: str
    item_name: str
    item_key: str
    total_seconds: int
    durations: dict[str, int] = field(default_factory=dict)
    # Valores recebidos no período por estado (linhas do histórico do Zabbix)
    samples: dict[str, int] = field(default_factory=dict)
    intervals: list[tuple[int, int, str]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return (self.total_seconds - self.durations.get(NO_DATA, 0)) / self.total_seconds * 100.0


def parse_item_patterns(raw: str) -> list[str]:
    # Vírgula não separa: chaves com parâmetros usam vírgula (ex.: key[a,b]).
    normalized = (raw or "").replace(";", "\n")
    return [part.strip() for part in normalized.split("\n") if part.strip()]


def key_matches(pattern: str, key: str) -> bool:
    if "*" not in pattern:
        return pattern == key
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, key, flags=re.IGNORECASE) is not None


def parse_duration(raw: object) -> int | None:
    text = str(raw or "").strip().lower()
    match = re.fullmatch(r"(\d+)([smhdw]?)", text)
    if not match:
        return None
    return int(match.group(1)) * _DURATION_UNITS.get(match.group(2) or "s", 1)


def build_valuemap(item: dict[str, Any]) -> dict[str, str]:
    valuemap = item.get("valuemap")
    if not isinstance(valuemap, dict):
        return {}
    mappings = valuemap.get("mappings", [])
    if not isinstance(mappings, list):
        return {}
    # Só mapeamentos exatos (type 0); faixas/regex não se aplicam a estados.
    return {
        str(mapping.get("value", "")): str(mapping.get("newvalue", ""))
        for mapping in mappings
        if isinstance(mapping, dict) and str(mapping.get("type", "0")) == "0"
    }


def normalize_state(raw: object, value_type: int, valuemap: dict[str, str]) -> str:
    value = str(raw if raw is not None else "").strip()
    if value_type in NUMERIC_VALUE_TYPES:
        try:
            number = float(value)
            if number.is_integer():
                value = str(int(number))
        except ValueError:
            pass
    value = valuemap.get(value, value)
    if len(value) > MAX_STATE_LENGTH:
        value = value[:MAX_STATE_LENGTH - 1] + "…"
    return value or "(vazio)"


class StateRuns:
    """Acumula amostras (em ordem de clock) guardando só as sequências de mesmo estado.

    Cada sequência é (primeiro_clock, ultimo_clock, estado): amostras seguidas com o
    mesmo estado e sem buraco maior que ``max_gap`` viram uma única entrada. Assim a
    memória cresce com o número de mudanças de estado, não com o número de coletas.
    ``samples`` conta as coletas por estado; ``counted=False`` usa a amostra só para
    definir o estado (ex.: valor anterior ao início do período) sem contá-la.
    """

    __slots__ = ("max_gap", "runs", "samples")

    def __init__(self, max_gap: int) -> None:
        self.max_gap = max_gap
        self.runs: list[tuple[int, int, str]] = []
        self.samples: dict[str, int] = {}

    def add(self, clock: int, state: str, counted: bool = True) -> None:
        if counted:
            self.samples[state] = self.samples.get(state, 0) + 1
        if self.runs:
            first, last, current = self.runs[-1]
            if current == state and 0 <= clock - last <= self.max_gap:
                self.runs[-1] = (first, clock, state)
                return
        self.runs.append((clock, clock, state))

    def intervals(
        self,
        start_ts: int,
        end_ts: int,
        previous: tuple[int, str] | None = None,
    ) -> list[tuple[int, int, str]]:
        """Converte as sequências em intervalos contínuos [inicio, fim) cobrindo todo o período.

        Cada amostra vale até a próxima amostra, limitada a ``max_gap`` segundos;
        o que sobra sem cobertura vira SEM DADOS. ``previous`` é a última amostra
        antes do início do período, usada para saber o estado no instante inicial.
        """
        if end_ts <= start_ts:
            return []

        runs = self.runs
        if previous is not None and previous[0] < start_ts:
            runs = [(previous[0], previous[0], previous[1]), *runs]

        intervals: list[tuple[int, int, str]] = []
        cursor = start_ts

        def _append(seg_start: int, seg_end: int, state: str) -> None:
            if intervals and intervals[-1][2] == state and intervals[-1][1] == seg_start:
                intervals[-1] = (intervals[-1][0], seg_end, state)
            else:
                intervals.append((seg_start, seg_end, state))

        for index, (first, last, state) in enumerate(runs):
            next_first = runs[index + 1][0] if index + 1 < len(runs) else end_ts
            seg_start = max(first, start_ts)
            seg_end = min(next_first, last + self.max_gap, end_ts)
            if seg_end <= seg_start:
                continue
            if seg_start > cursor:
                _append(cursor, seg_start, NO_DATA)
            _append(seg_start, seg_end, state)
            cursor = seg_end
        if cursor < end_ts:
            _append(cursor, end_ts, NO_DATA)
        return intervals


def build_state_intervals(
    samples: list[tuple[int, str]],
    start_ts: int,
    end_ts: int,
    max_gap: int,
    previous: tuple[int, str] | None = None,
) -> list[tuple[int, int, str]]:
    runs = StateRuns(max_gap)
    for clock, state in sorted(samples, key=lambda sample: sample[0]):
        runs.add(clock, state, counted=start_ts <= clock <= end_ts)
    return runs.intervals(start_ts, end_ts, previous)


def order_states(totals: dict[str, int]) -> list[str]:
    known = [state for state in KNOWN_STATE_ORDER if state in totals]
    others = sorted(
        (state for state in totals if state not in KNOWN_STATE_ORDER and state != NO_DATA),
        key=lambda state: (-totals[state], state),
    )
    tail = [NO_DATA] if NO_DATA in totals else []
    return [*known, *others, *tail]


class StateReportService:
    def __init__(self, client: ZabbixJsonRpcClient) -> None:
        self.client = client

    format_seconds = staticmethod(ReportService.format_seconds)

    @staticmethod
    def _fmt_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
        for index in range(0, len(values), size):
            yield values[index:index + size]

    def _get_items(self, group_ids: list[int], pattern: str) -> list[dict[str, Any]]:
        try:
            items = self.client.get_items(group_ids, pattern, include_valuemap=True)
        except Exception as exc:
            # Zabbix < 5.4 não aceita selectValueMap
            msg = str(exc).lower()
            if "selectvaluemap" not in msg and "invalid parameter" not in msg:
                raise
            items = self.client.get_items(group_ids, pattern, include_valuemap=False)
        return [item for item in items if key_matches(pattern, str(item.get("key_", "")))]

    def _collect_runs(
        self,
        items: list[dict[str, Any]],
        value_type: int,
        start_ts: int,
        end_ts: int,
        max_gap: int,
    ) -> dict[str, StateRuns]:
        """Lê o histórico em lotes/janelas e já reduz cada resposta a sequências de estado.

        Nenhuma resposta do history.get fica guardada depois de processada. A leitura
        começa ``max_gap`` antes do início: uma amostra mais antiga que isso não cobriria
        o início do período, então não é preciso buscar o "valor anterior" item a item.
        """
        valuemaps = {str(item["itemid"]): build_valuemap(item) for item in items}
        runs = {item_id: StateRuns(max_gap) for item_id in valuemaps}
        item_ids = list(valuemaps)
        for batch in self._chunked(item_ids, HISTORY_ITEM_BATCH):
            window = HISTORY_WINDOW_SECONDS
            window_start = start_ts - max_gap
            while window_start < end_ts:
                window_end = min(window_start + window, end_ts)
                # time_till é inclusivo: fecha a janela 1s antes para não duplicar a borda
                till = window_end - 1 if window_end < end_ts else window_end
                records = self.client.get_history(batch, value_type, window_start, till)
                for record in records:
                    item_id = str(record.get("itemid", ""))
                    if item_id not in runs:
                        continue
                    clock = int(str(record.get("clock", 0)))
                    runs[item_id].add(
                        clock,
                        normalize_state(record.get("value"), value_type, valuemaps[item_id]),
                        # a leitura começa antes do início só para saber o estado inicial
                        counted=clock >= start_ts,
                    )
                window = self._next_window(window, window_end - window_start, len(records))
                del records
                window_start = window_end
        return runs

    @staticmethod
    def _next_window(current: int, span: int, values: int) -> int:
        if values == 0:
            proposed = current * 4
        else:
            proposed = int(HISTORY_TARGET_VALUES * span / values)
        return max(HISTORY_MIN_WINDOW_SECONDS, min(proposed, HISTORY_MAX_WINDOW_SECONDS))

    def generate_state_report(
        self,
        group_id: str,
        start_ts: int,
        end_ts: int,
        item_patterns: list[str],
        max_gap_seconds: int,
        now_ts: int | None = None,
    ) -> dict[str, object]:
        if not item_patterns:
            raise ValueError("Informe ao menos uma chave de item.")
        if end_ts <= start_ts:
            raise ValueError("O fim do período precisa ser posterior ao início.")

        now_ts = int(time.time()) if now_ts is None else now_ts
        # Período no futuro não tem coleta: calcula só até agora para não inflar SEM DADOS.
        effective_end = min(end_ts, now_ts)
        if effective_end <= start_ts:
            raise ValueError("O período selecionado ainda não começou.")

        group_name, group_tree_ids = self.client.get_group_tree_ids(group_id)

        items_by_id: dict[str, dict[str, Any]] = {}
        unmatched_patterns: list[str] = []
        for pattern in item_patterns:
            found = self._get_items(group_tree_ids, pattern)
            if not found:
                unmatched_patterns.append(pattern)
            for item in found:
                items_by_id.setdefault(str(item["itemid"]), item)

        skipped: list[dict[str, str]] = []
        warnings: list[str] = [
            f"Nenhum item encontrado no grupo para a chave '{pattern}'." for pattern in unmatched_patterns
        ]
        active_items: list[dict[str, Any]] = []
        for item_id, item in items_by_id.items():
            hosts = item.get("hosts", [])
            host = hosts[0] if isinstance(hosts, list) and hosts else {}
            item["_host_name"] = str(host.get("name", item.get("hostid", "")))
            if int(str(host.get("status", 0))) != 0:
                skipped.append({"host_name": item["_host_name"], "item_name": str(item.get("name", "")), "item_key": str(item.get("key_", "")), "reason": "host_disabled"})
                continue
            if int(str(item.get("status", 0))) != 0:
                skipped.append({"host_name": item["_host_name"], "item_name": str(item.get("name", "")), "item_key": str(item.get("key_", "")), "reason": "item_disabled"})
                continue
            active_items.append(item)

            retention = parse_duration(item.get("history"))
            if retention == 0:
                warnings.append(
                    f"{item['_host_name']} / {item.get('key_')}: o item não guarda histórico (history=0)."
                )
            elif retention is not None and retention < now_ts - start_ts:
                warnings.append(
                    f"{item['_host_name']} / {item.get('key_')}: histórico do item é de {item.get('history')}, "
                    "menor que a distância até o início do período; o trecho mais antigo aparece como SEM DADOS."
                )

        active_items.sort(key=lambda item: (item["_host_name"].lower(), str(item.get("name", "")).lower()))

        items_by_type: dict[int, list[dict[str, Any]]] = {}
        for item in active_items:
            items_by_type.setdefault(int(str(item.get("value_type", 1))), []).append(item)

        runs_by_item: dict[str, StateRuns] = {}
        for value_type, typed_items in items_by_type.items():
            runs_by_item.update(
                self._collect_runs(typed_items, value_type, start_ts, effective_end, max_gap_seconds)
            )

        total_period = effective_end - start_ts
        rows: list[StateRow] = []
        totals: dict[str, int] = {}

        for item in active_items:
            item_id = str(item["itemid"])
            item_key = str(item.get("key_", ""))
            item_name = str(item.get("name", item_key))

            runs = runs_by_item.get(item_id) or StateRuns(max_gap_seconds)
            intervals = runs.intervals(start_ts, effective_end)
            row = StateRow(
                host_name=item["_host_name"],
                item_name=item_name,
                item_key=item_key,
                total_seconds=total_period,
                samples=dict(runs.samples),
                intervals=intervals,
            )
            for seg_start, seg_end, state in intervals:
                duration = seg_end - seg_start
                row.durations[state] = row.durations.get(state, 0) + duration
                totals[state] = totals.get(state, 0) + duration
            rows.append(row)

        return {
            "group_id": group_id,
            "group_name": group_name,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "effective_end_ts": effective_end,
            "total_period": total_period,
            "max_gap_seconds": max_gap_seconds,
            "item_patterns": item_patterns,
            "states": order_states(totals),
            "totals": totals,
            "rows": rows,
            "interval_count": sum(len(row.intervals) for row in rows),
            "warnings": warnings,
            "skipped": skipped,
        }

    def export_csv(self, report: dict[str, object], output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        states = [str(state) for state in report.get("states", [])]  # type: ignore[union-attr]
        rows = report.get("rows", [])
        rows = rows if isinstance(rows, list) else []

        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv_utils.writer(handle, delimiter=";")
            writer.writerow(["TEMPO POR ESTADO"])
            writer.writerow(["Grupo", str(report.get("group_name", report.get("group_id", "")))])
            writer.writerow(["Periodo", self._period_label(report)])
            writer.writerow(["Chaves", " | ".join(str(p) for p in report.get("item_patterns", []))])  # type: ignore[union-attr]
            writer.writerow(["Tolerancia sem dados", self.format_seconds(int(str(report.get("max_gap_seconds", 0))))])
            writer.writerow([])

            header = ["Host", "Item"]
            for state in states:
                header.extend([f"{state} (tempo)", f"{state} (%)", f"{state} (coletas)"])
            header.append("Cobertura (%)")
            writer.writerow(header)

            for row in rows:
                assert isinstance(row, StateRow)
                line: list[str] = [row.host_name, row.item_name]
                for state in states:
                    seconds = row.durations.get(state, 0)
                    pct = seconds / row.total_seconds * 100.0 if row.total_seconds else 0.0
                    line.extend([self.format_seconds(seconds), f"{pct:.2f}", str(row.samples.get(state, 0))])
                line.append(f"{row.coverage:.2f}")
                writer.writerow(line)
        return output_path

    def export_intervals_csv(self, report: dict[str, object], output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv_utils.writer(handle, delimiter=";")
            writer.writerow(["INTERVALOS POR ESTADO - AUDITORIA"])
            writer.writerow(["Grupo", str(report.get("group_name", report.get("group_id", "")))])
            writer.writerow(["Periodo", self._period_label(report)])
            writer.writerow([])
            writer.writerow(["Host", "Item", "Estado", "Inicio", "Fim", "Duracao(s)", "Duracao"])
            for row in self.iter_interval_rows(report):
                writer.writerow([
                    row.get("host_name", ""),
                    row.get("item_name", ""),
                    row.get("state", ""),
                    row.get("interval_start", ""),
                    row.get("interval_end", ""),
                    row.get("duration_seconds", ""),
                    row.get("duration_fmt", ""),
                ])
        return output_path

    def iter_interval_rows(self, report: dict[str, object]) -> Iterator[dict[str, object]]:
        """Formata os intervalos sob demanda, sem montar a lista inteira na memória."""
        rows = report.get("rows", [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, StateRow):
                continue
            for seg_start, seg_end, state in row.intervals:
                yield {
                    "host_name": row.host_name,
                    "item_name": row.item_name,
                    "item_key": row.item_key,
                    "state": state,
                    "interval_start": self._fmt_ts(seg_start),
                    "interval_end": self._fmt_ts(seg_end),
                    "duration_seconds": seg_end - seg_start,
                    "duration_fmt": self.format_seconds(seg_end - seg_start),
                }

    @staticmethod
    def _period_label(report: dict[str, object]) -> str:
        start_ts = int(str(report.get("start_ts", 0)))
        end_ts = int(str(report.get("effective_end_ts", report.get("end_ts", 0))))
        start_label = datetime.fromtimestamp(start_ts).strftime("%d/%m/%Y %H:%M")
        end_label = datetime.fromtimestamp(end_ts).strftime("%d/%m/%Y %H:%M")
        return f"{start_label} a {end_label}"
