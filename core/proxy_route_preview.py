"""Pure preflight derived from the builder's rule ownership, never live state."""
from __future__ import annotations

import ipaddress

from core import proxy_routing


def route_preflight(preferences: dict, *, strict_privacy=None) -> dict:
    blueprint = proxy_routing.local_proxy._service_route_blueprint(preferences)
    domains = blueprint["proxy_domain_routes"]
    cidrs = blueprint["proxy_ip_cidr_routes"]
    notices = list(blueprint["overridden_targets"])
    # Find only the nearest matching ancestor; same-outbound overlaps need no
    # warning. Complexity is bounded by DNS labels / IP bits, not target pairs.
    for domain, route in domains.items():
        labels = domain.split(".")
        for offset in range(1, len(labels)):
            parent = ".".join(labels[offset:])
            if parent not in domains:
                continue
            if route != domains[parent]:
                notices.append({"target": domain, "parent": parent, "kind": "exception",
                                "shadowed": blueprint["domain_owners"][parent],
                                "winner": blueprint["domain_owners"][domain]})
            break
    networks = {ipaddress.ip_network(cidr, strict=False): cidr for cidr in cidrs}
    for network, cidr in networks.items():
        parent = network
        for _ in range(network.prefixlen):
            parent = parent.supernet()
            if parent not in networks:
                continue
            parent_cidr = networks[parent]
            if cidrs[cidr] != cidrs[parent_cidr]:
                notices.append({"target": cidr, "parent": parent_cidr, "kind": "exception",
                                "shadowed": blueprint["ip_owners"][parent_cidr],
                                "winner": blueprint["ip_owners"][cidr]})
            break
    direct = "DIRECT" in {*domains.values(), *cidrs.values()}
    return {"overlaps": notices, "direct": direct, "privacy_conflict": direct and strict_privacy is True}
