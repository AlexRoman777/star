from __future__ import annotations

import ipaddress


def trusted_private_address(
    *,
    client_host: str | None,
    edge_marker: str | None,
    forwarded_client_ip: str | None,
    require_edge: bool,
    allowed_cidrs: list[str],
) -> str:
    if require_edge:
        if edge_marker != "star-edge" or not forwarded_client_ip:
            raise ValueError("request did not pass through the private edge")
        source = forwarded_client_ip
    else:
        source = forwarded_client_ip or client_host or ""
    try:
        address = ipaddress.ip_address(source)
        allowed = any(address in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs)
    except ValueError as exc:
        raise ValueError("private request source is invalid") from exc
    if not allowed:
        raise ValueError("request source is outside the trusted private networks")
    return str(address)
