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
