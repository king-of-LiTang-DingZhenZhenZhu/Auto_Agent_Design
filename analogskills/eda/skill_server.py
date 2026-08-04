"""Live Virtuoso SKILL server/client bridge.

This module keeps the live SKILL execution protocol self-contained in
``analogskills``. Virtuoso loads ``skill_server.il``, which starts this Python
module as a subprocess. Python clients then send JSON requests over ZMQ; this
server forwards SKILL expressions to Virtuoso over stdin/stdout and returns the
result.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Lock
from typing import Any, Callable, Mapping, TextIO

from .oa import OaWritePlan, write_oa_skill


@dataclass(frozen=True)
class SkillRequest:
    expr: str
    input_files: dict[str, Any] = field(default_factory=dict)
    out_file: str = ""
    request_type: str = "skill"

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": self.request_type,
            "expr": self.expr.replace("\n", " "),
            "input_files": self.input_files,
            "out_file": self.out_file,
        }


@dataclass(frozen=True)
class SkillResult:
    ok: bool
    data: Any = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class JsonZmqRouter:
    """Small JSON-over-ZMQ ROUTER wrapper used by the Virtuoso-side server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, min_port: int = 5000, max_port: int = 9999):
        self.host = host
        self.port = port
        self.min_port = min_port
        self.max_port = max_port
        self._socket: Any | None = None
        self._context: Any | None = None

    def open(self) -> int:
        try:
            import zmq
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency.
            raise ImportError("pyzmq is required for live Virtuoso SKILL IPC") from exc
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.ROUTER)
        if self.port:
            self._socket.bind(f"tcp://{self.host}:{self.port}")
        else:
            self.port = self._socket.bind_to_random_port(f"tcp://{self.host}", min_port=self.min_port, max_port=self.max_port)
        return self.port

    def recv_obj(self, timeout_ms: int | None = None) -> tuple[bytes, Any]:
        if self._socket is None:
            raise RuntimeError("ZMQ router socket is not open")
        import zmq
        old_timeout = self._socket.RCVTIMEO
        try:
            if timeout_ms is not None:
                self._socket.RCVTIMEO = timeout_ms
            try:
                parts = self._socket.recv_multipart()
            except zmq.error.Again as exc:
                raise TimeoutError("ZMQ receive timeout") from exc
            if len(parts) < 2:
                raise ValueError(f"expected at least 2 ZMQ frames, got {len(parts)}")
            return parts[0], json.loads(parts[-1].decode("utf-8"))
        finally:
            self._socket.RCVTIMEO = old_timeout

    def send_obj(self, identity: bytes, obj: Any) -> None:
        if self._socket is None:
            raise RuntimeError("ZMQ router socket is not open")
        self._socket.send_multipart([identity, json.dumps(obj).encode("utf-8")])

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._context is not None:
            self._context.term()
            self._context = None


class JsonZmqDealer:
    """Small JSON-over-ZMQ DEALER wrapper used by Python clients."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self._socket: Any | None = None
        self._context: Any | None = None

    def open(self) -> None:
        try:
            import zmq
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency.
            raise ImportError("pyzmq is required for live Virtuoso SKILL IPC") from exc
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.DEALER)
        self._socket.connect(f"tcp://{self.host}:{self.port}")

    def send_obj(self, obj: Any) -> None:
        if self._socket is None:
            raise RuntimeError("ZMQ dealer socket is not open")
        self._socket.send(json.dumps(obj).encode("utf-8"))

    def recv_obj(self, timeout_ms: int | None = None) -> Any:
        if self._socket is None:
            raise RuntimeError("ZMQ dealer socket is not open")
        import zmq
        old_timeout = self._socket.RCVTIMEO
        try:
            if timeout_ms is not None:
                self._socket.RCVTIMEO = timeout_ms
            try:
                data = self._socket.recv()
            except zmq.error.Again as exc:
                raise TimeoutError("ZMQ receive timeout") from exc
            return json.loads(data.decode("utf-8"))
        finally:
            self._socket.RCVTIMEO = old_timeout

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._context is not None:
            self._context.term()
            self._context = None


class VirtuosoSkillClient:
    """Client for a running ``analogskills``/BAG-compatible Virtuoso SKILL server."""

    def __init__(
        self,
        *,
        port_file: str | Path | None = None,
        host: str = "127.0.0.1",
        timeout_ms: int | None = None,
        auto_connect: bool = True,
        dealer_factory: Callable[[str, int], Any] | None = None,
    ):
        self.port_file = Path(port_file) if port_file is not None else None
        self.host = host
        self.timeout_ms = timeout_ms
        self.auto_connect = auto_connect
        self.dealer_factory = dealer_factory or (lambda host, port: JsonZmqDealer(host, port))
        self._dealer: Any | None = None
        self._connected = False
        self._connect_lock = Lock()

    def connect(self) -> None:
        with self._connect_lock:
            if self._connected:
                return
            port = self._read_port()
            self._dealer = self.dealer_factory(self.host, port)
            self._dealer.open()
            self._connected = True

    def ping(self) -> SkillResult:
        return self._request({"type": "ping"})

    def eval(self, expr: str, *, input_files: Mapping[str, Any] | None = None, out_file: str = "") -> Any:
        result = self.eval_result(expr, input_files=input_files, out_file=out_file)
        if not result.ok:
            raise RuntimeError(f"SKILL error: {result.error}")
        return result.data

    def eval_result(self, expr: str, *, input_files: Mapping[str, Any] | None = None, out_file: str = "") -> SkillResult:
        request = SkillRequest(expr, dict(input_files or {}), out_file)
        return self._request(request.to_wire())

    def disconnect(self) -> None:
        if self._dealer is not None:
            self._dealer.close()
        self._dealer = None
        self._connected = False

    def close(self) -> None:
        if self._dealer is not None:
            try:
                self._dealer.send_obj({"type": "exit"})
            finally:
                self._dealer.close()
        self._dealer = None
        self._connected = False

    def _request(self, wire_request: dict[str, Any]) -> SkillResult:
        self._ensure_connected()
        assert self._dealer is not None
        self._dealer.send_obj(wire_request)
        reply = self._dealer.recv_obj(self.timeout_ms)
        if reply.get("type") == "error":
            return SkillResult(False, error=str(reply.get("data", "unknown")), raw=reply)
        return SkillResult(True, data=reply.get("data"), raw=reply)

    def _ensure_connected(self) -> None:
        with self._connect_lock:
            connected = self._connected
        if not connected:
            if not self.auto_connect:
                raise RuntimeError("Skill client is not connected and auto_connect is disabled")
            self.connect()

    def _read_port(self) -> int:
        candidates: list[Path] = []
        if self.port_file is not None:
            candidates.append(self.port_file)
        candidates.extend(Path(name) for name in ("skill_server_port.txt", "BAG_server_port.txt"))
        for candidate in candidates:
            if candidate.exists():
                return int(candidate.read_text(encoding="utf-8").strip())
        raise RuntimeError("Cannot find Virtuoso SKILL server port file; load analogskills/eda/skill_server.il in Virtuoso first")

    def __enter__(self) -> "VirtuosoSkillClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False


class VirtuosoSkillServer:
    """Virtuoso-side ZMQ to stdin/stdout bridge.

    This process must be launched by Virtuoso SKILL ``ipcBeginProcess`` so its
    stdout is consumed by the SKILL handler and its stdin receives byte-counted
    evaluation results.
    """

    READY = "BAG skill server has started.  Yay!\n"

    def __init__(
        self,
        min_port: int = 5000,
        max_port: int = 9999,
        port_file: str | Path = "skill_server_port.txt",
        log_file: str | Path = "",
        *,
        router_factory: Callable[[], Any] | None = None,
    ):
        self.min_port = int(min_port)
        self.max_port = int(max_port)
        self.port_file = Path(port_file)
        self.log_file = Path(log_file) if log_file else None
        self.router_factory = router_factory or (lambda: JsonZmqRouter(port=0, min_port=self.min_port, max_port=self.max_port))
        self.router: Any | None = None
        self.virt_in: TextIO | None = None
        self.virt_out: TextIO | None = None
        self._tmp_dir: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._pending: dict[bytes, Future[dict[str, Any]]] = {}

    def start(self) -> int:
        self.router = self.router_factory()
        port = self.router.open()
        self.port_file.write_text(str(port), encoding="utf-8")
        self._tmp_dir = tempfile.mkdtemp(prefix="skillTmp_")
        self.virt_in = sys.stdout
        self.virt_out = sys.stdin
        self.virt_in.write(self.READY)
        self.virt_in.flush()
        return port

    def run(self) -> None:
        if self.router is None:
            self.start()
        assert self.router is not None
        while True:
            self._drain_completed()
            try:
                identity, request = self.router.recv_obj(timeout_ms=50)
            except TimeoutError:
                continue
            req_type = request.get("type", "")
            if req_type == "ping":
                self.router.send_obj(identity, {"type": "str", "data": "pong"})
            elif req_type in {"exit", "shutdown"}:
                self.router.send_obj(identity, {"type": "str", "data": "ok"})
                break
            elif req_type == "skill":
                self._pending[identity] = self._executor.submit(self._handle_skill, request)
            else:
                self.router.send_obj(identity, {"type": "error", "data": f"Unknown request type: {req_type}"})

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        if self.router is not None:
            self.router.close()
            self.router = None
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
        try:
            self.port_file.unlink()
        except FileNotFoundError:
            pass

    def _drain_completed(self) -> None:
        if self.router is None:
            return
        completed: list[bytes] = []
        for identity, future in list(self._pending.items()):
            if not future.done():
                continue
            completed.append(identity)
            try:
                reply = future.result()
            except Exception as exc:  # noqa: BLE001 - server boundary returns structured error.
                reply = {"type": "error", "data": f"SKILL thread error: {exc}"}
            self.router.send_obj(identity, reply)
        for identity in completed:
            del self._pending[identity]

    def _handle_skill(self, request: Mapping[str, Any]) -> dict[str, Any]:
        expr = str(request.get("expr", ""))
        input_files = dict(request.get("input_files") or {})
        out_file = str(request.get("out_file", ""))
        tmp_dir = self._tmp_dir or tempfile.gettempdir()

        for name, data in input_files.items():
            path = os.path.join(tmp_dir, str(name))
            if isinstance(data, (dict, list, tuple, bool, int, float)):
                object_to_skill_file(data, path)
            else:
                Path(path).write_text(str(data), encoding="utf-8")
            expr = expr.replace(f"__FILE:{name}__", f'"{path}"')

        out_path = ""
        if out_file:
            out_path = os.path.join(tmp_dir, out_file)
            expr = expr.replace(f"__FILE:{out_file}__", f'"{out_path}"')

        if self.virt_in is None or self.virt_out is None:
            return {"type": "error", "data": "Virtuoso stdio is not initialized"}
        self.virt_in.write(expr.replace("\n", " ") + "\n")
        self.virt_in.flush()

        try:
            line = self.virt_out.readline()
            if not line:
                return {"type": "error", "data": "Virtuoso closed server stdin"}
            byte_count = int(line.strip())
            data = self.virt_out.read(byte_count)
            if data.endswith("\n"):
                data = data[:-1]
        except (OSError, ValueError) as exc:
            return {"type": "error", "data": f"IO error reading SKILL result: {exc}"}

        if data.startswith("*Error*"):
            return {"type": "error", "data": data}
        if out_file and out_path:
            try:
                data = Path(out_path).read_text(encoding="utf-8")
            except OSError as exc:
                return {"type": "error", "data": f"Error reading SKILL output file: {exc}"}
        return {"type": "str", "data": data}


def object_to_skill_file(py_obj: Any, file_path: str | Path) -> Path:
    path = Path(file_path)
    with path.open("w", encoding="utf-8") as file_obj:
        _object_to_skill_file_helper(py_obj, file_obj)
        file_obj.write("\n")
    return path


def run_skill_file(client: VirtuosoSkillClient, path: str | Path) -> Any:
    return client.eval(f'load("{_skill_quote_path(path)}")')


def run_oa_plan_via_skill_server(
    client: VirtuosoSkillClient,
    plan: OaWritePlan,
    *,
    work_dir: str | Path | None = None,
    filename: str = "oa_write_plan.il",
) -> Any:
    directory = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="analogskills_oa_"))
    directory.mkdir(parents=True, exist_ok=True)
    script = write_oa_skill(plan, directory / filename)
    return run_skill_file(client, script)


def _object_to_skill_file_helper(py_obj: Any, file_obj: TextIO) -> None:
    if isinstance(py_obj, str):
        file_obj.write(py_obj)
    elif isinstance(py_obj, bool):
        file_obj.write("t" if py_obj else "nil")
    elif isinstance(py_obj, int):
        file_obj.write(f"#int {py_obj}")
    elif isinstance(py_obj, float):
        file_obj.write(f"#float {py_obj:f}")
    elif isinstance(py_obj, (list, tuple)):
        file_obj.write("#list\n")
        for val in py_obj:
            _object_to_skill_file_helper(val, file_obj)
            file_obj.write("\n")
        file_obj.write("#end")
    elif isinstance(py_obj, dict):
        file_obj.write("#prop_list\n")
        for key, val in py_obj.items():
            file_obj.write(f"{key}\n")
            _object_to_skill_file_helper(val, file_obj)
            file_obj.write("\n")
        file_obj.write("#end")
    else:
        file_obj.write(str(py_obj))


def _skill_quote_path(path: str | Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="analogskills live Virtuoso SKILL server")
    parser.add_argument("min_port", type=int, nargs="?", default=5000)
    parser.add_argument("max_port", type=int, nargs="?", default=9999)
    parser.add_argument("port_file", type=str, nargs="?", default="skill_server_port.txt")
    parser.add_argument("log_file", type=str, nargs="?", default="")
    args = parser.parse_args()

    server = VirtuosoSkillServer(args.min_port, args.max_port, args.port_file, args.log_file)
    try:
        server.start()
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    main()
