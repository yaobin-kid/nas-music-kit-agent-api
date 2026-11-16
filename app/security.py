"""访问控制：CIDR 白名单与 HTTP Basic 认证。"""

from __future__ import annotations

import base64
import ipaddress
import secrets
from dataclasses import dataclass

from fastapi import Request

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class AccessPolicy:
    networks: tuple[IPNetwork, ...]
    user: str
    password: str
    trust_proxy: bool


def parse_networks(value: str) -> tuple[IPNetwork, ...]:
    """解析逗号分隔的 IP/CIDR；空字符串表示不限制。"""
    networks = []
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ValueError(f"ALLOWED_CIDRS 格式无效: {item}") from exc
    return tuple(networks)


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip", "")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else ""


def ip_allowed(host: str, networks: tuple[IPNetwork, ...]) -> bool:
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def check_basic_auth(header: str, username: str, password: str) -> bool:
    """未同时配置账号密码时视为关闭认证。"""
    if not username or not password:
        return True
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return False
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, sep, pwd = decoded.partition(":")
    if not sep:
        return False
    return secrets.compare_digest(user.encode(), username.encode()) and secrets.compare_digest(
        pwd.encode(), password.encode()
    )
