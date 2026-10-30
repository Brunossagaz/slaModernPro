from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sla_modern_pro.state_report import (
    HISTORY_WINDOW_SECONDS,
    NO_DATA,
    StateReportService,
    build_state_intervals,
    build_valuemap,
    key_matches,
    normalize_state,
    order_states,
    parse_duration,
    parse_item_patterns,
)

START = 1_700_000_000


def _totals(intervals):
    totals = {}
    for start, end, state in intervals:
        totals[state] = totals.get(state, 0) + (end - start)
    return totals


def test_each_sample_holds_until_next_and_sums_to_period():
    samples = [(START, "GOOD"), (START + 60, "CONCERNING"), (START + 180, "GOOD")]
    intervals = build_state_intervals(samples, START, START + 600, max_gap=300)

    assert intervals == [
        (START, START + 60, "GOOD"),
        (START + 60, START + 180, "CONCERNING"),
        (START + 180, START + 480, "GOOD"),
        (START + 480, START + 600, NO_DATA),
    ]
    assert sum(end - start for start, end, _ in intervals) == 600


def test_gap_longer_than_tolerance_becomes_no_data():
    samples = [(START, "GOOD"), (START + 1000, "GOOD")]
    intervals = build_state_intervals(samples, START, START + 1060, max_gap=120)

    assert _totals(intervals) == {"GOOD": 180, NO_DATA: 880}


def test_previous_sample_defines_state_at_period_start():
    samples = [(START + 30, "BAD")]
    intervals = build_state_intervals(samples, START, START + 90, max_gap=300, previous=(START - 20, "CONCERNING"))

    assert intervals == [(START, START + 30, "CONCERNING"), (START + 30, START + 90, "BAD")]


def test_stale_previous_sample_does_not_cover_period_start():
    intervals = build_state_intervals([], START, START + 600, max_gap=300, previous=(START - 400, "GOOD"))

    assert intervals == [(START, START + 600, NO_DATA)]


def test_consecutive_equal_states_are_merged():
    samples = [(START + offset, "GOOD") for offset in range(0, 600, 60)]
    assert build_state_intervals(samples, START, START + 600, max_gap=120) == [(START, START + 600, "GOOD")]


def test_key_matches_exact_and_wildcard_with_brackets():
    assert key_matches("cluster.status", "cluster.status")
    assert not key_matches("cluster.status", "cluster.status.code")
    assert key_matches("cloudera.health[*]", "cloudera.health[HDFS,svc]")
    assert not key_matches("cloudera.health[*]", "cloudera.healthX")


def test_parse_item_patterns_keeps_commas_inside_keys():
    assert parse_item_patterns("key[a,b]\n cluster.status ;other") == ["key[a,b]", "cluster.status", "other"]


def test_normalize_state_uses_valuemap_for_numeric_items():
    valuemap = build_valuemap({"valuemap": {"mappings": [{"type": "0", "value": "1", "newvalue": "CONCERNING"}]}})
    assert normalize_state("1.0000", 0, valuemap) == "CONCERNING"
    assert normalize_state(" GOOD \n", 1, {}) == "GOOD"
    assert normalize_state("", 1, {}) == "(vazio)"


def test_parse_duration_and_state_order():
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("3600") == 3600
    assert parse_duration("{$HISTORY}") is None
    assert order_states({NO_DATA: 5, "X": 1, "GOOD": 100, "BAD": 2, "Y": 3}) == ["GOOD", "BAD", "Y", "X", NO_DATA]


class FakeClient:
    def __init__(self):
        self.history_calls = []
        self.items = [
            {
                "itemid": "10", "hostid": "1", "name": "Cluster status", "key_": "cluster.status",
                "value_type": "1", "history": "7d", "status": "0",
                "hosts": [{"hostid": "1", "name": "cdp-prod", "status": "0"}],
            },
            {
                "itemid": "11", "hostid": "2", "name": "Cluster status", "key_": "cluster.status",
                "value_type": "1", "history": "90d", "status": "1",
                "hosts": [{"hostid": "2", "name": "cdp-old", "status": "0"}],
            },
            {
                "itemid": "12", "hostid": "1", "name": "Cluster status code", "key_": "cluster.status.code",
                "value_type": "3", "history": "90d", "status": "0",
                "hosts": [{"hostid": "1", "name": "cdp-prod", "status": "0"}],
            },
        ]
        self.history = {
            "10": [(START - 30, "BAD"), (START + 60, "GOOD"), (START + HISTORY_WINDOW_SECONDS + 60, "CONCERNING")],
        }

    def get_group_tree_ids(self, group_id):
        return "Clientes/ACME", [int(group_id)]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.logout()

    def logout(self):
        self.logged_out = True

    def get_items(self, group_ids, key, include_valuemap=True):
        if include_valuemap:
            raise RuntimeError("Invalid parameter \"/\": unexpected parameter \"selectValueMap\".")
        # simula o LIKE do Zabbix: devolve também chaves que só começam igual
        return [item for item in self.items if item["key_"].startswith(key)]

    def get_history(self, item_ids, value_type, time_from=None, time_till=None, sortorder="ASC", limit=None):
        self.history_calls.append((tuple(item_ids), time_from, time_till, sortorder, limit))
        records = [
            {"itemid": item_id, "clock": str(clock), "value": value}
            for item_id in item_ids
            for clock, value in self.history.get(item_id, [])
            if (time_from is None or clock >= time_from) and (time_till is None or clock <= time_till)
        ]
        records.sort(key=lambda r: int(r["clock"]), reverse=sortorder == "DESC")
        return records[:limit] if limit else records


def test_generate_state_report_end_to_end(tmp_path):
    client = FakeClient()
    service = StateReportService(client)
    end = START + 2 * HISTORY_WINDOW_SECONDS
    report = service.generate_state_report(
        "5", START, end, ["cluster.status"], max_gap_seconds=HISTORY_WINDOW_SECONDS * 2, now_ts=end + 10 * 86400
    )

    rows = report["rows"]
    assert [(row.host_name, row.item_key) for row in rows] == [("cdp-prod", "cluster.status")]
    row = rows[0]
    assert row.durations == {
        "BAD": 60,
        "GOOD": HISTORY_WINDOW_SECONDS,
        "CONCERNING": HISTORY_WINDOW_SECONDS - 60,
    }
    assert sum(row.durations.values()) == end - START
    # o BAD veio do valor anterior ao início: define o estado mas não conta como coleta do período
    assert row.samples == {"GOOD": 1, "CONCERNING": 1}
    assert report["states"] == ["GOOD", "CONCERNING", "BAD"]
    assert report["skipped"] == [
        {"host_name": "cdp-old", "item_name": "Cluster status", "item_key": "cluster.status", "reason": "item_disabled"}
    ]
    assert any("7d" in warning for warning in report["warnings"])

    # janelas contíguas, sem sobreposição, começando antes do início (pega o valor anterior)
    # e sem nenhuma chamada extra por item
    windows = [(f, t) for _, f, t, _, _ in client.history_calls]
    assert windows[0][0] == START - HISTORY_WINDOW_SECONDS * 2
    assert windows[-1][1] == end
    assert all(nxt[0] == cur[1] + 1 for cur, nxt in zip(windows, windows[1:]))
    assert all(order == "ASC" and limit is None for *_, order, limit in client.history_calls)

    csv_path = service.export_csv(report, tmp_path / "state.csv")
    intervals_path = service.export_intervals_csv(report, tmp_path / "intervals.csv")
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    header = next(line for line in csv_text.splitlines() if line.startswith("Host;"))
    assert header.split(";")[:3] == ["Host", "Item", "GOOD (tempo)"]
    assert "GOOD (coletas)" in header and "ocorrencias" not in header
    assert "cdp-prod;Cluster status;" in csv_text
    intervals_header = next(
        line for line in intervals_path.read_text(encoding="utf-8-sig").splitlines() if line.startswith("Host;")
    )
    assert intervals_header == "Host;Item;Estado;Inicio;Fim;Duracao(s);Duracao"
    assert "GOOD (tempo)" in csv_path.read_text(encoding="utf-8-sig")
    assert "CONCERNING" in intervals_path.read_text(encoding="utf-8-sig")


def test_generate_state_report_clips_future_end_and_rejects_empty_keys():
    service = StateReportService(FakeClient())
    report = service.generate_state_report("5", START, START + 86400, ["cluster.status"], 300, now_ts=START + 600)
    assert report["effective_end_ts"] == START + 600
    assert report["rows"][0].total_seconds == 600

    with pytest.raises(ValueError):
        service.generate_state_report("5", START, START + 60, [], 300, now_ts=START + 600)


def test_web_pages_render(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from sla_modern_pro import web

    class FakeWebClient(FakeClient):
        def get_hostgroups(self):
            return [{"groupid": "5", "name": "Clientes/ACME"}]

    monkeypatch.setattr(web, "_build_client", lambda url, user, password, interactive=False: FakeWebClient())
    monkeypatch.setattr(web.settings, "allowed_hosts", ["testserver"])
    client = TestClient(web.app)
    url = "https://zabbix.test/api_jsonrpc.php"

    assert client.post("/api/groups", data={"url": url, "user": "a", "password": "b"}).json() == {
        "groups": [{"groupid": "5", "name": "Clientes/ACME"}]
    }

    response = client.post(
        "/state-report",
        data={
            "url": url, "user": "a", "password": "b", "group_id": "5",
            "item_keys": "cluster.status", "max_gap_minutes": "10",
            "start": "2023-11-14T19:00", "end": "2023-11-15T19:00",
        },
    )
    assert response.status_code == 200
    assert "Tempo total por estado" in response.text
    assert 'data-initial="state"' in response.text
    download = [line for line in response.text.splitlines() if "/download-state/" in line][0]
    report_id = download.split("/download-state/")[1].split('"')[0]
    assert client.get(f"/download-state/{report_id}").status_code == 200
    assert client.get(f"/download-state-intervals/{report_id}").status_code == 200

    missing = client.post(
        "/state-report",
        data={"url": url, "user": "a", "password": "b", "group_id": "5", "start": "2023-11-14T19:00", "end": "2023-11-15T19:00"},
    )
    assert "Informe ao menos uma chave de item." in missing.text


def _naive_intervals(samples, start_ts, end_ts, max_gap, previous=None):
    # Implementação direta (uma amostra por vez), usada como referência
    points = sorted(samples)
    if previous is not None and previous[0] < start_ts:
        points.insert(0, previous)
    per_second = {}
    for index, (clock, state) in enumerate(points):
        next_clock = points[index + 1][0] if index + 1 < len(points) else end_ts
        for second in range(max(clock, start_ts), min(next_clock, clock + max_gap, end_ts)):
            per_second[second] = state
    totals = {}
    for second in range(start_ts, end_ts):
        state = per_second.get(second, NO_DATA)
        totals[state] = totals.get(state, 0) + 1
    return totals


def test_runs_match_naive_calculation_on_random_samples():
    import random

    rng = random.Random(42)
    for _ in range(200):
        clock = START - rng.randint(0, 200)
        samples = []
        for _ in range(rng.randint(0, 40)):
            clock += rng.choice([10, 30, 60, 60, 60, 400])
            samples.append((clock, rng.choice(["GOOD", "GOOD", "GOOD", "CONCERNING", "BAD"])))
        previous = (START - rng.randint(1, 500), "GOOD") if rng.random() < 0.5 else None
        in_period = [sample for sample in samples if START <= sample[0] <= START + 2000]
        intervals = build_state_intervals(in_period, START, START + 2000, max_gap=120, previous=previous)

        assert _totals(intervals) == _naive_intervals(in_period, START, START + 2000, 120, previous)


class MultiKeyClient(FakeClient):
    def __init__(self):
        super().__init__()
        host = [{"hostid": "1", "name": "dp-prod", "status": "0"}]
        self.items = [
            {"itemid": "20", "name": "Status HDFS", "key_": "dp.status.service[hdfs]", "value_type": "1", "history": "90d", "status": "0", "hosts": host},
            {"itemid": "21", "name": "Status YARN", "key_": "dp.status.service[yarn]", "value_type": "1", "history": "90d", "status": "0", "hosts": host},
            {"itemid": "22", "name": "Integridade", "key_": "dp.integridade", "value_type": "3", "history": "90d", "status": "0", "hosts": host,
             "valuemap": {"mappings": [{"type": "0", "value": "0", "newvalue": "OK"}, {"type": "0", "value": "1", "newvalue": "FALHA"}]}},
        ]
        self.history = {
            "20": [(START, "GOOD"), (START + 60, "BAD")],
            "21": [(START, "CONCERNING")],
            "22": [(START, "0"), (START + 30, "1")],
        }

    def get_items(self, group_ids, key, include_valuemap=True):
        prefix = key.rstrip("*")
        return [item for item in self.items if item["key_"].startswith(prefix)]


def test_multiple_keys_are_analysed_separately():
    service = StateReportService(MultiKeyClient())
    report = service.generate_state_report(
        "5", START, START + 120, parse_item_patterns("dp.status.service*;dp.integridade"), 300, now_ts=START + 86400
    )

    by_key = {row.item_key: row.durations for row in report["rows"]}
    assert by_key == {
        "dp.status.service[hdfs]": {"GOOD": 60, "BAD": 60},
        "dp.status.service[yarn]": {"CONCERNING": 120},
        "dp.integridade": {"OK": 30, "FALHA": 90},
    }
    assert {row.item_key: row.samples for row in report["rows"]} == {
        "dp.status.service[hdfs]": {"GOOD": 1, "BAD": 1},
        "dp.status.service[yarn]": {"CONCERNING": 1},
        "dp.integridade": {"OK": 1, "FALHA": 1},
    }
    assert report["warnings"] == []


class BulkClient(FakeClient):
    """Simula muitos itens coletando a cada minuto, gerando o histórico sob demanda."""

    def __init__(self, item_count):
        super().__init__()
        host = [{"hostid": "1", "name": "dp-prod", "status": "0"}]
        self.items = [
            {"itemid": str(1000 + i), "name": f"Status {i}", "key_": f"dp.status.service[{i}]",
             "value_type": "1", "history": "90d", "status": "0", "hosts": host}
            for i in range(item_count)
        ]

    def get_items(self, group_ids, key, include_valuemap=True):
        return list(self.items)

    def get_history(self, item_ids, value_type, time_from=None, time_till=None, sortorder="ASC", limit=None):
        self.history_calls.append((tuple(item_ids), time_from, time_till, sortorder, limit))
        if time_from is None:
            return []
        records = []
        for clock in range(time_from - time_from % 60, time_till + 1, 60):
            if clock < time_from:
                continue
            for item_id in item_ids:
                state = "CONCERNING" if (clock // 60 + int(item_id)) % 97 == 0 else "GOOD"
                records.append({"itemid": item_id, "clock": str(clock), "value": state})
        return records


def test_month_of_minute_data_for_many_items_stays_small():
    import tracemalloc

    start = START - START % 86400
    end = start + 31 * 86400
    service = StateReportService(BulkClient(item_count=40))
    service.client.history_calls = []

    tracemalloc.start()
    report = service.generate_state_report("5", start, end, ["dp.status.service*"], 600, now_ts=end)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 40 itens x 31 dias x 1/min = ~1.8 milhão de valores; antes tudo ficava na memória
    assert len(report["rows"]) == 40
    assert all(sum(row.durations.values()) == end - start for row in report["rows"])
    # uma coleta por minuto, com início e fim inclusivos
    assert all(sum(row.samples.values()) == 31 * 1440 + 1 for row in report["rows"])
    assert peak < 150 * 1024 * 1024
    # ~1.8 milhão de valores / ~50k por chamada: janela adaptativa mantém poucas chamadas
    assert len(service.client.history_calls) <= 50


def test_samples_count_every_value_in_period_but_not_before_start():
    samples = [(START - 60, "BAD")] + [(START + offset, "GOOD") for offset in range(0, 600, 60)]
    runs_totals = _totals(build_state_intervals(samples, START, START + 600, max_gap=120))
    assert runs_totals == {"GOOD": 600}

    from sla_modern_pro.state_report import StateRuns

    runs = StateRuns(120)
    for clock, state in samples:
        runs.add(clock, state, counted=clock >= START)
    assert runs.samples == {"GOOD": 10}
    # 10 coletas GOOD seguidas continuam sendo um único trecho no cálculo de tempo
    assert len(runs.runs) == 2
