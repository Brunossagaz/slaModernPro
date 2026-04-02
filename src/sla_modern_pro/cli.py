from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .client import ZabbixJsonRpcClient
from .report import ReportService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SLA Modern Pro")
    parser.add_argument("--url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--start", required=True, help="ISO datetime, ex: 2026-03-01T00:00:00")
    parser.add_argument("--end", required=True, help="ISO datetime, ex: 2026-03-31T23:59:00")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start_ts = int(datetime.fromisoformat(args.start).timestamp())
    end_ts = int(datetime.fromisoformat(args.end).timestamp())

    client = ZabbixJsonRpcClient(args.url, args.user, args.password)
    service = ReportService(client)
    report = service.generate_group_report(args.group_id, start_ts, end_ts)
    output_path = service.export_csv(report, Path(args.output))
    print(f"Relatório exportado para {output_path}")


if __name__ == "__main__":
    main()
