"""A minimal blocking WebSocket client (RFC 6455), standard library only.

Blender's Python has no WebSocket package and extensions can't pip-install,
so this covers exactly what the Tencil daemon needs: text frames, ping/pong,
fragmentation and close. No TLS, no extensions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self._buffer = b""
        self.closed = False

    @classmethod
    def connect(cls, host: str, port: int, path: str = "/", timeout: float = 5.0) -> WebSocket:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            ws = cls(sock)
            head = ws._read_until(b"\r\n\r\n").decode("latin-1")
            status, *header_lines = head.split("\r\n")
            if " 101 " not in f"{status} ":
                raise WebSocketError(f"Upgrade refused: {status.strip()}")
            headers = {}
            for line in header_lines:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != expected:
                raise WebSocketError("Bad Sec-WebSocket-Accept")
            # Reads block until a message arrives; the caller closes the socket to stop.
            sock.settimeout(None)
            return ws
        except BaseException:
            sock.close()
            raise

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def recv(self) -> str | None:
        """The next text message, or None once the connection is closed."""
        parts: list[bytes] = []
        opcode_of_message: int | None = None
        while True:
            try:
                fin, opcode, payload = self._read_frame()
            except (OSError, WebSocketError):
                self.closed = True
                return None
            if opcode == OP_PING:
                self._safe_send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._safe_send(OP_CLOSE, payload[:2])
                self.close()
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                opcode_of_message = opcode
                parts = [payload]
            elif opcode == OP_CONTINUATION and opcode_of_message is not None:
                parts.append(payload)
            else:
                raise WebSocketError(f"Unexpected opcode {opcode}")
            if fin:
                data = b"".join(parts)
                if opcode_of_message == OP_TEXT:
                    return data.decode("utf-8")
                parts, opcode_of_message = [], None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def _safe_send(self, opcode: int, payload: bytes) -> None:
        try:
            self._send_frame(opcode, payload)
        except OSError:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # Clients must mask every frame.
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = _xor(payload, mask)
        with self._send_lock:
            if self.closed:
                raise OSError("WebSocket is closed")
            self._sock.sendall(bytes(header) + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        mask = self._read_exact(4) if second & 0x80 else None
        payload = self._read_exact(length)
        return fin, opcode, _xor(payload, mask) if mask else payload

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._fill()
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            if len(self._buffer) > 65536:
                raise WebSocketError("Handshake response too large")
            self._fill()
        data, _, self._buffer = self._buffer.partition(marker)
        return data

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise WebSocketError("Connection closed")
        self._buffer += chunk


def _xor(data: bytes, mask: bytes) -> bytes:
    # int.from_bytes keeps large payloads (screenshots) fast without numpy.
    if not data:
        return data
    repeated = (mask * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(data), "big")
