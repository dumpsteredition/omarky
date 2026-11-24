"""Codex app-server route for OMarky.

Only a Codex TUI launched by ``bin/omarky-codex`` can register a socket. The
adapter never creates, resumes, or forks a conversation while discovering
routes. A send is always addressed to the explicit, currently loaded thread ID.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Tuple


_CONNECTOR = "codex"
_CLIENT_INFO = {"name": "omarky", "title": "OMarky", "version": "0.1.0"}
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HTTP_HEADER = 16 * 1024
_MAX_WS_MESSAGE = 64 * 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_RPC_TIMEOUT = 8.0
_WAIT_REPLY_TIMEOUT = 180.0
_POLL_INTERVAL = 0.35


class _RouteUnavailable(Exception):
    """The paired app-server is no longer reachable or owned by the TUI."""


class _ProtocolFailure(Exception):
    """The local Codex app-server returned data we cannot safely interpret."""


class _RPCFailure(Exception):
    def __init__(self, code: Any, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else -1


def _private_directory(path: Path, *, create: bool) -> Optional[Path]:
    """Return an owner-only directory, refusing symlinks and shared paths."""
    try:
        if create:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        if _uid() >= 0 and info.st_uid != _uid():
            return None
        if info.st_mode & 0o077:
            return None
        return path
    except OSError:
        return None


def runtime_root(*, create: bool = False) -> Optional[Path]:
    """Private registry root shared by the launcher and the Codex adapter."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        parent = Path(xdg)
        try:
            info = parent.lstat()
        except OSError:
            return None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (_uid() >= 0 and info.st_uid != _uid())
                or info.st_mode & 0o077):
            return None
        root = parent / "omarky-codex"
    else:
        # XDG_RUNTIME_DIR is optional. Keep the fallback under a private uid
        # directory in the sticky system temp root, never in a shared flat path.
        root = Path(tempfile.gettempdir()) / ("omarky-codex-%s" % _uid())
    return _private_directory(root, create=create)


def _proc_start_time(pid: int) -> Optional[str]:
    """Linux process start ticks, used to reject stale leases after PID reuse."""
    try:
        raw = Path("/proc/%d/stat" % pid).read_text(encoding="ascii")
        end = raw.rfind(")")
        fields = raw[end + 2 :].split()
        # fields[0] is proc field 3 (state); starttime is field 22.
        return fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _process_commandline(pid: int) -> Optional[List[str]]:
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
        return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]
    except OSError:
        return None


def _is_launcher_process(pid: int, expected_start: str) -> bool:
    """Require the live owner process to be this exact launcher, not just a PID."""
    actual_start = _proc_start_time(pid)
    argv = _process_commandline(pid)
    if actual_start is None or actual_start != expected_start or not argv:
        return False
    expected_script = Path(__file__).resolve().parent.parent / "bin" / "omarky-codex"
    try:
        cwd = Path(os.readlink("/proc/%d/cwd" % pid))
    except OSError:
        cwd = Path("/")
    for arg in argv:
        if Path(arg).name != "omarky-codex":
            continue
        candidate = Path(arg)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            if candidate.resolve() == expected_script:
                return True
        except OSError:
            continue
    return False


def _codex_socket_is_private(socket_path: Path, root: Path) -> bool:
    """Validate Codex's custom-socket alias and its owner-only backing socket.

    Codex 0.172 creates the requested socket path as a symlink to a random
    socket under /tmp/codex-daemon-<uid>. Both sides are checked; a generic
    symlink or shared backing directory is never followed.
    """
    try:
        link_info = socket_path.lstat()
        if _uid() >= 0 and link_info.st_uid != _uid():
            return False
        if stat.S_ISSOCK(link_info.st_mode):
            backing_path = socket_path
            backing_info = link_info
        elif stat.S_ISLNK(link_info.st_mode):
            backing_path = socket_path.resolve(strict=True)
            daemon_root = Path(tempfile.gettempdir()) / ("codex-daemon-%s" % _uid())
            if backing_path.parent != daemon_root or not re.fullmatch(r"[0-9a-f]{32,128}", backing_path.name):
                return False
            dir_info = daemon_root.lstat()
            if (not stat.S_ISDIR(dir_info.st_mode) or stat.S_ISLNK(dir_info.st_mode)
                    or (_uid() >= 0 and dir_info.st_uid != _uid()) or dir_info.st_mode & 0o077):
                return False
            backing_info = backing_path.stat()
        else:
            return False
        return (stat.S_ISSOCK(backing_info.st_mode)
                and (_uid() < 0 or backing_info.st_uid == _uid())
                and not (backing_info.st_mode & 0o077)
                and socket_path.parent == root)
    except OSError:
        return False


def _safe_lease(path: Path, root: Path) -> Optional[Dict[str, Any]]:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (_uid() >= 0 and info.st_uid != _uid())
                or info.st_mode & 0o077 or info.st_size > 4096):
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = record.get("pid")
        start_time = record.get("start_time")
        sock = record.get("socket_path")
        if (record.get("version") != 1 or not isinstance(pid, int) or pid < 2
                or not isinstance(start_time, str) or not start_time
                or not isinstance(sock, str) or not _is_launcher_process(pid, start_time)):
            return None
        sock_path = Path(sock)
        if sock_path.parent != root or sock_path.name != path.stem + ".sock":
            return None
        if not _codex_socket_is_private(sock_path, root):
            return None
        return {"endpoint": "unix://" + str(sock_path), "socket_path": str(sock_path), "pid": pid}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _active_leases() -> List[Dict[str, Any]]:
    root = runtime_root(create=False)
    if root is None:
        return []
    result: List[Dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for path in entries:
        if not (path.name.startswith("codex-") and path.name.endswith(".json")):
            continue
        lease = _safe_lease(path, root)
        if lease is not None:
            result.append(lease)
    return result


def _lease_for_endpoint(endpoint: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(endpoint, str):
        return None
    for lease in _active_leases():
        if endpoint == lease["endpoint"]:
            return lease
    return None


def create_lease(pid: int, socket_path: Path) -> Tuple[Path, Path]:
    """Create an owner-only lease and return (root, lease_path)."""
    root = runtime_root(create=True)
    if root is None:
        raise RuntimeError("No secure OMarky runtime directory is available.")
    if socket_path.parent != root or not socket_path.name.startswith("codex-"):
        raise RuntimeError("Refusing a Codex socket outside the private runtime directory.")
    start_time = _proc_start_time(pid)
    if start_time is None:
        raise RuntimeError("Cannot verify the Codex TUI owner process on this system.")
    lease_path = root / (socket_path.stem + ".json")
    payload = json.dumps({"version": 1, "pid": pid, "start_time": start_time,
                          "socket_path": str(socket_path)}, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lease_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(lease_path)
        except OSError:
            pass
        raise
    return root, lease_path


def _safe_unlink(path: Optional[Path], parent: Path) -> None:
    if path is None:
        return
    try:
        if path.parent == parent:
            info = path.lstat()
            if info.st_uid == _uid() and (stat.S_ISSOCK(info.st_mode) or stat.S_ISREG(info.st_mode)):
                path.unlink()
    except OSError:
        pass


def _version_key(path: Path) -> Tuple[int, ...]:
    parts = []
    for component in path.parent.parent.name.replace("-", ".").split("."):
        digits = "".join(ch for ch in component if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def _is_mise_shim(path: Path) -> bool:
    return any(part in {"shims", "mise"} and path.parts[index - 1 : index + 1] == ("mise", "shims")
               for index, part in enumerate(path.parts) if index > 0)


def _usable_codex(path: Any) -> Optional[str]:
    try:
        candidate = Path(os.fspath(path)).expanduser().resolve(strict=True)
        if (candidate.name != "codex" or not candidate.is_file()
                or not os.access(candidate, os.X_OK) or _is_mise_shim(candidate)):
            return None
        return str(candidate)
    except (OSError, TypeError, ValueError):
        return None


def find_codex_binary() -> Optional[str]:
    """Find Codex without invoking PATH shims that may self-update."""
    configured = os.environ.get("OMARKY_CODEX_BIN")
    if configured:
        return _usable_codex(configured)
    on_path = shutil.which("codex")
    direct = _usable_codex(on_path) if on_path else None
    if direct:
        return direct
    installs = Path.home() / ".local" / "share" / "mise" / "installs" / "codex"
    try:
        candidates = [_usable_codex(path) for path in installs.glob("*/bin/codex")]
    except OSError:
        candidates = []
    usable = [(path, _version_key(Path(path))) for path in candidates if path]
    if usable:
        usable.sort(key=lambda item: item[1], reverse=True)
        return usable[0][0]
    return None


def _websocket_frame(payload: bytes, *, opcode: int = 1) -> bytes:
    if len(payload) > _MAX_WS_MESSAGE:
        raise _ProtocolFailure("Codex message is too large.")
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes((0x80 | opcode, 0x80 | size))
    elif size <= 0xFFFF:
        header = bytes((0x80 | opcode, 0x80 | 126)) + size.to_bytes(2, "big")
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + size.to_bytes(8, "big")
    masked = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
    return header + mask + masked


class _AppServerClient:
    """Small stdlib WebSocket/JSON-RPC client over Codex's documented proxy."""

    def __init__(self, codex_binary: str, socket_path: str):
        self.codex_binary = codex_binary
        self.socket_path = socket_path
        self.process: Optional[subprocess.Popen] = None
        self.buffer = bytearray()
        self.next_id = 0
        self.pending: Deque[Dict[str, Any]] = deque()
        self.initialized = False

    def __enter__(self) -> "_AppServerClient":
        try:
            self.process = subprocess.Popen(
                [self.codex_binary, "app-server", "proxy", "--sock", self.socket_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, close_fds=True,
            )
        except OSError as exc:
            raise _RouteUnavailable("Could not start the Codex app-server proxy.") from exc
        try:
            self._websocket_handshake()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdin, process.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def _read_more(self, deadline: float) -> None:
        process = self.process
        if process is None or process.stdout is None:
            raise _RouteUnavailable("Codex app-server proxy is closed.")
        if process.poll() is not None:
            raise _RouteUnavailable("Codex app-server proxy exited.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for Codex app-server.")
        try:
            ready, _, _ = select.select([process.stdout], [], [], remaining)
        except (OSError, ValueError) as exc:
            raise _RouteUnavailable("Codex app-server proxy is unavailable.") from exc
        if not ready:
            raise TimeoutError("Timed out waiting for Codex app-server.")
        try:
            chunk = os.read(process.stdout.fileno(), 65536)
        except OSError as exc:
            raise _RouteUnavailable("Codex app-server proxy is unavailable.") from exc
        if not chunk:
            raise _RouteUnavailable("Codex app-server closed the local connection.")
        self.buffer.extend(chunk)

    def _take(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            self._read_more(deadline)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _read_until(self, delimiter: bytes, deadline: float, max_size: int) -> bytes:
        while True:
            index = self.buffer.find(delimiter)
            if index >= 0:
                end = index + len(delimiter)
                result = bytes(self.buffer[:end])
                del self.buffer[:end]
                return result
            if len(self.buffer) > max_size:
                raise _ProtocolFailure("Codex WebSocket handshake headers exceeded the limit.")
            self._read_more(deadline)

    def _write_bytes(self, payload: bytes) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise _RouteUnavailable("Codex app-server proxy is closed.")
        view = memoryview(payload)
        while view:
            try:
                written = os.write(process.stdin.fileno(), view)
            except (BrokenPipeError, OSError) as exc:
                raise _RouteUnavailable("Codex app-server proxy closed while sending.") from exc
            if written <= 0:
                raise _RouteUnavailable("Codex app-server proxy closed while sending.")
            view = view[written:]

    def _send_ws(self, payload: bytes, *, opcode: int = 1) -> None:
        self._write_bytes(_websocket_frame(payload, opcode=opcode))

    def _websocket_handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % key
        ).encode("ascii")
        self._write_bytes(request)
        response = self._read_until(b"\r\n\r\n", time.monotonic() + _RPC_TIMEOUT, _MAX_HTTP_HEADER)
        try:
            lines = response.decode("latin1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise _ProtocolFailure("Codex returned invalid WebSocket headers.") from exc
        if not lines or " 101 " not in lines[0]:
            raise _RouteUnavailable("Codex app-server rejected the local WebSocket connection.")
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(key.encode("ascii") + _WEBSOCKET_GUID).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected or headers.get("upgrade", "").lower() != "websocket":
            raise _ProtocolFailure("Codex WebSocket handshake did not match the RFC 6455 response.")

    def _read_ws_message(self, deadline: float) -> bytes:
        parts: List[bytes] = []
        active_opcode: Optional[int] = None
        total = 0
        while True:
            first, second = self._take(2, deadline)
            fin = bool(first & 0x80)
            if first & 0x70:
                raise _ProtocolFailure("Codex used an unsupported WebSocket extension.")
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            size = second & 0x7F
            if size == 126:
                size = int.from_bytes(self._take(2, deadline), "big")
            elif size == 127:
                size = int.from_bytes(self._take(8, deadline), "big")
            if size > _MAX_WS_MESSAGE or total + size > _MAX_WS_MESSAGE:
                raise _ProtocolFailure("Codex WebSocket message exceeded the size limit.")
            mask = self._take(4, deadline) if masked else None
            data = self._take(size, deadline)
            if mask is not None:
                data = bytes(value ^ mask[index & 3] for index, value in enumerate(data))
            if opcode >= 8 and (not fin or size > 125):
                raise _ProtocolFailure("Codex sent an invalid WebSocket control frame.")
            if opcode == 8:
                raise _RouteUnavailable("Codex app-server closed the WebSocket.")
            if opcode == 9:
                self._send_ws(data, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                if active_opcode is not None:
                    raise _ProtocolFailure("Codex interleaved fragmented WebSocket messages.")
                active_opcode = opcode
                parts = [data]
                total = len(data)
                if fin:
                    return b"".join(parts)
                continue
            if opcode == 0 and active_opcode is not None:
                parts.append(data)
                total += len(data)
                if fin:
                    return b"".join(parts)
                continue
            raise _ProtocolFailure("Codex sent an unsupported WebSocket frame.")

    def _receive_json(self, deadline: float) -> Dict[str, Any]:
        while True:
            raw = self._read_ws_message(deadline)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _ProtocolFailure("Codex returned invalid JSON-RPC data.") from exc
            if not isinstance(message, dict):
                raise _ProtocolFailure("Codex returned a non-object JSON-RPC message.")
            return message

    def _send_rpc(self, request: Mapping[str, Any]) -> None:
        try:
            payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise _ProtocolFailure("OMarky could not encode the Codex request.") from exc
        self._send_ws(payload)

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send_rpc(message)

    def request(self, method: str, params: Optional[Mapping[str, Any]] = None,
                *, timeout: float = _RPC_TIMEOUT) -> Dict[str, Any]:
        if not self.initialized and method != "initialize":
            raise _ProtocolFailure("Codex request attempted before initialization.")
        self.next_id += 1
        request_id = self.next_id
        message: Dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        self._send_rpc(message)
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive_json(deadline)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    error = message.get("error")
                    if not isinstance(error, dict):
                        raise _ProtocolFailure("Codex returned an invalid JSON-RPC error.")
                    raise _RPCFailure(error.get("code"), str(error.get("message", "Codex request failed.")))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise _ProtocolFailure("Codex returned an invalid JSON-RPC result.")
                return result
            # Never approve an app-server initiated permission or user-input
            # request. Reject it explicitly; OMarky is a targeted image sender,
            # not an interactive Codex approval client.
            if "id" in message and "method" in message:
                self._send_rpc({"id": message["id"], "error": {
                    "code": -32601, "message": "OMarky does not handle Codex server requests."}})
                continue
            if "method" in message:
                self.pending.append(message)
                continue
            raise _ProtocolFailure("Codex returned an uncorrelated JSON-RPC response.")

    def initialize(self) -> None:
        result = self.request("initialize", {"clientInfo": _CLIENT_INFO})
        if not isinstance(result, dict):
            raise _ProtocolFailure("Codex initialization response was invalid.")
        self.notify("initialized", {})
        self.initialized = True


def _status_type(thread: Mapping[str, Any]) -> Optional[str]:
    status = thread.get("status")
    if isinstance(status, dict) and isinstance(status.get("type"), str):
        return status["type"]
    return None


def _clean_label_part(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join("".join(ch if ch >= " " and ch != "\x7f" else " " for ch in value).split())
    return cleaned[:88] or fallback


def _thread_label(thread: Mapping[str, Any]) -> str:
    cwd = thread.get("cwd")
    project = _clean_label_part(Path(cwd).name if isinstance(cwd, str) and cwd else "", "Codex")
    title = _clean_label_part(thread.get("name") or thread.get("preview"), "conversation")
    label = "%s — %s" % (project, title)
    return label[:120]


def _targets_from_session(client: _AppServerClient, endpoint: str) -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = []
    cursor: Optional[str] = None
    seen_cursors = set()
    for _ in range(20):
        params: Dict[str, Any] = {"limit": 100, "archived": False, "sourceKinds": ["cli", "vscode"]}
        if cursor:
            params["cursor"] = cursor
        response = client.request("thread/list", params)
        records = response.get("data")
        if not isinstance(records, list):
            raise _ProtocolFailure("Codex thread/list response was invalid.")
        for thread in records:
            if not isinstance(thread, dict) or _status_type(thread) not in {"idle", "active"}:
                continue
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            targets.append({
                "connector": _CONNECTOR,
                "endpoint": endpoint,
                "thread_id": thread_id,
                "label": _thread_label(thread),
            })
        next_cursor = response.get("nextCursor")
        if not next_cursor:
            break
        if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
            raise _ProtocolFailure("Codex thread/list returned a repeated pagination cursor.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return targets


def discover() -> List[Dict[str, str]]:
    """Return only currently loaded, live Codex threads from our paired TUI."""
    binary = find_codex_binary()
    if not binary:
        return []
    targets: List[Dict[str, str]] = []
    for lease in _active_leases():
        try:
            with _AppServerClient(binary, lease["socket_path"]) as client:
                client.initialize()
                targets.extend(_targets_from_session(client, lease["endpoint"]))
        except (_RouteUnavailable, _ProtocolFailure, _RPCFailure, TimeoutError, OSError, ValueError):
            # An unreachable or malformed owner session is not a live target.
            continue
    unique: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for target in targets:
        key = (_CONNECTOR, target["endpoint"], target["thread_id"])
        unique[key] = target
    return list(unique.values())


def _target_status(client: _AppServerClient, thread_id: str) -> str:
    response = client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = response.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise _RouteUnavailable("The selected Codex conversation is gone.")
    return _status_type(thread) or "unknown"


def _rpc_status(error: _RPCFailure, *, after_turn_start: bool = False) -> Tuple[str, str]:
    message = error.message
    lowered = message.lower()
    if any(token in lowered for token in ("already in progress", "already running", "thread is busy", "turn is active", "turn is in progress")):
        return "busy", "Codex is already working in this conversation. Wait for it to finish, then send again."
    if any(token in lowered for token in ("thread not found", "unknown thread", "thread does not exist", "no such thread", "not loaded")):
        return "gone", "That conversation is no longer open in the paired Codex TUI. Reopen it there and refresh targets."
    if after_turn_start:
        return "uncertain", "Codex returned an unexpected turn error. Check the paired conversation before retrying."
    return "error", "Codex rejected the request. Refresh the target and try again if it is still open."


def _agent_text(items: Any) -> Optional[str]:
    if not isinstance(items, list):
        return None
    messages = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            messages.append(text)
    return messages[-1] if messages else None


def _wait_for_final_turn(client: _AppServerClient, thread_id: str, turn_id: str,
                         timeout: float) -> Tuple[str, Optional[str]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            page = client.request("thread/turns/list", {
                "threadId": thread_id,
                "limit": 50,
                "sortDirection": "desc",
                "itemsView": "full",
            }, timeout=min(_RPC_TIMEOUT, max(0.5, deadline - time.monotonic())))
        except _RPCFailure as exc:
            status, _message = _rpc_status(exc)
            return ("gone" if status == "gone" else "uncertain"), None
        except (TimeoutError, _RouteUnavailable, _ProtocolFailure, OSError):
            return "uncertain", None
        turns = page.get("data")
        if not isinstance(turns, list):
            return "uncertain", None
        selected = next((item for item in turns if isinstance(item, dict) and item.get("id") == turn_id), None)
        if selected is not None:
            status = selected.get("status")
            if status == "completed":
                return "completed", _agent_text(selected.get("items"))
            if status == "failed":
                return "failed", None
            if status == "interrupted":
                return "interrupted", None
        time.sleep(min(_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
    return "timeout", None


def send(target: Mapping[str, Any], png: bytes, note: str, *, request_id: str,
         wait_reply: bool) -> Dict[str, str]:
    """Send marked PNG + note to the explicit live thread; never auto-retry."""
    if not isinstance(target, Mapping) or target.get("connector") != _CONNECTOR:
        return {"status": "error", "message": "The selected target is not a Codex conversation."}
    thread_id = target.get("thread_id")
    endpoint = target.get("endpoint")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(endpoint, str):
        return {"status": "error", "message": "The selected Codex target is incomplete."}
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > _MAX_IMAGE_BYTES:
        return {"status": "error", "message": "OMarky can send only a valid PNG up to 32 MiB."}
    if not isinstance(note, str):
        return {"status": "error", "message": "The note must be text."}
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        return {"status": "error", "message": "The turn request ID is invalid."}
    if not isinstance(wait_reply, bool):
        return {"status": "error", "message": "The reply-wait setting is invalid."}
    lease = _lease_for_endpoint(endpoint)
    if lease is None:
        return {"status": "gone", "message": "The paired Codex TUI is closed or no longer owns this target. Reopen it and refresh."}
    binary = find_codex_binary()
    if not binary:
        return {"status": "gone", "message": "Codex CLI is unavailable. Reopen the paired Codex TUI after installing Codex."}
    try:
        client_context = _AppServerClient(binary, lease["socket_path"])
        with client_context as client:
            client.initialize()
            try:
                status = _target_status(client, thread_id)
            except _RPCFailure as exc:
                return {"status": _rpc_status(exc)[0], "message": _rpc_status(exc)[1]}
            if status == "active":
                return {"status": "busy", "message": "Codex is already working in this conversation. Wait for it to finish, then send again."}
            if status == "notLoaded":
                return {"status": "gone", "message": "That conversation is no longer open in the paired Codex TUI. Reopen it there and refresh targets."}
            if status == "systemError":
                return {"status": "uncertain", "message": "Codex reports an error for this conversation. Check it in the paired TUI."}
            if status != "idle":
                return {"status": "uncertain", "message": "Codex could not confirm that the selected conversation is ready."}
            image_data = base64.b64encode(png).decode("ascii")
            params = {
                "threadId": thread_id,
                "clientUserMessageId": request_id,
                "input": [
                    {"type": "text", "text": note},
                    {"type": "image", "url": "data:image/png;base64," + image_data},
                ],
            }
            try:
                started = client.request("turn/start", params, timeout=_RPC_TIMEOUT)
            except _RPCFailure as exc:
                state, message = _rpc_status(exc, after_turn_start=True)
                return {"status": state, "message": message}
            except (TimeoutError, _RouteUnavailable, _ProtocolFailure, OSError):
                return {"status": "uncertain", "message": "Codex may have accepted the turn, but OMarky did not receive confirmation. Check the paired conversation before retrying."}
            turn = started.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                return {"status": "uncertain", "message": "Codex accepted a turn without a usable turn ID. Check the paired conversation before retrying."}
            label = _clean_label_part(target.get("label"), "Codex conversation")
            if not wait_reply:
                return {"status": "ok", "message": "Sent to %s; Codex accepted the turn." % label}
            state, answer = _wait_for_final_turn(client, thread_id, turn_id, _WAIT_REPLY_TIMEOUT)
            if state == "completed" and answer is not None:
                return {"status": "ok", "message": "Codex replied in %s." % label, "answer": answer}
            if state == "failed":
                return {"status": "error", "message": "Codex could not complete the selected turn. Check the paired conversation."}
            if state == "interrupted":
                return {"status": "uncertain", "message": "The Codex turn was interrupted before a final reply. Check the paired conversation before retrying."}
            if state == "completed":
                return {"status": "uncertain", "message": "Codex completed the turn, but OMarky could not read its final assistant reply. Check the paired conversation."}
            if state == "gone":
                return {"status": "uncertain", "message": "The paired Codex TUI closed before OMarky could confirm the final reply. Check the conversation before retrying."}
            return {"status": "uncertain", "message": "Codex accepted the turn, but OMarky could not confirm its final reply. Check the paired conversation before retrying."}
    except _RPCFailure as exc:
        status, message = _rpc_status(exc)
        return {"status": status, "message": message}
    except (TimeoutError, _RouteUnavailable, _ProtocolFailure, OSError, subprocess.SubprocessError):
        return {"status": "gone", "message": "The paired Codex app-server is unavailable. Reopen the Codex TUI and refresh targets."}


def guidance() -> List[str]:
    binary = find_codex_binary()
    if not binary:
        return ["Codex CLI was not found. Install the official Codex CLI or set OMARKY_CODEX_BIN to its executable; OMarky does not run connector-discovery scripts."]
    if not _active_leases():
        launcher = Path(__file__).resolve().parent.parent / "bin" / "omarky-codex"
        return ["To pair explicitly, run %s in a terminal. It opens Codex's existing-conversation picker on a private local socket; keep that TUI open while selecting or sending." % launcher]
    if not discover():
        return ["The paired Codex TUI is open, but no loaded conversation is available. Choose or resume an existing chat in that TUI, then refresh OMarky; OMarky never resumes a detached thread itself."]
    return []
