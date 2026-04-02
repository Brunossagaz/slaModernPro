# SLA Modern Pro

Base nova para extração de relatórios SLA do Zabbix, pensada para volume alto de dados e baixa pressão na API.

## Foco
- Cliente JSON-RPC com retry e timeout configurável
- Coleta em lotes para reduzir payloads grandes
- Serviço de relatório separado da interface
- Exportação CSV
- CLI para execução rápida e automação

## Estrutura
- `src/sla_modern_pro/client.py`: cliente Zabbix
- `src/sla_modern_pro/report.py`: motor de relatório
- `src/sla_modern_pro/cli.py`: linha de comando

## Execução
```bash
pip install -e .
sla-modern-pro --url https://zabbix.exemplo/api_jsonrpc.php --user usuario --password senha --group-id 251 --start 2026-03-01T00:00:00 --end 2026-03-31T23:59:00 --output relatorio.csv
```

## Web app interno
```bash
pip install -e .
sla-modern-pro-web
```

Depois abra `http://127.0.0.1:8000` no navegador.

## Próximos passos
- Adicionar API web opcional
- Adicionar cache local de grupos e triggers
- Otimizar cálculo incremental por host
