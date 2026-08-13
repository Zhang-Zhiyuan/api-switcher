"""Lightweight constants shared by network diagnostics UI and core modules."""

SERVICE_PING0 = "ping0"
SERVICE_NETCOFFEE = "netcoffee"
SERVICE_PROXYCHECK = "proxycheck"
SERVICE_IPAPI = "ipapi"
SERVICE_IPQS = "ipqs"
SERVICE_VPNAPI = "vpnapi"
SERVICE_ORDER = (SERVICE_NETCOFFEE, SERVICE_PING0, SERVICE_PROXYCHECK, SERVICE_IPAPI, SERVICE_IPQS, SERVICE_VPNAPI)
SERVICE_SET = set(SERVICE_ORDER)
HIDDEN_SERVICES = {SERVICE_IPQS}
VISIBLE_SERVICE_ORDER = tuple(service for service in SERVICE_ORDER if service not in HIDDEN_SERVICES)

SERVICE_ALIASES = {
    "netcoffee": SERVICE_NETCOFFEE,
    "net.coffee": SERVICE_NETCOFFEE,
    "ipnetcoffee": SERVICE_NETCOFFEE,
    "ip.net.coffee": SERVICE_NETCOFFEE,
    "ipnetcoffeeai": SERVICE_NETCOFFEE,
    "ping0": SERVICE_PING0,
    "ping0.cc": SERVICE_PING0,
    "ping0cc": SERVICE_PING0,
    "proxycheck": SERVICE_PROXYCHECK,
    "proxycheck.io": SERVICE_PROXYCHECK,
    "proxycheckio": SERVICE_PROXYCHECK,
    "ipapi": SERVICE_IPAPI,
    "ipapi.is": SERVICE_IPAPI,
    "ipapiis": SERVICE_IPAPI,
    "ipqs": SERVICE_IPQS,
    "ipqualityscore": SERVICE_IPQS,
    "ipqualityscore.com": SERVICE_IPQS,
    "ipqualityscorecom": SERVICE_IPQS,
    "vpnapi": SERVICE_VPNAPI,
    "vpnapi.io": SERVICE_VPNAPI,
    "vpnapiio": SERVICE_VPNAPI,
}

SERVICE_LABELS = {
    SERVICE_NETCOFFEE: "Net.Coffee AI",
    SERVICE_PING0: "Ping0",
    SERVICE_PROXYCHECK: "ProxyCheck",
    SERVICE_IPAPI: "ipapi.is",
    SERVICE_IPQS: "IPQualityScore",
    SERVICE_VPNAPI: "VPNAPI.io",
}

DEFAULT_ENABLED = {
    SERVICE_NETCOFFEE: True,
    SERVICE_PING0: False,
    SERVICE_PROXYCHECK: False,
    SERVICE_IPAPI: False,
    SERVICE_IPQS: False,
    SERVICE_VPNAPI: False,
}

ENV_KEYS = {
    SERVICE_NETCOFFEE: (),
    SERVICE_PING0: ("PING0_API_KEY",),
    SERVICE_PROXYCHECK: ("PROXYCHECK_API_KEY",),
    SERVICE_IPAPI: ("IPAPI_IS_API_KEY", "IPAPI_API_KEY"),
    SERVICE_IPQS: ("IPQS_API_KEY", "IPQUALITYSCORE_API_KEY"),
    SERVICE_VPNAPI: ("VPNAPI_KEY", "VPNAPI_API_KEY"),
}
