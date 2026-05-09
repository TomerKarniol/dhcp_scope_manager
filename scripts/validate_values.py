#!/usr/bin/env python3
"""Validate DHCP Helm values files before Crossplane deployment.

Self-contained — no dependency on the application source tree.
Mirrors the backend validation logic so bad values fail in CI instead of
reaching the FastAPI backend or the Windows DHCP server.

# ============================================================================
# GitLab CI integration — add to .gitlab-ci.yml:
#
# validate-dhcp-values:
#   stage: validate
#   image: python:3.12-slim
#   before_script:
#     - pip install --quiet -r scripts/requirements.txt
#   script:
#     - python scripts/validate_values.py --repo-root . --warnings-as-errors
#   rules:
#     - if: $CI_PIPELINE_SOURCE == "merge_request_event"
#       changes:
#         - sites/**/*.yaml
#     - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
# ============================================================================

Usage:
  # Main CI mode — discover and validate all hosted clusters in the repo
  python scripts/validate_values.py --repo-root . --warnings-as-errors

  # Manual layered validation (site → MCE → hosted-cluster)
  python scripts/validate_values.py \\
    --site-values     sites/site-a/values.yaml \\
    --mce-values      sites/site-a/mce-a/values.yaml \\
    --hosted-cluster-values sites/site-a/mce-a/cluster-a/values.yaml

  # Single-file validation (no inheritance)
  python scripts/validate_values.py --values helm/values.yaml

Exit codes:
  0  Validation passed
  1  One or more errors (or warnings with --warnings-as-errors)
  2  Script/IO error (file not found, bad YAML, etc.)

Dependencies: pydantic>=2.8, PyYAML>=6.0 (see scripts/requirements.txt)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field as dc_field
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ERROR = "error"
WARNING = "warning"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    severity: Literal["error", "warning"]
    path: str           # dotted field path, e.g. "dhcp_values.network"
    message: str
    suggestion: str | None = None
    # Where the value came from — set when source can be determined
    source: str | None = None  # "hosted-cluster", "mce", "site", "default"


Issues = list[ValidationIssue]


@dataclass
class ClusterContext:
    """Describes one hosted cluster and the files that make up its merge chain."""
    site: str
    mce: str
    cluster: str
    site_file: Path | None
    mce_file: Path | None
    cluster_file: Path

    def merge_chain(self) -> list[Path]:
        return [f for f in (self.site_file, self.mce_file, self.cluster_file) if f is not None]

    def label(self) -> str:
        return f"{self.site}/{self.mce}/{self.cluster}"


@dataclass
class ValidationResult:
    context: ClusterContext | None   # None in single-file mode
    issues: Issues = dc_field(default_factory=list)

    @property
    def errors(self) -> Issues:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> Issues:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)


# ---------------------------------------------------------------------------
# Issue factory helpers
# ---------------------------------------------------------------------------

def _err(path: str, message: str, suggestion: str | None = None,
         source: str | None = None) -> ValidationIssue:
    return ValidationIssue(ERROR, path, message, suggestion, source)


def _warn(path: str, message: str, suggestion: str | None = None,
          source: str | None = None) -> ValidationIssue:
    return ValidationIssue(WARNING, path, message, suggestion, source)


# ---------------------------------------------------------------------------
# YAML loading and deep merging
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place — mirrors Helm -f merge semantics.

    - null in override replaces the key (how you disable inherited maps).
    - dict in override is merged recursively into the same key in base.
    - Any other value in override replaces the base value.
    """
    for key, value in override.items():
        if value is None:
            base[key] = None
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_yaml_file(path: Path) -> dict:
    """Load a YAML file and return its content as a dict. Exits on IO/parse error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse error in {path}:\n  {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"ERROR: {path} does not contain a YAML mapping at the top level", file=sys.stderr)
        sys.exit(2)
    return data


def merge_files(*paths: Path) -> dict:
    """Load and deep-merge YAML files in order (later files win)."""
    merged: dict = {}
    for p in paths:
        _deep_merge(merged, load_yaml_file(p))
    return merged


# ---------------------------------------------------------------------------
# Nested field access helpers
# ---------------------------------------------------------------------------

def get_nested(data: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts. Returns default if any key is missing."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is None:
            return default
    return current


def _nested_present(data: dict, *keys: str) -> bool:
    """Return True if the nested key path exists and has a non-None value."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return current is not None


# ---------------------------------------------------------------------------
# Kubernetes name validation helpers
# ---------------------------------------------------------------------------

_K8S_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")


def _is_valid_k8s_label(name: str) -> bool:
    """Return True if name is a valid Kubernetes DNS label (RFC 1123)."""
    return len(name) <= 63 and bool(_K8S_DNS_LABEL_RE.match(name))


# ---------------------------------------------------------------------------
# Pydantic models — self-contained copies of app/models/*.py
#
# These are intentionally duplicated here so this script is standalone.
# When you change app/models, update these models too to keep CI and runtime
# validation identical.
# ---------------------------------------------------------------------------

def _ip_to_int(ip: IPv4Address) -> int:
    return int(ip)


class _CiDhcpExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    startAddress: IPv4Address
    endAddress: IPv4Address

    @model_validator(mode="after")
    def end_gte_start(self) -> "_CiDhcpExclusion":
        if int(self.endAddress) < int(self.startAddress):
            raise ValueError(
                f"endAddress {self.endAddress} must be >= startAddress {self.startAddress}"
            )
        return self


class _CiDhcpFailover(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partnerServer: str = Field(min_length=1, max_length=255)
    relationshipName: str = Field(min_length=1, max_length=64)
    mode: Literal["HotStandby", "LoadBalance"]
    serverRole: Optional[Literal["Active", "Standby"]] = None
    reservePercent: int = Field(default=0, ge=0, le=100)
    loadBalancePercent: Optional[int] = Field(default=None, ge=0, le=100)
    maxClientLeadTimeMinutes: int = Field(ge=1, le=1440)

    @field_validator("partnerServer", "relationshipName")
    @classmethod
    def not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank or whitespace-only")
        return v

    @model_validator(mode="after")
    def enforce_mode_fields(self) -> "_CiDhcpFailover":
        if self.mode == "HotStandby":
            if self.serverRole is None:
                raise ValueError("serverRole is required when mode is 'HotStandby'")
            self.loadBalancePercent = 0
        else:  # LoadBalance
            if self.loadBalancePercent is None:
                raise ValueError("loadBalancePercent is required when mode is 'LoadBalance'")
            self.serverRole = "Active"
            self.reservePercent = 0
        return self


class _CiDhcpScopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopeName: str = Field(min_length=1, max_length=256)

    @field_validator("scopeName")
    @classmethod
    def scope_name_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scopeName must not be blank or whitespace-only")
        return v

    network: IPv4Address
    subnetMask: IPv4Address
    startRange: IPv4Address
    endRange: IPv4Address
    leaseDurationDays: int = Field(ge=1, le=3650)
    description: str = Field(default="", max_length=1024)

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v: object) -> object:
        return "" if v is None else v

    gateway: Optional[IPv4Address] = None

    @field_validator("gateway", mode="before")
    @classmethod
    def normalize_gateway(cls, v: object) -> object:
        return None if v == "" else v

    dnsServers: list[IPv4Address] = Field(default_factory=list, min_length=1)
    dnsDomain: str = Field(default="", max_length=256)

    @field_validator("dnsDomain", mode="before")
    @classmethod
    def normalize_dns_domain(cls, v: object) -> object:
        return "" if v is None else v

    exclusions: list[_CiDhcpExclusion] = Field(default_factory=list)
    failover: Optional[_CiDhcpFailover] = None

    @model_validator(mode="after")
    def no_duplicate_exclusions(self) -> "_CiDhcpScopePayload":
        seen: set[tuple] = set()
        for i, excl in enumerate(self.exclusions):
            key = (excl.startAddress, excl.endAddress)
            if key in seen:
                raise ValueError(
                    f"exclusions[{i}] {excl.startAddress}-{excl.endAddress} is a duplicate"
                )
            seen.add(key)
        return self

    @model_validator(mode="after")
    def no_overlapping_exclusions(self) -> "_CiDhcpScopePayload":
        if len(self.exclusions) < 2:
            return self
        sorted_excl = sorted(
            self.exclusions,
            key=lambda x: (_ip_to_int(x.startAddress), _ip_to_int(x.endAddress)),
        )
        for i in range(len(sorted_excl) - 1):
            a, b = sorted_excl[i], sorted_excl[i + 1]
            if _ip_to_int(a.endAddress) >= _ip_to_int(b.startAddress):
                raise ValueError(
                    f"exclusions overlap: {a.startAddress}-{a.endAddress} "
                    f"overlaps with {b.startAddress}-{b.endAddress}"
                )
        return self

    @model_validator(mode="after")
    def end_range_gte_start_range(self) -> "_CiDhcpScopePayload":
        if int(self.endRange) < int(self.startRange):
            raise ValueError(
                f"endRange {self.endRange} must be >= startRange {self.startRange}"
            )
        return self

    @model_validator(mode="after")
    def validate_subnet_consistency(self) -> "_CiDhcpScopePayload":
        try:
            subnet = IPv4Network(f"{self.network}/{self.subnetMask}", strict=True)
        except ValueError as exc:
            raise ValueError(
                f"network {self.network} with subnetMask {self.subnetMask} "
                f"is not a valid subnet: {exc}"
            ) from exc

        for fname, ip in [("startRange", self.startRange), ("endRange", self.endRange)]:
            if ip not in subnet:
                raise ValueError(f"{fname} {ip} is not within subnet {subnet}")
        if self.gateway is not None and self.gateway not in subnet:
            raise ValueError(f"gateway {self.gateway} is not within subnet {subnet}")
        for i, excl in enumerate(self.exclusions):
            for attr in ("startAddress", "endAddress"):
                ip = getattr(excl, attr)
                if ip not in subnet:
                    raise ValueError(
                        f"exclusions[{i}].{attr} {ip} is not within subnet {subnet}"
                    )

        net_addr = subnet.network_address
        bcast_addr = subnet.broadcast_address
        for fname, ip in [("startRange", self.startRange), ("endRange", self.endRange)]:
            if ip == net_addr:
                raise ValueError(f"{fname} {ip} must not be the network address {net_addr}")
            if ip == bcast_addr:
                raise ValueError(f"{fname} {ip} must not be the broadcast address {bcast_addr}")
        if self.gateway is not None:
            if self.gateway == net_addr:
                raise ValueError(f"gateway {self.gateway} must not be the network address {net_addr}")
            if self.gateway == bcast_addr:
                raise ValueError(f"gateway {self.gateway} must not be the broadcast address {bcast_addr}")
        return self

    @model_validator(mode="after")
    def gateway_not_in_distribution_range(self) -> "_CiDhcpScopePayload":
        """Reject gateway inside [startRange, endRange] unless covered by an exclusion.

        Without an exclusion the DHCP server would lease the gateway address to a
        client, causing a network outage for every host on the subnet.
        """
        if self.gateway is None:
            return self
        gw_int = int(self.gateway)
        if not (int(self.startRange) <= gw_int <= int(self.endRange)):
            return self
        for excl in self.exclusions:
            if int(excl.startAddress) <= gw_int <= int(excl.endAddress):
                return self
        raise ValueError(
            f"gateway {self.gateway} is inside the DHCP distribution range "
            f"{self.startRange}-{self.endRange} but is not covered by any exclusion. "
            "Add an exclusion for the gateway address or move it outside the distribution range."
        )


# ---------------------------------------------------------------------------
# Field mapping: YAML dhcp_values structure → Pydantic kwargs
# ---------------------------------------------------------------------------

def _to_scope_kwargs(dv: dict) -> dict:
    """Map dhcp_values (from YAML) to _CiDhcpScopePayload constructor kwargs.

    The values file uses dns.servers / dns.domain; the Pydantic model uses
    flat dnsServers / dnsDomain fields. Everything else is 1-to-1.
    """
    dns = dv.get("dns") or {}
    return {
        "scopeName":         dv.get("scopeName"),
        "network":           dv.get("network"),
        "subnetMask":        dv.get("subnetMask"),
        "startRange":        dv.get("startRange"),
        "endRange":          dv.get("endRange"),
        "leaseDurationDays": dv.get("leaseDurationDays"),
        "description":       dv.get("description") or "",
        "gateway":           dv.get("gateway"),
        "dnsServers":        dns.get("servers") or [],
        "dnsDomain":         dns.get("domain") or "",
        "exclusions":        dv.get("exclusions") or [],
        "failover":          dv.get("failover"),
    }


# ---------------------------------------------------------------------------
# Required-field pre-checks (run before Pydantic for cleaner error messages)
# ---------------------------------------------------------------------------

_REQUIRED_PATHS: list[tuple[str, ...]] = [
    # DHCP scope fields
    ("dhcp_values", "scopeName"),
    ("dhcp_values", "network"),
    ("dhcp_values", "subnetMask"),
    ("dhcp_values", "startRange"),
    ("dhcp_values", "endRange"),
    ("dhcp_values", "leaseDurationDays"),
    ("dhcp_values", "dns", "servers"),
    # API server
    ("apiServer", "url"),
    ("apiServer", "tokenSecretRef", "name"),
    ("apiServer", "tokenSecretRef", "namespace"),
    ("apiServer", "tokenSecretRef", "key"),
]


def validate_required_fields(values: dict) -> Issues:
    """Check that all required fields are present after inheritance resolution."""
    return [
        _err(".".join(keys), "is required but missing or null")
        for keys in _REQUIRED_PATHS
        if not _nested_present(values, *keys)
    ]


# ---------------------------------------------------------------------------
# DHCP scope validator (Pydantic)
# ---------------------------------------------------------------------------

def validate_dhcp_pydantic(values: dict) -> Issues:
    """Run the full Pydantic scope model. Returns one ValidationIssue per error."""
    dv = values.get("dhcp_values")
    if not isinstance(dv, dict):
        return [_err("dhcp_values", f"must be a YAML mapping, got {type(dv).__name__}")]

    kwargs = _to_scope_kwargs(dv)
    try:
        _CiDhcpScopePayload(**kwargs)
        return []
    except ValidationError as exc:
        issues: Issues = []
        for error in exc.errors():
            loc = ".".join(str(p) for p in error["loc"])
            msg = error["msg"].removeprefix("Value error, ")
            path = f"dhcp_values.{loc}" if loc else "dhcp_values"
            suggestion = _dhcp_suggestion(msg, kwargs)
            issues.append(_err(path, msg, suggestion))
        return issues


def _dhcp_suggestion(msg: str, kwargs: dict) -> str | None:
    """Return a helpful suggestion string for common Pydantic error messages."""
    if "not within subnet" in msg or "not the network address" in msg or "not the broadcast address" in msg:
        return "Choose a host address inside the subnet, not the network or broadcast address."
    if "is not a valid subnet" in msg:
        try:
            net = IPv4Address(str(kwargs.get("network", "")))
            mask = str(kwargs.get("subnetMask", ""))
            correct = str(IPv4Network(f"{net}/{mask}", strict=False).network_address)
            if correct != str(net):
                return f'The correct network address for mask {mask} is "{correct}". Use that instead of "{net}".'
        except Exception:
            pass
    if "inside the DHCP distribution range" in msg:
        gw = kwargs.get("gateway")
        return (
            f"Add an exclusion covering {gw} to prevent the DHCP server from leasing "
            "the gateway address to a client, or move the gateway outside the distribution range."
        )
    return None


# ---------------------------------------------------------------------------
# Exclusion order (parity risk warning)
# ---------------------------------------------------------------------------

def validate_exclusion_order(values: dict) -> Issues:
    """Warn if exclusions are not sorted — the backend always returns them sorted.

    An unsorted values file causes Crossplane to detect a mismatch on every GET
    and issue a PUT every ~60 seconds indefinitely (reconciliation loop).
    """
    exclusions = get_nested(values, "dhcp_values", "exclusions") or []
    if not isinstance(exclusions, list) or len(exclusions) < 2:
        return []

    ints: list[int] = []
    for excl in exclusions:
        try:
            ints.append(int(IPv4Address(str(excl.get("startAddress", "")))))
        except (AddressValueError, ValueError, AttributeError):
            return []  # malformed IP — Pydantic will catch this

    if ints != sorted(ints):
        return [_warn(
            "dhcp_values.exclusions",
            "exclusions are not in ascending IP order; the backend always returns them "
            "sorted, so Crossplane will detect a mismatch and PUT every ~60 seconds.",
            suggestion="Sort exclusions by startAddress in ascending IP numerical order.",
        )]
    return []


# ---------------------------------------------------------------------------
# DNS validators
# ---------------------------------------------------------------------------

def validate_dns_duplicates(values: dict) -> Issues:
    """Warn if the DNS server list contains duplicate entries."""
    servers = get_nested(values, "dhcp_values", "dns", "servers") or []
    if not isinstance(servers, list):
        return []
    seen: set[str] = set()
    for srv in servers:
        s = str(srv)
        if s in seen:
            return [_warn(
                "dhcp_values.dns.servers",
                f"duplicate DNS server {s!r}; remove the duplicate.",
            )]
        seen.add(s)
    return []


def validate_dns_domain(values: dict) -> Issues:
    """Validate dns.domain format if present."""
    domain = get_nested(values, "dhcp_values", "dns", "domain")
    if not domain:
        return []
    issues: Issues = []
    if not isinstance(domain, str):
        return [_err("dhcp_values.dns.domain", "must be a string")]
    if " " in domain:
        issues.append(_err(
            "dhcp_values.dns.domain",
            f"{domain!r} contains spaces — not valid for DHCP option 15 (DNS search domain)",
        ))
    if len(domain) > 256:
        issues.append(_err(
            "dhcp_values.dns.domain",
            f"exceeds 256 characters (got {len(domain)})",
        ))
    return issues


# ---------------------------------------------------------------------------
# API server validator
# ---------------------------------------------------------------------------

def validate_api_server(values: dict) -> Issues:
    """Validate apiServer.url and tokenSecretRef fields."""
    issues: Issues = []
    api = values.get("apiServer") or {}

    url = api.get("url")
    if url:
        parsed = urlparse(str(url))
        if parsed.scheme not in ("http", "https"):
            issues.append(_err(
                "apiServer.url",
                f"{url!r} must use http or https scheme",
                suggestion="Use a URL like 'https://dhcp-api.example.com'.",
            ))
        elif not parsed.netloc:
            issues.append(_err("apiServer.url", f"{url!r} has no hostname"))
        if str(url).endswith("/"):
            issues.append(_warn(
                "apiServer.url",
                f"{url!r} ends with '/'; the Helm template appends '/api/v1/scopes' which "
                "will produce a double slash and may cause 404 responses.",
                suggestion="Remove the trailing slash.",
            ))

    ref = api.get("tokenSecretRef") or {}
    for fname in ("name", "namespace", "key"):
        val = ref.get(fname)
        if val is not None and not _is_valid_k8s_label(str(val)):
            issues.append(_err(
                f"apiServer.tokenSecretRef.{fname}",
                f"{val!r} is not a valid Kubernetes name "
                "(must be lowercase alphanumeric and hyphens, 1–63 characters, "
                "start and end with alphanumeric)",
            ))
    return issues


# ---------------------------------------------------------------------------
# Crossplane / Kubernetes validators
# ---------------------------------------------------------------------------

def validate_crossplane(values: dict) -> Issues:
    """Validate crossplane.namespace and providerConfigName."""
    issues: Issues = []
    cp = values.get("crossplane") or {}

    ns = cp.get("namespace")
    if ns is not None and not _is_valid_k8s_label(str(ns)):
        issues.append(_err(
            "crossplane.namespace",
            f"{ns!r} is not a valid Kubernetes namespace name "
            "(lowercase alphanumeric and hyphens, max 63 characters)",
        ))

    pcn = cp.get("providerConfigName")
    if pcn is not None and not _is_valid_k8s_label(str(pcn)):
        issues.append(_err(
            "crossplane.providerConfigName",
            f"{pcn!r} is not a valid Kubernetes resource name",
        ))

    return issues


def validate_kubernetes_names(values: dict) -> Issues:
    """Validate that the Crossplane CR name generated from network is a valid k8s label.

    The Helm template generates: dhcp-scope-{{ network | replace "." "-" }}
    e.g. "10.20.30.0" → "dhcp-scope-10-20-30-0"
    """
    network = get_nested(values, "dhcp_values", "network")
    if not network:
        return []  # caught by required-field check

    cr_name = f"dhcp-scope-{str(network).replace('.', '-')}"
    if len(cr_name) > 63:
        return [_err(
            "dhcp_values.network",
            f"generated Crossplane CR name '{cr_name}' exceeds 63 characters (got {len(cr_name)})",
        )]
    if not _K8S_DNS_LABEL_RE.match(cr_name):
        return [_err(
            "dhcp_values.network",
            f"generated Crossplane CR name '{cr_name}' contains invalid characters",
        )]
    return []


# ---------------------------------------------------------------------------
# Crossplane reconciliation parity risk warnings
# ---------------------------------------------------------------------------

def validate_parity_risks(values: dict) -> Issues:
    """Warn about values known to cause Crossplane reconciliation drift (PUT loops).

    These are not hard errors but will cause Crossplane to PUT every ~60 seconds
    indefinitely if left unaddressed.
    """
    issues: Issues = []
    dv = values.get("dhcp_values") or {}

    # failover: {} survives Helm deep-merge from parent layers.
    # Only failover: null removes an inherited failover config.
    failover_raw = dv.get("failover")
    if isinstance(failover_raw, dict) and not failover_raw:
        issues.append(_warn(
            "dhcp_values.failover",
            "failover is set to {} (empty object). Helm deep-merges {} with any "
            "inherited failover values, so the parent failover config will still apply.",
            suggestion="Use 'failover: null' to explicitly disable failover.",
        ))

    # description omitted → backend normalizes to "" → Helm renders ""
    # This is safe but explicit is better than implicit.
    if "description" not in dv:
        issues.append(_warn(
            "dhcp_values.description",
            "description is not set; it defaults to '' in the backend. "
            "Set it explicitly to make intent clear and avoid unexpected drift "
            "if a parent layer sets a non-empty description.",
            suggestion="Add 'description: \"\"' or a meaningful description.",
        ))

    # gateway omitted → treated as null → no DHCP option 3 sent to clients.
    # Explicit is better here for the same inheritance reason.
    if "gateway" not in dv:
        issues.append(_warn(
            "dhcp_values.gateway",
            "gateway is not set. It defaults to null (no router option sent to clients). "
            "If a parent layer sets a gateway, omitting it here will NOT override it to null — "
            "use 'gateway: null' or 'gateway: \"\"' to explicitly clear it.",
            suggestion="Add 'gateway: \"\"' to explicitly disable, or set a gateway IP.",
        ))

    return issues


# ---------------------------------------------------------------------------
# Master validator — runs all checks and collects every issue
# ---------------------------------------------------------------------------

def validate_effective_values(values: dict) -> Issues:
    """Run all validators against merged effective values. Returns all issues."""
    issues: Issues = []
    issues += validate_required_fields(values)
    # Only run deeper checks if the dhcp_values block exists
    if isinstance(values.get("dhcp_values"), dict):
        issues += validate_dhcp_pydantic(values)
        issues += validate_exclusion_order(values)
        issues += validate_dns_duplicates(values)
        issues += validate_dns_domain(values)
        issues += validate_kubernetes_names(values)
        issues += validate_parity_risks(values)
    issues += validate_api_server(values)
    issues += validate_crossplane(values)
    return issues


# ---------------------------------------------------------------------------
# Cluster discovery
# ---------------------------------------------------------------------------

def _find_values_file(directory: Path) -> Path | None:
    """Find values.yaml or config.yaml in a directory (values.yaml takes priority)."""
    for name in ("values.yaml", "config.yaml"):
        p = directory / name
        if p.is_file():
            return p
    return None


def discover_clusters(sites_dir: Path) -> list[ClusterContext]:
    """Walk sites/ and return one ClusterContext per hosted-cluster values file.

    Supports two directory layouts:

    New layout (values.yaml at every level):
      sites/{site}/{mce}/{cluster}/values.yaml

    Old layout (config.yaml at site/MCE level, named YAML at cluster level):
      sites/{site}/mce/{mce}/hosted-cluster/{cluster}.yaml

    Both layouts may coexist in the same repo.
    """
    if not sites_dir.is_dir():
        print(f"ERROR: sites/ directory not found: {sites_dir}", file=sys.stderr)
        sys.exit(2)

    contexts: list[ClusterContext] = []
    seen: set[Path] = set()

    def _add(cluster_file: Path, mce_dir: Path, site_dir: Path,
             site: str, mce: str) -> None:
        cluster_file = cluster_file.resolve()
        if cluster_file in seen:
            return
        seen.add(cluster_file)
        # Cluster name: parent dir name for values.yaml, stem for named yaml files
        cluster = (
            cluster_file.parent.name
            if cluster_file.name == "values.yaml"
            else cluster_file.stem
        )
        contexts.append(ClusterContext(
            site=site,
            mce=mce,
            cluster=cluster,
            site_file=_find_values_file(site_dir),
            mce_file=_find_values_file(mce_dir),
            cluster_file=cluster_file,
        ))

    for site_dir in sorted(sites_dir.iterdir()):
        if not site_dir.is_dir():
            continue
        site = site_dir.name

        for sub in sorted(site_dir.iterdir()):
            if not sub.is_dir():
                continue

            # Old layout: sites/{site}/mce/{mce}/hosted-cluster/{cluster}.yaml
            if sub.name == "mce":
                for mce_dir in sorted(sub.iterdir()):
                    if not mce_dir.is_dir():
                        continue
                    mce = mce_dir.name
                    hc_dir = mce_dir / "hosted-cluster"
                    if hc_dir.is_dir():
                        for cf in sorted(hc_dir.glob("*.yaml")):
                            _add(cf, mce_dir, site_dir, site, mce)
                continue

            # New layout: sites/{site}/{mce}/{cluster}/values.yaml
            mce = sub.name
            for cluster_dir in sorted(sub.iterdir()):
                if not cluster_dir.is_dir():
                    continue
                cf = cluster_dir / "values.yaml"
                if cf.is_file():
                    _add(cf, sub, site_dir, site, mce)

    return sorted(contexts, key=lambda c: (c.site, c.mce, c.cluster))


# ---------------------------------------------------------------------------
# Terminal color helpers
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"


def _c(text: str, *codes: str, use_color: bool = True) -> str:
    return ("".join(codes) + text + _RESET) if use_color else text


# ---------------------------------------------------------------------------
# Text reporter
# ---------------------------------------------------------------------------

def _print_cluster_header(ctx: ClusterContext, use_color: bool) -> None:
    print(f"\n{'─' * 60}")
    print(f"Validating hosted cluster: {_c(ctx.label(), _BOLD, use_color=use_color)}")
    rows = [
        ("site",           ctx.site,    ctx.site_file),
        ("mce",            ctx.mce,     ctx.mce_file),
        ("hosted-cluster", ctx.cluster, ctx.cluster_file),
    ]
    for level, name, path in rows:
        path_str = str(path) if path else _c("(no file)", _DIM, use_color=use_color)
        print(f"  {level:15s} {_c(name, _BOLD, use_color=use_color):30s} {_c(path_str, _DIM, use_color=use_color)}")


def print_text_report(results: list[ValidationResult], use_color: bool = True) -> None:
    for res in results:
        if res.context:
            _print_cluster_header(res.context, use_color)
        else:
            print(f"\n{'─' * 60}")
            print("Validating: single values file")

        if not res.issues:
            print(f"  {_c('PASS', _GREEN, _BOLD, use_color=use_color)} — no issues found")
            continue

        for issue in res.issues:
            if issue.severity == ERROR:
                sev = _c("ERROR", _RED, _BOLD, use_color=use_color)
            else:
                sev = _c("WARNING", _YELLOW, _BOLD, use_color=use_color)

            print(f"\n  {sev} {_c(issue.path, _BOLD, use_color=use_color)}:")
            print(f"    {issue.message}")
            if issue.suggestion:
                print(f"    {_c('Suggestion:', _CYAN, use_color=use_color)} {issue.suggestion}")


def print_summary(results: list[ValidationResult], warnings_as_errors: bool,
                  use_color: bool = True) -> None:
    total = len(results)
    n_errors  = sum(1 for r in results if r.has_errors)
    n_warned  = sum(1 for r in results if r.has_warnings and not r.has_errors)
    n_passed  = total - n_errors - n_warned

    print(f"\n{'═' * 60}")
    print(f"Validated {total} cluster(s):")
    print(f"  {_c(str(n_passed) + ' passed',  _GREEN  if n_passed  else _DIM, _BOLD, use_color=use_color)}")
    print(f"  {_c(str(n_errors) + ' failed',  _RED    if n_errors  else _DIM, _BOLD, use_color=use_color)}")
    print(f"  {_c(str(n_warned) + ' warnings',_YELLOW if n_warned  else _DIM, _BOLD, use_color=use_color)}"
          + (" ← treated as errors (--warnings-as-errors)" if warnings_as_errors and n_warned else ""))

    failed = [r for r in results
              if r.has_errors or (warnings_as_errors and r.has_warnings)]
    if failed:
        print("\nFailed:")
        for r in failed:
            label = r.context.label() if r.context else "single-file"
            print(f"  {_c('FAIL', _RED, _BOLD, use_color=use_color)}  {label}")


# ---------------------------------------------------------------------------
# JSON reporter
# ---------------------------------------------------------------------------

def print_json_report(results: list[ValidationResult]) -> None:
    output = []
    for res in results:
        ctx = res.context
        output.append({
            "cluster":        ctx.label() if ctx else None,
            "site":           ctx.site if ctx else None,
            "mce":            ctx.mce if ctx else None,
            "hosted_cluster": ctx.cluster if ctx else None,
            "files": {
                "site":           str(ctx.site_file) if ctx and ctx.site_file else None,
                "mce":            str(ctx.mce_file) if ctx and ctx.mce_file else None,
                "hosted_cluster": str(ctx.cluster_file) if ctx else None,
            },
            "passed": not res.has_errors,
            "issues": [
                {
                    "severity":   i.severity,
                    "path":       i.path,
                    "message":    i.message,
                    "suggestion": i.suggestion,
                    "source":     i.source,
                }
                for i in res.issues
            ],
        })
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="validate_values.py",
        description="Validate DHCP Helm values files for Crossplane deployment.",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--repo-root", metavar="DIR",
        help="Auto-discover and validate all hosted clusters under DIR/sites/",
    )
    mode.add_argument(
        "--values", metavar="FILE",
        help="Single-file mode: validate one YAML file directly (no inheritance)",
    )

    p.add_argument("--site-values",            metavar="FILE", help="Site-level values file")
    p.add_argument("--mce-values",             metavar="FILE", help="MCE-level values file")
    p.add_argument("--hosted-cluster-values",  metavar="FILE", help="Hosted-cluster values file")

    p.add_argument(
        "--warnings-as-errors", "--strict", action="store_true",
        help="Exit 1 if any warnings exist (recommended for CI)",
    )
    p.add_argument(
        "--output", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    p.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color output",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    use_color = not args.no_color and sys.stdout.isatty()
    warnings_as_errors: bool = args.warnings_as_errors

    results: list[ValidationResult] = []

    # ── repo-root mode (main CI mode) ───────────────────────────────────────
    if args.repo_root:
        repo = Path(args.repo_root).resolve()
        clusters = discover_clusters(repo / "sites")

        if not clusters:
            print("No hosted-cluster values files found under sites/ — nothing to validate.")
            sys.exit(0)

        print(f"Found {len(clusters)} hosted cluster(s) to validate.")

        for ctx in clusters:
            merged = merge_files(*ctx.merge_chain())
            issues = validate_effective_values(merged)
            results.append(ValidationResult(context=ctx, issues=issues))

    # ── manual layered mode ─────────────────────────────────────────────────
    elif args.hosted_cluster_values or args.site_values or args.mce_values:
        if not args.hosted_cluster_values:
            print("ERROR: --hosted-cluster-values is required in manual mode.", file=sys.stderr)
            sys.exit(2)

        cluster_path = Path(args.hosted_cluster_values)
        site_path    = Path(args.site_values)   if args.site_values   else None
        mce_path     = Path(args.mce_values)    if args.mce_values    else None

        ctx = ClusterContext(
            site    = site_path.parent.name    if site_path    else "site",
            mce     = mce_path.parent.name     if mce_path     else "mce",
            cluster = (cluster_path.parent.name
                       if cluster_path.name == "values.yaml"
                       else cluster_path.stem),
            site_file    = site_path,
            mce_file     = mce_path,
            cluster_file = cluster_path,
        )
        merged = merge_files(*ctx.merge_chain())
        results.append(ValidationResult(context=ctx, issues=validate_effective_values(merged)))

    # ── single-file mode ────────────────────────────────────────────────────
    elif args.values:
        merged = merge_files(Path(args.values))
        results.append(ValidationResult(context=None, issues=validate_effective_values(merged)))

    else:
        print(
            "ERROR: specify one of --repo-root, --values, or --hosted-cluster-values.",
            file=sys.stderr,
        )
        sys.exit(2)

    # ── output ──────────────────────────────────────────────────────────────
    if args.output == "json":
        print_json_report(results)
    else:
        print_text_report(results, use_color=use_color)
        print_summary(results, warnings_as_errors, use_color=use_color)

    # ── exit code ───────────────────────────────────────────────────────────
    has_errors   = any(r.has_errors   for r in results)
    has_warnings = any(r.has_warnings for r in results)
    sys.exit(1 if (has_errors or (warnings_as_errors and has_warnings)) else 0)


if __name__ == "__main__":
    main()
