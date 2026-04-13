from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import time
import requests


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
        self.authenticate(user, password)

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
        if auth_required and self.auth_token:
            payload["auth"] = self.auth_token

        last_error: Exception | None = None
        for attempt in range(1, self.retry_policy.attempts + 1):
            try:
                response = self.session.post(self.url, json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    raise RuntimeError(str(body["error"]))
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
                "output": ["triggerid", "status", "description"],
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
