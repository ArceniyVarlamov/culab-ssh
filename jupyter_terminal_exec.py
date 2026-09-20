#!/usr/bin/env python3
"""Execute one shell command through a Jupyter Server terminal WebSocket.

Authentication uses a JupyterHub API token from ``~/.culab-env``. The token
is never printed.

Usage:
  HUB_URL=https://jupyter.culab.ru python3 jupyter_terminal_exec.py 'hostname; pwd; id'
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

ENV_FILE = Path(os.environ.get("CULAB_ENV", Path.home() / ".culab-env"))


class HubAuthError(RuntimeError):
    pass


def load_hub_token(path: Path = ENV_FILE) -> str:
    """Read JUPYTERHUB_TOKEN without sourcing a shell file."""
    if os.environ.get("JUPYTERHUB_TOKEN"):
        return os.environ["JUPYTERHUB_TOKEN"]
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:]
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "JUPYTERHUB_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
    raise HubAuthError(f"JUPYTERHUB_TOKEN not found in environment or {path}")


def discover_base_path(hub_url: str, token: str) -> str:
    req = Request(
        f"{hub_url.rstrip('/')}/hub/api/user",
        headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    try:
        with build_opener().open(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        exc.read()
        if exc.code == 403:
            raise HubAuthError("JupyterHub token rejected: HTTP 403") from exc
        raise RuntimeError(f"JupyterHub token rejected: HTTP {exc.code}") from exc
    user = str(payload.get("name") or "")
    if not user:
        raise RuntimeError("JupyterHub token response has no user name")
    return f"/user/{quote(user, safe='@')}"


def create_terminal(hub_url: str, base_path: str, token: str) -> str:
    opener = build_opener()
    api_path = f"{base_path}/api/terminals"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
        "Referer": f"{hub_url.rstrip('/')}{base_path}/tree",
    }
    headers["Authorization"] = f"token {token}"
    req = Request(
        f"{hub_url.rstrip('/')}{api_path}",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    try:
        with opener.open(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 403:
            raise HubAuthError("JupyterHub token rejected: HTTP 403") from exc
        raise RuntimeError(f"create terminal failed: HTTP {exc.code}: {body[:500]}") from exc
    return str(payload["name"])


def list_terminals(hub_url: str, base_path: str, token: str) -> list[str]:
    return [item["name"] for item in list_terminals_detailed(hub_url, base_path, token)]


def list_terminals_detailed(hub_url: str, base_path: str, token: str) -> list[dict]:
    opener = build_opener()
    headers = {
        "Authorization": f"token {token}",
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }
    req = Request(
        f"{hub_url.rstrip('/')}{base_path}/api/terminals",
        headers=headers,
        method="GET",
    )
    with opener.open(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [
        {"name": str(it["name"]), "last_activity": it.get("last_activity")}
        for it in payload
        if "name" in it
    ]


def delete_terminal(hub_url: str, base_path: str, token: str, terminal_name: str) -> None:
    opener = build_opener()
    api_path = f"{base_path}/api/terminals/{quote(terminal_name)}"
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
        "Referer": f"{hub_url.rstrip('/')}{base_path}/tree",
    }
    headers["Authorization"] = f"token {token}"
    req = Request(
        f"{hub_url.rstrip('/')}{api_path}",
        headers=headers,
        method="DELETE",
    )
    with opener.open(req, timeout=20):
        pass


def reuse_terminal_enabled() -> bool:
    return os.environ.get("JUPYTER_REUSE_TERMINAL", "0") == "1"


def get_terminal(hub_url: str, base_path: str, token: str) -> str:
    if reuse_terminal_enabled():
        try:
            terminals = list_terminals(hub_url, base_path, token)
        except Exception:
            terminals = []
        if terminals:
            return terminals[-1]
    return create_terminal(hub_url, base_path, token)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def ws_send_text(sock: socket.socket, text: str) -> None:
    previous_timeout = sock.gettimeout()
    sock.settimeout(None)
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = secrets.token_bytes(4)
    header.extend(mask)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    try:
        sock.sendall(bytes(header) + masked)
    finally:
        sock.settimeout(previous_timeout)


def ws_recv_text(sock: socket.socket, timeout: float) -> str | None:
    sock.settimeout(timeout)
    first = recv_exact(sock, 2)
    opcode = first[0] & 0x0F
    length = first[1] & 0x7F
    masked = bool(first[1] & 0x80)
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    if opcode == 0x8:
        return None
    if opcode == 0x9:
        return ""
    return payload.decode("utf-8", "replace")


def drain_ws(sock: socket.socket, seconds: float = 0.8) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            ws_recv_text(sock, timeout=0.1)
        except socket.timeout:
            continue
        except Exception:
            return


def ws_connect(hub_url: str, base_path: str, terminal_name: str, token: str) -> socket.socket:
    parsed = urlparse(hub_url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    ws_path = f"{base_path}/terminals/websocket/{quote(terminal_name)}"
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    headers = [
        f"GET {ws_path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits",
        f"Origin: https://{host}",
        f"User-Agent: {DEFAULT_USER_AGENT}",
        "Accept-Language: ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        f"Referer: https://{host}{base_path}/tree",
    ]
    # JupyterHub accepts its API token as `token` for REST, but the terminal
    # WebSocket endpoint expects the OAuth-compatible Bearer form.
    headers.append(f"Authorization: Bearer {token}")
    headers.extend(["", ""])
    raw = socket.create_connection((host, port), timeout=20)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    sock.sendall("\r\n".join(headers).encode("utf-8"))
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)
    head = response.decode("iso-8859-1", "replace")
    if " 101 " not in head.split("\r\n", 1)[0]:
        if "tmgrdfrend/showcaptcha" in head:
            raise RuntimeError(
                "Jupyter/Yandex anti-bot redirected this script to /tmgrdfrend/showcaptcha. "
                "Pause before retrying and verify that the bridge uses Bearer auth for WebSocket."
            )
        raise RuntimeError(f"websocket upgrade failed: {head[:500]}")
    accept = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    expected = base64.b64encode(accept).decode()
    accept_headers = [
        line.split(":", 1)[1].strip()
        for line in head.split("\r\n")
        if line.lower().startswith("sec-websocket-accept:")
    ]
    if expected not in accept_headers:
        raise RuntimeError("websocket accept header did not match")
    return sock


def main() -> int:
    hub_url = os.environ.get("HUB_URL", "https://jupyter.culab.ru")
    command = " ".join(sys.argv[1:]) or "hostname; pwd; id; uname -a"
    marker = f"__CODEX_DONE_{secrets.token_hex(6)}__"
    token = load_hub_token()
    base_path = discover_base_path(hub_url, token)
    terminal_name = get_terminal(hub_url, base_path, token)
    print(f"base_path={base_path}")
    print(f"terminal={terminal_name}")
    sock: socket.socket | None = None
    try:
        sock = ws_connect(hub_url, base_path, terminal_name, token)
        if reuse_terminal_enabled():
            ws_send_text(sock, json.dumps(["stdin", "\x03\rstty sane\r"]))
            drain_ws(sock, 1.0)
        ws_send_text(sock, json.dumps(["stdin", f"{command}; printf '\\n{marker}:$?\\n'\r"]))
        deadline = time.time() + int(os.environ.get("JUPYTER_EXEC_TIMEOUT", "25"))
        output = []
        while time.time() < deadline:
            try:
                message = ws_recv_text(sock, timeout=1.5)
            except socket.timeout:
                continue
            if not message:
                continue
            try:
                kind, data = json.loads(message)
            except Exception:
                data = message
            if data:
                output.append(str(data))
                if "".join(output).count(marker) >= 2:
                    break
        print("".join(output))
    finally:
        if sock is not None:
            sock.close()
        if not reuse_terminal_enabled():
            try:
                delete_terminal(hub_url, base_path, token, terminal_name)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
