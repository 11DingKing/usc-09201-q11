"""林业灵活用工核验服务入口。

运行 `python3 -m service.main` 后访问 `/health` 确认服务状态；
设置环境变量 `JOURNAL_FILE` 可开启 JSONL 日志持久化，重启后自动重放。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import Api
from .service import Service
from .store import Store


def create_server(
    host: str = "0.0.0.0", port: int = 0, service: Service | None = None
) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    if service is None:
        service = Service(Store(journal_path=os.environ.get("JOURNAL_FILE") or None))
    api = Api(service)

    class Handler(BaseHTTPRequestHandler):
        """处理核验服务 HTTP 请求。"""

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = api.dispatch(self.command, self.path, self.headers, body)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _dispatch  # noqa: N802
        do_POST = _dispatch  # noqa: N802

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
