"""Windows-side newline-delimited JSON client for the Raspberry Pi serial bridge."""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid


class SerialBridgeClient:
    def __init__(self, port: str = "auto", baudrate: int = 9600):
        self.port = str(port or "auto").strip()
        self.baudrate = int(baudrate)
        self._stop = threading.Event()
        self._commands: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._serial = None
        self._thread: threading.Thread | None = None
        self._connected_port = ""
        self._last_error = ""
        self._last_seen = 0.0
        self._pending: dict[str, dict] = {}
        self._candidate_offset = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker, name="smart-home-serial", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            serial_port = self._serial
            self._serial = None
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass
        self._fail_pending("Bluetooth serial connection stopped.")

    def status(self) -> dict:
        with self._lock:
            return {
                "connected": self._serial is not None,
                "port": self._connected_port,
                "baudrate": self.baudrate,
                "last_error": self._last_error,
                "last_seen": self._last_seen,
                "running": bool(self._thread and self._thread.is_alive()),
            }

    def request(self, action: str, timeout: float = 10.0, **params) -> dict:
        self.start()
        request_id = uuid.uuid4().hex
        pending = {"event": threading.Event(), "response": None}
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("Bluetooth serial client is stopped.")
            self._pending[request_id] = pending
        payload = {"type": "command", "id": request_id, "action": action}
        payload.update(params)
        self._commands.put((request_id, payload))
        if not pending["event"].wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
                connection, self._serial = self._serial, None
                self._connected_port = ""
                self._last_error = "Raspberry Pi response timed out; reconnecting."
                if self.port.lower() == "auto":
                    self._candidate_offset += 1
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise TimeoutError("Raspberry Pi did not respond over Bluetooth.")
        response = pending["response"]
        if not isinstance(response, dict):
            raise RuntimeError("Raspberry Pi returned an invalid response.")
        if not response.get("success"):
            message = str(response.get("message") or "Smart-home command failed.")
            raise RuntimeError(message)
        return response

    def _ports(self) -> list[str]:
        if self.port.lower() != "auto":
            return [self.port]
        candidates = ["COM8", "COM9"]
        try:
            from serial.tools import list_ports
            for item in list_ports.comports():
                if item.device not in candidates:
                    candidates.append(item.device)
        except Exception as exc:
            with self._lock:
                self._last_error = f"Could not enumerate COM ports: {exc}"
        if candidates:
            offset = self._candidate_offset % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        return candidates

    def _connect(self) -> bool:
        try:
            import serial
        except ImportError:
            with self._lock:
                self._last_error = "pyserial is missing; install the project requirements."
            return False
        for port in self._ports():
            if self._stop.is_set():
                return False
            try:
                connection = serial.Serial(
                    port=port, baudrate=self.baudrate, timeout=0.15,
                    write_timeout=1.0, exclusive=True)
            except TypeError:
                try:
                    connection = serial.Serial(
                        port=port, baudrate=self.baudrate, timeout=0.15,
                        write_timeout=1.0)
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"{port}: {exc}"
                    continue
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{port}: {exc}"
                continue
            with self._lock:
                self._serial = connection
                self._connected_port = port
                self._last_error = ""
            return True
        return False

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                connected = self._serial is not None
            if not connected:
                if not self._connect():
                    self._stop.wait(2.0)
                    continue

            try:
                with self._lock:
                    connection = self._serial
                if connection is None:
                    continue
                line = connection.readline()
                if line:
                    self._handle_line(line)
                try:
                    request_id, payload = self._commands.get_nowait()
                except queue.Empty:
                    continue
                with self._lock:
                    if request_id not in self._pending:
                        continue
                encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                with self._lock:
                    connection = self._serial
                if connection is None:
                    raise OSError("Bluetooth serial connection was lost.")
                connection.write(encoded)
                connection.flush()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Bluetooth serial error: {exc}"
                    connection, self._serial = self._serial, None
                    self._connected_port = ""
                    if self.port.lower() == "auto":
                        self._candidate_offset += 1
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                self._fail_pending("Bluetooth serial connection was lost.")
                if not self._stop.is_set():
                    self._stop.wait(1.0)

    def _handle_line(self, line: bytes) -> None:
        if len(line) > 65536:
            with self._lock:
                self._last_error = "Ignored an oversized serial response."
            return
        try:
            response = json.loads(line.decode("utf-8").strip())
        except (UnicodeDecodeError, json.JSONDecodeError):
            with self._lock:
                self._last_error = "Ignored malformed JSON from the Raspberry Pi."
            return
        if not isinstance(response, dict) or response.get("type") != "response":
            return
        request_id = str(response.get("id") or "")
        with self._lock:
            pending = self._pending.pop(request_id, None)
            self._last_seen = time.time()
        if pending is not None:
            pending["response"] = response
            pending["event"].set()

    def _fail_pending(self, message: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item["response"] = {"success": False, "message": message}
            item["event"].set()
