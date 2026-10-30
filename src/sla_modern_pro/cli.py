from __future__ import annotations

from datetime import datetime
from getpass import getpass
from pathlib import Path
import argparse
import os

from .client import ZabbixJsonRpcClient
from .forms import validate_zabbix_url
from .report import ReportService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SLA Modern Pro",
        epilog="A senha é lida da variável ZABBIX_PASSWORD ou pedida no terminal "
        "(não vai na linha de comando, que fica visível no histórico e em 'ps').",
    )
    parser.add_argument("--url", default=os.environ.get("SLA_ZABBIX_DEFAULT_URL"), required=not os.environ.get("SLA_ZABBIX_DEFAULT_URL"))
    parser.add_argument("--user", default=os.environ.get("ZABBIX_USER"), required=not os.environ.get("ZABBIX_USER"))
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--start", required=True, help="ISO datetime, ex: 2026-03-01T00:00:00")
    parser.add_argument("--end", required=True, help="ISO datetime, ex: 2026-03-31T23:59:00")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    url = validate_zabbix_url(args.url)
    password = os.environ.get("ZABBIX_PASSWORD") or getpass(f"Senha do Zabbix para {args.user}: ")
    start_ts = int(datetime.fromisoformat(args.start).timestamp())
    end_ts = int(datetime.fromisoformat(args.end).timestamp())

    with ZabbixJsonRpcClient(url, args.user, password) as client:
        service = ReportService(client)
        report = service.generate_group_report(args.group_id, start_ts, end_ts)
        output_path = service.export_csv(report, Path(args.output))
    print(f"Relatório exportado para {output_path}")


if __name__ == "__main__":
    main()
