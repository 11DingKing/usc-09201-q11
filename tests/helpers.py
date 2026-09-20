"""测试共享辅助：内存客户端与常用报文。"""

from __future__ import annotations

import json

from service.api import Api
from service.service import Service


class Client:
    """直接调用 Api.dispatch 的测试客户端（不走网络）。"""

    def __init__(self, service: Service | None = None) -> None:
        self.service = service or Service()
        self.api = Api(self.service)

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        actor: str | None = "admin-1",
        role: str = "base_admin",
    ) -> tuple[int, dict]:
        headers = {}
        if actor is not None:
            headers["X-Actor-Id"] = actor
            headers["X-Actor-Role"] = role
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        return self.api.dispatch(method, path, headers, body)

    def post(self, path: str, payload: dict, **kwargs) -> tuple[int, dict]:
        return self.request("POST", path, payload, **kwargs)

    def get(self, path: str, **kwargs) -> tuple[int, dict]:
        return self.request("GET", path, None, **kwargs)


def task_payload(task_id: str = "T-1", **overrides) -> dict:
    payload = {
        "task_id": task_id,
        "base_id": "base-a",
        "plot_id": "plot-1",
        "plot_location": {"lat": 29.4761, "lon": 109.3182},
        "team_id": "team-1",
        "title": "油茶林除草",
        "plan_start": "2026-09-18T00:00:00+08:00",
        "plan_end": "2026-09-21T00:00:00+08:00",
        "worker_ids": ["W1", "W2"],
    }
    payload.update(overrides)
    return payload


def event_payload(event_id: str, **overrides) -> dict:
    payload = {
        "event_id": event_id,
        "task_id": "T-1",
        "worker_id": "W1",
        "kind": "start",
        "occurred_at": "2026-09-19T08:00:00+08:00",
        "source": "leader-phone-a",
    }
    payload.update(**overrides)
    return payload


def create_task(client: Client, task_id: str = "T-1", **overrides) -> dict:
    status, body = client.post("/tasks", task_payload(task_id, **overrides))
    assert status == 201, body
    return body
