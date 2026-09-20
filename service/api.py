"""HTTP 接口层：路由、身份解析与统一错误响应。"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError, ValidationError
from .privacy import parse_actor
from .service import Service


class Api:
    """把 HTTP 请求分派到领域服务。"""

    def __init__(self, service: Service) -> None:
        self.svc = service
        self.routes = [
            ("GET", re.compile(r"^/health$"), self._health, False),
            ("POST", re.compile(r"^/tasks$"), self._create_task, True),
            ("GET", re.compile(r"^/tasks/([^/]+)$"), self._get_task, True),
            ("POST", re.compile(r"^/tasks/([^/]+)/reassign$"), self._reassign, True),
            ("GET", re.compile(r"^/tasks/([^/]+)/timelines$"), self._timelines, True),
            ("GET", re.compile(r"^/tasks/([^/]+)/events$"), self._task_events, True),
            ("POST", re.compile(r"^/events$"), self._ingest_event, True),
            ("POST", re.compile(r"^/events/batch$"), self._ingest_batch, True),
            ("GET", re.compile(r"^/conflicts$"), self._conflicts, True),
            ("POST", re.compile(r"^/disputes$"), self._file_dispute, True),
            ("GET", re.compile(r"^/disputes$"), self._list_disputes, True),
            ("GET", re.compile(r"^/disputes/([^/]+)$"), self._get_dispute, True),
            ("POST", re.compile(r"^/disputes/([^/]+)/decision$"), self._decide_dispute, True),
            ("POST", re.compile(r"^/settlements/run$"), self._run_settlement, True),
            ("GET", re.compile(r"^/settlements$"), self._list_runs, True),
            ("GET", re.compile(r"^/settlements/([^/]+)$"), self._get_run, True),
            (
                "GET",
                re.compile(r"^/settlements/([^/]+)/lines/([^/]+)/explain$"),
                self._explain_line,
                True,
            ),
            ("GET", re.compile(r"^/workers/([^/]+)/hours$"), self._worker_hours, True),
            ("GET", re.compile(r"^/audit-log$"), self._audit_log, True),
        ]

    def dispatch(
        self, method: str, raw_path: str, headers, body: bytes
    ) -> tuple[int, dict]:
        """分发请求，返回 (状态码, 响应体)。"""

        parts = urlsplit(raw_path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        for route_method, pattern, handler, protected in self.routes:
            if route_method != method:
                continue
            match = pattern.match(parts.path)
            if not match:
                continue
            try:
                actor = (
                    parse_actor(headers.get("X-Actor-Id"), headers.get("X-Actor-Role"))
                    if protected
                    else None
                )
                payload = None
                if method == "POST":
                    payload = self._parse_body(body)
                status, result = handler(actor, match, query, payload)
                return status, result
            except DomainError as exc:
                return exc.status, exc.to_dict()
        return 404, {"error": "not_found", "message": "接口不存在"}

    @staticmethod
    def _parse_body(body: bytes) -> dict:
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("请求体不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    # ---- 各端点 ----

    def _health(self, actor, match, query, payload):
        return 200, {"status": "ok"}

    def _create_task(self, actor, match, query, payload):
        return 201, self.svc.create_task(actor, payload)

    def _get_task(self, actor, match, query, payload):
        return 200, self.svc.get_task(actor, match.group(1))

    def _reassign(self, actor, match, query, payload):
        return 200, self.svc.reassign(actor, match.group(1), payload)

    def _timelines(self, actor, match, query, payload):
        return 200, self.svc.task_timelines(actor, match.group(1))

    def _task_events(self, actor, match, query, payload):
        return 200, self.svc.task_events(actor, match.group(1))

    def _ingest_event(self, actor, match, query, payload):
        event, created = self.svc.ingest_event(actor, payload)
        return (201 if created else 200), event

    def _ingest_batch(self, actor, match, query, payload):
        return 200, self.svc.ingest_batch(actor, payload)

    def _conflicts(self, actor, match, query, payload):
        return 200, self.svc.list_conflicts(
            actor,
            base_id=query.get("base_id"),
            worker_id=query.get("worker_id"),
            kind=query.get("kind"),
        )

    def _file_dispute(self, actor, match, query, payload):
        return 201, self.svc.file_dispute(actor, payload)

    def _list_disputes(self, actor, match, query, payload):
        return 200, self.svc.list_disputes(
            actor, status=query.get("status"), worker_id=query.get("worker_id")
        )

    def _get_dispute(self, actor, match, query, payload):
        return 200, self.svc.get_dispute(actor, match.group(1))

    def _decide_dispute(self, actor, match, query, payload):
        return 200, self.svc.decide_dispute(actor, match.group(1), payload)

    def _run_settlement(self, actor, match, query, payload):
        return 201, self.svc.run_settlement(actor, payload)

    def _list_runs(self, actor, match, query, payload):
        return 200, self.svc.list_runs(actor)

    def _get_run(self, actor, match, query, payload):
        return 200, self.svc.get_run(actor, match.group(1))

    def _explain_line(self, actor, match, query, payload):
        return 200, self.svc.explain_line(actor, match.group(1), match.group(2))

    def _worker_hours(self, actor, match, query, payload):
        return 200, self.svc.worker_hours(
            actor,
            match.group(1),
            query.get("from", "1970-01-01"),
            query.get("to", "2999-12-31"),
        )

    def _audit_log(self, actor, match, query, payload):
        try:
            limit = int(query.get("limit", "200"))
        except ValueError as exc:
            raise ValidationError("limit 必须是整数") from exc
        return 200, self.svc.audit_log(actor, limit=limit)
