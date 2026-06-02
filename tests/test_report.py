from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sla_modern_pro.report import ReportService


class DummyClient:
    def get_hosts(self, group_ids):
        return []

    def get_events(self, trigger_ids, time_from=None, time_till=None, sortorder="ASC"):
        return []


def test_format_seconds():
    assert ReportService.format_seconds(0) == "00:00:00"
    assert ReportService.format_seconds(3661) == "01:01:01"


def test_build_alarm_analytics_includes_severity_in_detailed_log():
    service = ReportService(DummyClient())

    analytics = service._build_alarm_analytics(
        events=[
            {
                "eventid": "1001",
                "objectid": "2001",
                "clock": "1710000000",
                "value": "1",
            }
        ],
        trigger_is_enabled={"2001": True},
        trigger_descriptions={"2001": "CPU usage high"},
        trigger_severities={"2001": "Alta"},
        host_triggers_all={"3001": ["2001"]},
        host_names={"3001": "host-a"},
    )

    detailed_log = analytics.get("detailed_log", [])
    assert isinstance(detailed_log, list)
    assert detailed_log
    assert detailed_log[0]["severity"] == "Alta"
