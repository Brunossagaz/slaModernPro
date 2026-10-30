# SLA Modern Pro

Aplicação interna para geração de relatórios SLA do Zabbix com foco em confiabilidade, transparência de cálculo e baixa pressão na API.

## O que o app faz
- Consulta host groups e subgrupos no Zabbix
- Gera disponibilidade por host (downtime, uptime, disponibilidade)
- Calcula MTTA e MTTR globais e por host
- Permite filtrar disponibilidade por uma ou mais triggers específicas
- Exporta CSV do relatório final
- Exporta CSV de auditoria com base de cálculo (eventos e intervalos)
- Aba "Status de Cluster": tempo que cada item de status (ex.: saúde do Cloudera) ficou em cada estado no período

## Arquitetura
- Cliente Zabbix JSON-RPC com retry/backoff e timeout
- Serviço de cálculo desacoplado da interface
- Web app interno (FastAPI + Jinja2)
- Coleta em lotes para reduzir risco de timeout/502

## Estrutura
- `src/sla_modern_pro/client.py`: cliente Zabbix e métodos de coleta
- `src/sla_modern_pro/report.py`: regras de negócio e cálculo SLA/MTTA/MTTR
- `src/sla_modern_pro/state_report.py`: cálculo de tempo por estado (aba Status de Cluster)
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

### Tempo por estado (aba Status de Cluster)
- Base: histórico (`history.get`) dos itens cuja chave foi informada, nos hosts do grupo e subgrupos
- Chave exata ou com `*` como curinga (ex.: `cloudera.service.health[*]`); uma por linha
- Cada valor coletado vale até o próximo valor, limitado pela tolerância sem dados; o restante vira `SEM DADOS`
- O estado no início do período vem do último valor anterior ao início
- Itens numéricos com value map exibem o texto mapeado
- Período no futuro é cortado no instante atual
- Aviso quando a retenção de histórico do item é menor que a distância até o início do período
  (o trecho sem histórico aparece como `SEM DADOS`)

## Como rodar (Docker, recomendado)

Pré-requisito: Docker (no Windows, Docker Desktop com WSL2).

```bash
git clone https://github.com/Brunossagaz/slaModernPro.git
cd slaModernPro
cp .env.example .env    # no PowerShell: Copy-Item .env.example .env
```

Edite o `.env` e troque `zabbix.exemplo.com.br` pelo endereço do Zabbix que você vai consultar
(`SLA_ZABBIX_DEFAULT_URL` e `SLA_ZABBIX_ALLOWED_HOSTS`). Depois:

```bash
docker compose up -d --build
```

Abra `http://localhost:8000` e entre com **seu** usuário do Zabbix (de preferência somente leitura).

Para atualizar depois: `git pull` e `docker compose up -d --build`.
Para parar: `docker compose down`.

## Execução (sem Docker)
```powershell
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

Por padrão o `sla-modern-pro-web` agora sobe **sem** hot-reload (evita rodar
o watcher de arquivos fora de ambiente de dev). Para reativar localmente:

```powershell
$env:SLA_MODERN_PRO_RELOAD = "1"
sla-modern-pro-web
```

## Docker

```bash
docker compose build
docker compose up -d
docker compose logs -f sla-modern-pro
curl http://localhost:8000/health
```

- A imagem roda como usuário não-root, com filesystem raiz somente leitura
  (só `/tmp` é gravável, via `tmpfs`, onde ficam os CSVs temporários) e sem
  capabilities Linux extras.
- A porta é publicada só em `127.0.0.1:8000` (não fica visível para a LAN).
  Veja os comentários em `docker-compose.yml` antes de mudar isso.
- `docker compose down` remove o container e, junto com ele, qualquer CSV
  temporário gerado (fica em `tmpfs`, não persiste em disco).
- Ajustes de uma máquina só (ex.: ligar a app à rede Docker de um Zabbix local)
  vão em `docker-compose.override.yml`, que o compose aplica sozinho e que fica
  fora do git. Exemplo para alcançar um container `zabbix-frontend`:

  ```yaml
  services:
    sla-modern-pro:
      networks: [default, zabbix]
  networks:
    zabbix:
      external: true
      name: zabbix_network-zabbix
  ```

  Nesse caso, coloque `SLA_INSECURE_ZABBIX_HOSTS=zabbix-frontend` no `.env` se o
  frontend local for só HTTP.

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
A senha não vai na linha de comando (ficaria no histórico do shell e visível em `ps`):
ela é lida de `ZABBIX_PASSWORD` ou pedida no terminal.
```powershell
sla-modern-pro --url https://zabbix.exemplo/api_jsonrpc.php --user usuario --group-id 251 --start 2026-03-01T00:00:00 --end 2026-03-31T23:59:00 --output relatorio.csv
```

## Configuração
Copie `.env.example` para `.env` e ajuste. O `.env` está no `.gitignore` e no
`.dockerignore`. Nenhuma senha vai nele: a do Zabbix é digitada na tela.
O mais importante é `SLA_ZABBIX_ALLOWED_HOSTS` (servidores Zabbix que a app pode chamar).

## Segurança

Controles na aplicação:
- **Sessão no servidor**: a senha do Zabbix fica só na memória do servidor, ligada a um
  cookie `HttpOnly`, `SameSite=Strict` (e `Secure` em HTTPS), com expiração por inatividade
  e absoluta. Ela nunca volta para o HTML. "Desconectar" apaga a senha e os relatórios da sessão.
- **Downloads** só para a sessão que gerou o relatório (sem ela, 404).
- **Login no Zabbix**: bloqueio temporário por IP e por usuário após falhas seguidas;
  a sessão aberta no Zabbix é encerrada (`user.logout`) ao fim de cada uso.
- **SSRF**: a URL precisa ser a API (`/api_jsonrpc.php`), em HTTPS, sem credenciais na URL
  e, se configurado, num servidor de `SLA_ZABBIX_ALLOWED_HOSTS`.
- **CSRF e DNS rebinding**: POST vindo de outro site é recusado (Origin/Sec-Fetch-Site) e o
  cabeçalho `Host` precisa estar em `SLA_ALLOWED_HOSTS`.
- **Validação no servidor** (Pydantic) de tipo, formato e tamanho de todos os campos,
  período máximo de 400 dias e limite de relatórios simultâneos.
- **Cabeçalhos**: CSP sem script inline, `X-Frame-Options`, `X-Content-Type-Options`,
  `Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store` e HSTS quando em HTTPS.
- **CSV**: células que começam com `= + - @` recebem `'` na frente (sem injeção de fórmula no Excel).
- Sem `/docs` e `/openapi.json`; mensagens de erro nunca incluem a senha.

Containerização:
- Usuário não-root, filesystem raiz somente leitura, `cap_drop: ALL`, `no-new-privileges`.
- Porta publicada só em `127.0.0.1`; fora do Docker, `sla-modern-pro-web` também escuta só em `127.0.0.1`.
- Imagem final sem `pip`; dependências pinadas em `requirements-lock.txt` e verificadas com `pip-audit`.
- `/tmp` como `tmpfs` limitado; CSVs expiram em 6h.

Riscos aceitos (uso local, só em `localhost`):
- **Sem login próprio**: quem alcança `localhost:8000` usa a interface. Antes de expor na
  rede é obrigatório colocar um reverse proxy com TLS e autenticação (SSO/basic auth) e
  ajustar `SLA_ALLOWED_HOSTS` e `SLA_COOKIE_SECURE=1`.
- A senha do Zabbix fica em memória enquanto a sessão existir. Use um usuário Zabbix
  dedicado e **somente leitura**.

## Roadmap sugerido
- Filtro por host na exportação de auditoria
- Histórico de execuções no próprio web app
- Exportação Excel com múltiplas abas
