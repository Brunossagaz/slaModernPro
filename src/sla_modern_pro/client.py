from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import re
import time
import requests


class ZabbixApiError(RuntimeError):
    """Erro devolvido pela própria API do Zabbix (ex.: login inválido), não falha de rede."""


# A partir do 6.4 o Zabbix aceita o token no cabeçalho "Authorization: Bearer"; o campo
# "auth" no corpo foi depreciado e o 7.2+ passou a recusá-lo ("unexpected parameter auth").
BEARER_AUTH_MIN_VERSION = (6, 4)


def parse_version(raw: object) -> tuple[int, ...]:
    parts = []
    for piece in str(raw).strip().split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
        if match.end() < len(piece):
            break  # sufixo tipo "rc1": ignora o resto
    if not parts:
        raise ValueError(f"Versão do Zabbix não reconhecida: {raw!r}")
    return tuple(parts)


@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 6
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0


class ZabbixJsonRpcClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        timeout_seconds: int = 120,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy()
        self.session = requests.Session()
        self.auth_token: str | None = None
        self._request_id = 0
        self.use_bearer_auth = False
        self.api_version = self._detect_version()
        self.use_bearer_auth = self.api_version is not None and self.api_version >= BEARER_AUTH_MIN_VERSION
        self.authenticate(user, password)

    def _detect_version(self) -> tuple[int, ...] | None:
        """apiinfo.version não exige login; None se o servidor não informar (usa o formato antigo)."""
        try:
            return parse_version(self.call("apiinfo.version", {}, auth_required=False))
        except (ZabbixApiError, ValueError):
            return None

    def authenticate(self, user: str, password: str) -> str:
        result = self.call("user.login", {"username": user, "password": password}, auth_required=False)
        self.auth_token = str(result)
        return self.auth_token

    def call(self, method: str, params: dict[str, Any] | None = None, auth_required: bool = True) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._next_id(),
        }
        headers: dict[str, str] = {}
        if auth_required and self.auth_token:
            if self.use_bearer_auth:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            else:
                payload["auth"] = self.auth_token

        last_error: Exception | None = None
        for attempt in range(1, self.retry_policy.attempts + 1):
            try:
                response = self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout_seconds)
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    raise ZabbixApiError(str(body["error"]))
                return body.get("result")
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_policy.attempts or not self._is_retriable(exc):
                    raise
                delay = min(
                    self.retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
                    self.retry_policy.max_delay_seconds,
                )
                time.sleep(delay)

        if last_error:
            raise last_error
        raise RuntimeError("Zabbix API call failed without exception")

    def logout(self) -> None:
        """Encerra a sessão aberta no Zabbix (senão cada relatório deixa uma sessão ativa lá)."""
        if not self.auth_token:
            return
        try:
            self.call("user.logout", {})
        except Exception:
            pass
        finally:
            self.auth_token = None
            self.session.close()

    def __enter__(self) -> "ZabbixJsonRpcClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.logout()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _is_retriable(exc: Exception) -> bool:
        message = str(exc).lower()
        retriable_tokens = (
            "502",
            "503",
            "504",
            "bad gateway",
            "gateway timeout",
            "service unavailable",
            "timeout",
            "timed out",
            "connection reset",
        )
        return any(token in message for token in retriable_tokens)

    def chunked_call(self, method: str, params_list: Iterable[dict[str, Any]]) -> list[Any]:
        results: list[Any] = []
        for params in params_list:
            result = self.call(method, params)
            if isinstance(result, list):
                results.extend(result)
            else:
                results.append(result)
        return results

    def get_hostgroups(self) -> list[dict[str, Any]]:
        return self.call("hostgroup.get", {"output": ["groupid", "name"], "sortfield": "name"})

    def get_group_name(self, group_id: str) -> str:
        result = self.call("hostgroup.get", {"groupids": [int(group_id)], "output": ["name"]})
        if not result:
            raise RuntimeError(f"Host group not found: {group_id}")
        return str(result[0]["name"])

    def get_subgroup_ids(self, group_name: str) -> list[str]:
        result = self.call(
            "hostgroup.get",
            {
                "search": {"name": f"{group_name}/"},
                "output": ["groupid"],
            },
        )
        return [str(item["groupid"]) for item in result]

    def get_group_tree_ids(self, group_id: str) -> tuple[str, list[int]]:
        group_name = self.get_group_name(group_id)
        subgroup_ids = self.get_subgroup_ids(group_name)
        return group_name, [int(group_id), *[int(subgroup_id) for subgroup_id in subgroup_ids]]

    def get_hosts(self, group_ids: list[int]) -> list[dict[str, Any]]:
        return self.call(
            "host.get",
            {
                "output": ["hostid", "name", "status"],
                "groupids": group_ids,
                "sortfield": "name",
                "selectTriggers": ["triggerid", "description", "status"],
            },
        )

    def get_trigger_details(self, trigger_ids: list[str]) -> list[dict[str, Any]]:
        if not trigger_ids:
            return []
        return self.call(
            "trigger.get",
            {
                "triggerids": trigger_ids,
                "output": ["triggerid", "status", "description", "priority"],
                "selectItems": ["itemid", "name", "status"],
            },
        )

    def get_events(
        self,
        trigger_ids: list[str],
        time_from: int | None = None,
        time_till: int | None = None,
        sortorder: str = "ASC",
        include_acknowledges: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "source": 0,
            "objectids": trigger_ids,
            "sortfield": "clock",
            "sortorder": sortorder,
            "output": ["eventid", "objectid", "clock", "value"],
        }
        if include_acknowledges:
            # "extend" tende a ser mais compatível entre versões de API
            params["selectAcknowledges"] = "extend"
        if time_from is not None:
            params["time_from"] = time_from
        if time_till is not None:
            params["time_till"] = time_till
        return self.call("event.get", params)

    def get_items(self, group_ids: list[int], key: str, include_valuemap: bool = True) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "output": ["itemid", "hostid", "name", "key_", "value_type", "history", "status"],
            "groupids": group_ids,
            "templated": False,
            "selectHosts": ["hostid", "name", "status"],
            "sortfield": "name",
        }
        if "*" in key:
            params["search"] = {"key_": key}
            params["searchWildcardsEnabled"] = True
        else:
            params["filter"] = {"key_": key}
        if include_valuemap:
            params["selectValueMap"] = ["mappings"]
        return self.call("item.get", params)

    def get_history(
        self,
        item_ids: list[str],
        value_type: int,
        time_from: int | None = None,
        time_till: int | None = None,
        sortorder: str = "ASC",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "output": ["itemid", "clock", "value"],
            "history": value_type,
            "itemids": item_ids,
            "sortfield": "clock",
            "sortorder": sortorder,
        }
        if time_from is not None:
            params["time_from"] = time_from
        if time_till is not None:
            params["time_till"] = time_till
        if limit is not None:
            params["limit"] = limit
        return self.call("history.get", params)
