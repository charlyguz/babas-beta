import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional

CHAT_PATH = "/v1/chat/completions"


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: Dict[str, str]
    body: Any

    @property
    def response_format(self) -> Optional[dict]:
        return self.body.get("response_format") if isinstance(self.body, dict) else None

    @property
    def format_type(self) -> Optional[str]:
        fmt = self.response_format
        return fmt.get("type") if isinstance(fmt, dict) else None


@dataclass
class _Reply:
    status: int
    body: Any
    headers: Dict[str, str] = field(default_factory=dict)


def error_body(message: str, code: Optional[str] = None, error_type: str = "invalid_request_error") -> dict:
    error: Dict[str, Any] = {"message": message, "type": error_type}
    if code is not None:
        error["code"] = code
    return {"error": error}


def _completion_body(content: Optional[str], finish_reason: str, refusal: Optional[str]) -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 1,
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeOpenAIServer:
    def __init__(self, reject_types: Iterable[str] = (), delay: float = 0.0) -> None:
        self.reject_types = set(reject_types)
        self.delay = delay
        self.requests: List[RecordedRequest] = []
        self._replies: List[_Reply] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server is not started"
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def format_types(self) -> List[Optional[str]]:
        with self._lock:
            return [request.format_type for request in self.requests]

    def queue(self, *contents: Optional[str]) -> None:
        for content in contents:
            self.queue_completion(content)

    def queue_completion(
        self, content: Optional[str], *, finish_reason: str = "stop", refusal: Optional[str] = None, times: int = 1
    ) -> None:
        self.queue_reply(200, _completion_body(content, finish_reason, refusal), times=times)

    def queue_error(
        self,
        status: int,
        message: str = "Something went wrong",
        *,
        code: Optional[str] = None,
        times: int = 1,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.queue_reply(status, error_body(message, code), headers=headers, times=times)

    def queue_reply(self, status: int, body: Any, *, headers: Optional[Dict[str, str]] = None, times: int = 1) -> None:
        reply_headers = dict(headers or {})
        if status == 429 or status >= 500:
            reply_headers.setdefault("retry-after-ms", "1")
        with self._lock:
            self._replies.extend(_Reply(status, body, dict(reply_headers)) for _ in range(times))

    def start(self) -> "FakeOpenAIServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body: Any = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_unparsed": raw.decode("utf-8", "replace")}
                headers = {name.lower(): value for name, value in self.headers.items()}
                reply = owner._answer(RecordedRequest(self.command, self.path.split("?")[0], headers, body))
                if owner.delay:
                    owner._stopping.wait(owner.delay)
                data = json.dumps(reply.body).encode("utf-8")
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for name, value in reply.headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return None

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> "FakeOpenAIServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _answer(self, request: RecordedRequest) -> _Reply:
        with self._lock:
            self.requests.append(request)
            if request.path != CHAT_PATH:
                return _Reply(404, error_body(f"unknown route {request.path}", "not_found", "not_found_error"))
            if request.format_type in self.reject_types:
                message = f"response_format {request.format_type} is not supported"
                return _Reply(400, error_body(message))
            if self._replies:
                return self._replies.pop(0)
            return _Reply(418, error_body("the fake server has no queued reply"))
