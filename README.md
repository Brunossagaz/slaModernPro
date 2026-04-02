# SLA Modern Pro

Aplicação interna para geração de relatórios SLA do Zabbix com foco em confiabilidade, transparência de cálculo e baixa pressão na API.

## O que o app faz
- Consulta host groups e subgrupos no Zabbix
- Gera disponibilidade por host (downtime, uptime, disponibilidade)
- Calcula MTTA e MTTR globais e por host
- Permite filtrar disponibilidade por uma ou mais triggers específicas
- Exporta CSV do relatório final
- Exporta CSV de auditoria com base de cálculo (eventos e intervalos)

## Arquitetura
- Cliente Zabbix JSON-RPC com retry/backoff e timeout
- Serviço de cálculo desacoplado da interface
- Web app interno (FastAPI + Jinja2)
- Coleta em lotes para reduzir risco de timeout/502

## Estrutura
- `src/sla_modern_pro/client.py`: cliente Zabbix e métodos de coleta
- `src/sla_modern_pro/report.py`: regras de negócio e cálculo SLA/MTTA/MTTR
- `src/sla_modern_pro/web.py`: backend web (FastAPI)
- `src/sla_modern_pro/templates/index.html`: interface
- `src/sla_modern_pro/static/styles.css`: estilos da interface
- `tests/`: testes iniciais

## Regras de cálculo

### Disponibilidade (Downtime/Uptime)
- Base: triggers filtradas pelo usuário
- Downtime por host: união de intervalos de problema para evitar dupla contagem entre triggers
- Uptime: período total - downtime
- Disponibilidade: uptime / período total * 100

### MTTA/MTTR
- Base: todos os alertas do host no período (triggers habilitadas), independente do filtro de trigger
- MTTA: tempo entre abertura do problema e primeiro acknowledge
- MTTR: tempo entre abertura e resolução do problema
- O app tenta usar `selectAcknowledges` quando suportado pela API; se não suportado, faz fallback automático

### Validações aplicadas
- Hosts desabilitados não entram no cálculo
- Triggers desabilitadas não entram no cálculo
- Triggers com item desabilitado não entram no cálculo

## Execução
```powershell
cd "C:\Users\bruno.sagaz\OneDrive - compwire.com.br\Documentos\Scripts\Relatórios\Desenvolvendo\slaModernPro"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## Web app interno
```powershell
sla-modern-pro-web
```

Abra no navegador:

`http://127.0.0.1:8000`

## Uso rápido
1. Preencha URL, usuário e senha da API Zabbix
2. Carregue os grupos
3. Selecione o grupo e período
4. Opcional: informe triggers específicas (uma por linha, vírgula ou ponto e vírgula)
5. Gere o relatório
6. Baixe:
- CSV do relatório
- CSV da base de cálculo (auditoria)

## Exportações
- CSV do relatório:
	- downtime, uptime, disponibilidade por host
	- MTTA e MTTR por host
	- resumo global de MTTA/MTTR e contagem de problemas
- CSV da base de cálculo:
	- eventos usados
	- intervalos por trigger antes da união
	- intervalos finais por host após união
	- itens ignorados por validação (host/trigger/item desabilitado)

## Troubleshooting
- Erro 500 ao abrir a home:
	- Reinicie o serviço e confirme dependências instaladas (`pip install -e .`)
- Imports FastAPI/Uvicorn/Requests não resolvidos no editor:
	- Selecione o interpretador da `.venv` no VS Code
- Diferença de valores vs planilha:
	- Baixe o CSV de base de cálculo e compare os intervalos por host
- API com 502/timeout:
	- O app já usa retry e lotes, mas instabilidade do Zabbix/proxy pode afetar execução

## CLI (opcional)
```powershell
sla-modern-pro --url https://zabbix.exemplo/api_jsonrpc.php --user usuario --password senha --group-id 251 --start 2026-03-01T00:00:00 --end 2026-03-31T23:59:00 --output relatorio.csv
```

## Roadmap sugerido
- Filtro por host na exportação de auditoria
- Histórico de execuções no próprio web app
- Exportação Excel com múltiplas abas
