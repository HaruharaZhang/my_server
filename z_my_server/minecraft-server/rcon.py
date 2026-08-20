#!/usr/bin/env python3
"""极简 Minecraft RCON 客户端（仅标准库）。

用法: python3 rcon.py <命令...>
从同目录下 server/server.properties 读取 rcon 端口与密码。
"""
import pathlib
import socket
import struct
import sys

PROPS = pathlib.Path(__file__).resolve().parent / "server" / "server.properties"

AUTH_REQUEST = 3
COMMAND_REQUEST = 2


def read_config():
    port = 25575
    password = ""
    for line in PROPS.read_text(encoding="latin-1").splitlines():
        if line.startswith("rcon.port="):
            port = int(line.split("=", 1)[1])
        elif line.startswith("rcon.password="):
            password = line.split("=", 1)[1]
    if not password:
        print("server.properties 里没有 rcon.password", file=sys.stderr)
        sys.exit(1)
    return port, password


def send_packet(sock, request_id, packet_type, payload):
    body = struct.pack("<ii", request_id, packet_type) + payload.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(body)) + body)


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("rcon 连接被关闭")
        data += chunk
    return data


def recv_packet(sock):
    (length,) = struct.unpack("<i", recv_exact(sock, 4))
    body = recv_exact(sock, length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    text = body[8:-2].decode("utf-8", "replace")
    return request_id, packet_type, text


def main():
    command = " ".join(sys.argv[1:])
    if not command:
        print("用法: rcon.py <minecraft 命令>", file=sys.stderr)
        sys.exit(2)
    port, password = read_config()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        send_packet(sock, 1, AUTH_REQUEST, password)
        request_id, _, _ = recv_packet(sock)
        if request_id == -1:
            print("rcon 认证失败", file=sys.stderr)
            sys.exit(1)
        send_packet(sock, 2, COMMAND_REQUEST, command)
        _, _, text = recv_packet(sock)
        if text:
            print(text)


if __name__ == "__main__":
    main()
