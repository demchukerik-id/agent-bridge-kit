#!/usr/bin/env python3
"""
Minimal Chrome DevTools Protocol client — stdlib only.

Just enough to:
  - List Chrome's debugger targets via HTTP /json/list
  - Open a WebSocket to a target's webSocketDebuggerUrl
  - Send JSON-RPC commands and await responses
  - Receive Target/Runtime events

Designed for cdp-daemon.py; not a general-purpose CDP library.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Callable, Optional


# ─── HTTP /json/list ─────────────────────────────────────────────────────

def list_targets(host: str = "127.0.0.1", port: int = 9222) -> list[dict]:
    url = f"http://{host}:{port}/json/list"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def get_version(host: str = "127.0.0.1", port: int = 9222) -> dict:
    url = f"http://{host}:{port}/json/version"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


# ─── WebSocket client (RFC 6455, text frames only) ───────────────────────

_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_handshake(sock: socket.socket, path: str, host_header: str) -> None:
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode("ascii"))
    expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    # Read until \r\n\r\n
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WS handshake closed early")
        buf += chunk
        if len(buf) > 16384:
            raise ConnectionError("WS handshake header too large")
    headers, _, leftover = buf.partition(b"\r\n\r\n")
    head_lines = headers.decode("iso-8859-1").split("\r\n")
    status = head_lines[0]
    if " 101 " not in status:
        raise ConnectionError(f"WS handshake failed: {status}")
    accept_ok = False
    for line in head_lines[1:]:
        if line.lower().startswith("sec-websocket-accept:"):
            if line.split(":", 1)[1].strip() == expected:
                accept_ok = True
    if not accept_ok:
        raise ConnectionError("WS handshake: bad Sec-WebSocket-Accept")
    if leftover:
        # Stash any payload bytes that arrived with the handshake response.
        sock.setblocking(True)
        sock._ws_leftover = leftover  # type: ignore[attr-defined]


def _recv_exact(sock: socket.socket, n: int, prebuf: bytes = b"") -> tuple[bytes, bytes]:
    """Read exactly n bytes from sock, possibly using leftover prebuf."""
    out = bytearray()
    if prebuf:
        take = prebuf[:n]
        out += take
        prebuf = prebuf[len(take):]
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionError("WS socket closed during read")
        out += chunk
    return bytes(out), prebuf


def _read_frame(sock: socket.socket, leftover_buf: bytearray) -> tuple[int, bytes]:
    """Read one full WS frame; assemble fragmented frames; return (opcode, payload)."""
    full_payload = bytearray()
    final_opcode = 0
    while True:
        hdr, leftover = _recv_exact(sock, 2, bytes(leftover_buf))
        leftover_buf.clear(); leftover_buf.extend(leftover)
        b1, b2 = hdr[0], hdr[1]
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        plen = b2 & 0x7F
        if plen == 126:
            ext, leftover = _recv_exact(sock, 2, bytes(leftover_buf))
            leftover_buf.clear(); leftover_buf.extend(leftover)
            plen = struct.unpack(">H", ext)[0]
        elif plen == 127:
            ext, leftover = _recv_exact(sock, 8, bytes(leftover_buf))
            leftover_buf.clear(); leftover_buf.extend(leftover)
            plen = struct.unpack(">Q", ext)[0]
        if masked:  # server should not mask
            mask, leftover = _recv_exact(sock, 4, bytes(leftover_buf))
            leftover_buf.clear(); leftover_buf.extend(leftover)
        else:
            mask = b""
        payload, leftover = _recv_exact(sock, plen, bytes(leftover_buf))
        leftover_buf.clear(); leftover_buf.extend(leftover)
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        if final_opcode == 0:
            final_opcode = opcode
        full_payload += payload
        if fin:
            return final_opcode, bytes(full_payload)


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    b1 = 0x80 | (opcode & 0x0F)
    mask_bit = 0x80
    plen = len(payload)
    if plen < 126:
        header = struct.pack("!BB", b1, mask_bit | plen)
    elif plen <= 0xFFFF:
        header = struct.pack("!BBH", b1, mask_bit | 126, plen)
    else:
        header = struct.pack("!BBQ", b1, mask_bit | 127, plen)
    mask = secrets.token_bytes(4)
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


# ─── CDP client ──────────────────────────────────────────────────────────

class CDPClient:
    """One WebSocket per target. Synchronous send_command(); background reader thread."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        u = urllib.parse.urlparse(ws_url)
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 9222
        self.path = u.path + (f"?{u.query}" if u.query else "")
        self.sock: Optional[socket.socket] = None
        self._leftover = bytearray()
        self._next_id = 1
        self._lock = threading.Lock()
        self._inbox_lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._response_event = threading.Event()
        self._events: deque = deque()
        self._closed = False
        self._reader_error: Optional[str] = None
        self._reader_thread: Optional[threading.Thread] = None
        self.event_handler: Optional[Callable[[dict], None]] = None

    def connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=5)
        s.settimeout(None)
        # Enable TCP keepalive so dead connections are detected by OS quickly.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in [
                (getattr(socket, "TCP_KEEPIDLE", None), 30),
                (getattr(socket, "TCP_KEEPINTVL", None), 10),
                (getattr(socket, "TCP_KEEPCNT", None), 3),
            ]:
                if opt is not None:
                    s.setsockopt(socket.IPPROTO_TCP, opt, val)
        except OSError:
            pass
        _ws_handshake(s, self.path, f"{self.host}:{self.port}")
        leftover = getattr(s, "_ws_leftover", b"")
        self._leftover = bytearray(leftover)
        self.sock = s
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        try:
            while not self._closed and self.sock:
                opcode, payload = _read_frame(self.sock, self._leftover)
                if opcode == _OP_CLOSE:
                    self._reader_error = "received CLOSE frame"
                    break
                if opcode == _OP_PING:
                    _send_frame(self.sock, _OP_PONG, payload)
                    continue
                if opcode == _OP_PONG:
                    continue
                if opcode != _OP_TEXT:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception as e:
                    self._reader_error = f"json decode: {e}"
                    continue
                if "id" in msg:
                    with self._inbox_lock:
                        self._responses[msg["id"]] = msg
                    self._response_event.set()
                else:
                    if self.event_handler:
                        try: self.event_handler(msg)
                        except Exception: pass
                    self._events.append(msg)
        except Exception as e:
            # Surface so send_command can raise instead of hanging.
            self._reader_error = f"{type(e).__name__}: {e}"
        finally:
            self._closed = True
            self._response_event.set()  # wake any waiter so it sees _closed=True
            try:
                if self.sock: self.sock.close()
            except Exception:
                pass

    def send_command(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        if self._closed or not self.sock:
            raise ConnectionError("CDP client not connected")
        with self._lock:
            mid = self._next_id
            self._next_id += 1
        msg = {"id": mid, "method": method, "params": params or {}}
        _send_frame(self.sock, _OP_TEXT, json.dumps(msg).encode("utf-8"))
        deadline = (None if timeout is None else (os.times().elapsed if False else None))
        # Wait for response
        import time as _t
        start = _t.monotonic()
        while True:
            with self._inbox_lock:
                if mid in self._responses:
                    return self._responses.pop(mid)
            if self._closed:
                raise ConnectionError(
                    f"CDP connection closed while awaiting {method}"
                    + (f" (reader: {self._reader_error})" if self._reader_error else "")
                )
            if timeout is not None and (_t.monotonic() - start) > timeout:
                raise TimeoutError(f"CDP {method} timed out after {timeout}s")
            self._response_event.wait(timeout=0.1)
            self._response_event.clear()

    def close(self) -> None:
        self._closed = True
        try:
            if self.sock:
                _send_frame(self.sock, _OP_CLOSE, b"")
                self.sock.close()
        except Exception:
            pass
