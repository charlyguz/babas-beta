import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from babas import __version__
from babas.bridge.protocol import (
    DEFAULT_PORT,
    PINNED_EXTENSION_ID,
    PROTOCOL_VERSION,
    Envelope,
    Hello,
    ResultMessage,
    new_id,
    parse_incoming,
    request,
)
from babas.drivers.base import DriverError, ErrorCode

log = logging.getLogger("babas.bridge")

EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]
HELLO_TIMEOUT = 5.0
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


NOT_PAIRED = "not paired"


def default_origins() -> List[str]:
    return [f"chrome-extension://{PINNED_EXTENSION_ID}"]


def default_pairing_file() -> Path:
    home = os.environ.get("BABAS_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".babas"
    return base / "pairing.json"


def reset_pairing(path: Path) -> bool:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True


def _read_token(raw: str) -> Optional[str]:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pairing-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"token": token}))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _describe_error(exc: Exception) -> str:
    # Pydantic messages embed the offending input, which for a hello may hold the pairing token.
    if isinstance(exc, ValidationError):
        problems = sorted({f"{err['type']} at {'.'.join(str(part) for part in err['loc']) or 'frame'}" for err in exc.errors()})
        return "invalid frame (" + "; ".join(problems) + ")"
    return str(exc) or type(exc).__name__


class BridgeServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        allowed_origins: Optional[List[str]] = None,
        pairing_file: Optional[Path] = None,
    ):
        self.host = host
        self.port = port
        self.allowed_origins = allowed_origins or default_origins()
        self.pairing_file = Path(pairing_file) if pairing_file is not None else None
        self.extension_version: Optional[str] = None
        self._server: Optional[Server] = None
        self._conn: Optional[ServerConnection] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._handlers: Dict[str, List[EventHandler]] = {}
        self._connected = asyncio.Event()
        self._tasks: set = set()
        self._pairing_warned: set = set()

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._connected.is_set()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self.host,
            self.port,
            origins=self.allowed_origins,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_MESSAGE_BYTES,
        )
        sockets = list(self._server.sockets)
        if sockets:
            self.port = sockets[0].getsockname()[1]
        log.info("bridge listening on %s", self.url)

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.close(1001, "server stopping")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._fail_pending(ErrorCode.NO_EXTENSION, "The bridge stopped")

    async def wait_connected(self, timeout: Optional[float] = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def on(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def request(self, type: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        conn = self._conn
        if conn is None or not self._connected.is_set():
            raise DriverError(ErrorCode.NO_EXTENSION, "The BABAS extension is not connected. Open Chrome with the extension installed.")
        envelope = request(type, payload)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[envelope.id] = future
        try:
            await conn.send(envelope.model_dump_json())
            result: ResultMessage = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise DriverError(ErrorCode.TIMEOUT, f"The extension did not answer '{type}' within {timeout:.0f} s") from None
        except ConnectionClosed:
            raise DriverError(ErrorCode.NO_EXTENSION, "The extension disconnected") from None
        finally:
            self._pending.pop(envelope.id, None)
        if not result.ok:
            error = result.error
            raise DriverError(error.code if error else ErrorCode.INTERNAL, error.message if error else "Unknown error", error.data if error else None)
        return result.payload or {}

    def _fail_pending(self, code: str, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(DriverError(code, message))
        self._pending.clear()

    async def _handshake(self, conn: ServerConnection) -> Optional[Hello]:
        try:
            raw = await asyncio.wait_for(conn.recv(), HELLO_TIMEOUT)
            envelope = Envelope.model_validate_json(raw)
            if envelope.type != "hello":
                raise ValueError("first message must be hello")
            hello = Hello.model_validate(envelope.payload)
        except (asyncio.TimeoutError, ValueError, ValidationError, ConnectionClosed) as exc:
            log.log(logging.DEBUG if isinstance(exc, ConnectionClosed) else logging.WARNING, "rejected bridge client: %s", _describe_error(exc))
            await conn.close(1008, "hello expected")
            return None
        if hello.protocol != PROTOCOL_VERSION:
            await conn.close(1008, f"protocol {hello.protocol} not supported (server speaks {PROTOCOL_VERSION})")
            return None
        return hello

    def _warn_pairing(self, reason: str) -> None:
        if reason not in self._pairing_warned:
            self._pairing_warned.add(reason)
            log.warning("rejected bridge client: %s. Fix or delete the pairing file (python -m babas pair --reset)", reason)

    def _check_pairing(self, hello: Hello) -> Tuple[bool, Optional[str]]:
        path = self.pairing_file
        if path is None:
            return True, None
        try:
            raw: Optional[str] = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError) as exc:
            self._warn_pairing(f"pairing file unreadable ({type(exc).__name__})")
            return False, None
        if raw is None:
            token = secrets.token_urlsafe(32)
            try:
                _write_token(path, token)
            except OSError as exc:
                self._warn_pairing(f"pairing file not writable ({type(exc).__name__})")
                return False, None
            log.info("created a new pairing")
            return True, token
        stored = _read_token(raw)
        if stored is None:
            self._warn_pairing("pairing file is corrupt")
            return False, None
        presented = (hello.token or "").encode("utf-8")
        if not hmac.compare_digest(presented, stored.encode("utf-8")):
            self._warn_pairing("extension is not paired")
            return False, None
        return True, None

    async def _handle(self, conn: ServerConnection) -> None:
        hello = await self._handshake(conn)
        if hello is None:
            return
        accepted, new_token = self._check_pairing(hello)
        if not accepted:
            await conn.close(1008, NOT_PAIRED)
            return
        self._pairing_warned.clear()
        previous = self._conn
        self._conn = conn
        if previous is not None:
            self._fail_pending(ErrorCode.NO_EXTENSION, "The extension reconnected")
            await previous.close(1000, "replaced by a new connection")
        self.extension_version = hello.ext_version
        welcome_payload: Dict[str, Any] = {"server_version": __version__}
        if new_token is not None:
            welcome_payload["token"] = new_token
        welcomed = False
        try:
            await conn.send(Envelope(id=new_id(), type="welcome", payload=welcome_payload).model_dump_json())
            welcomed = True
            self._connected.set()
            log.info("extension connected (v%s)", hello.ext_version)
            async for raw in conn:
                self._on_message(raw)
        except ConnectionClosed:
            pass
        finally:
            if not welcomed and new_token is not None and self.pairing_file is not None:
                with contextlib.suppress(OSError):
                    reset_pairing(self.pairing_file)
            if self._conn is conn:
                self._conn = None
                self._connected.clear()
                self._fail_pending(ErrorCode.NO_EXTENSION, "The extension disconnected")
                log.info("extension disconnected")

    def _on_message(self, raw: Any) -> None:
        try:
            message = parse_incoming(json.loads(raw))
        except (ValueError, ValidationError):
            log.warning("ignored malformed bridge message")
            return
        if isinstance(message, ResultMessage):
            future = self._pending.get(message.id)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if message.type == "ping":
            return
        for handler in self._handlers.get(message.type, []):
            task = asyncio.create_task(self._run_handler(handler, message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_handler(self, handler: EventHandler, message: Envelope) -> None:
        try:
            await handler(message.payload)
        except Exception:
            log.exception("bridge event handler for %s failed", message.type)
