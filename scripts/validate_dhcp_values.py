#!/usr/bin/env python3
"""Validate dhcp_values from one or more Helm values files.

Self-contained — no dependency on the application source tree.
Add this file to your CI pipeline alongside scripts/requirements.txt.

Usage:
    # Single values file
    python scripts/validate_dhcp_values.py helm/values.yaml

    # Layered values (site defaults + cluster override, same precedence as helm -f)
    python scripts/validate_dhcp_values.py site/defaults.yaml clusters/cluster-a.yaml

Exit codes:
    0 — validation passed
    1 — validation failed (details printed to stderr)

Dependencies (see scripts/requirements.txt):
    pydantic==2.8.0
    PyYAML>=6.0,<7.0

EXAMPLE CI
validate-dhcp-values:
  stage: validate
  image: python:3.12-slim
  before_script:
    - pip install --quiet -r scripts/requirements.txt
  script:
    - python scripts/validate_changed_clusters.py
  rules:
    - changes:
        - sites/**/*.yaml
"""
from __future__ import annotations

import sys
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ValidationError


def _ip_to_int(ip: IPv4Address) -> int:
    return int(ip)


# ---------------------------------------------------------------------------
# Pydantic models — kept in sync with app/models.py
# These are intentionally duplicated here so this script is self-contained.
# If you change app/models.py, update these models too.
# ---------------------------------------------------------------------------

class DhcpExclusion(BaseModel):
    startAddress: IPv4Address
    endAddress: IPv4Address

    @model_validator(mode="after")
    def end_gte_start(self) -> "DhcpExclusion":
        if int(self.endAddress) < int(self.startAddress):
            raise ValueError(
                f"endAddress {self.endAddress} must be >= startAddress {self.startAddress}"
            )
        return self


class DhcpFailover(BaseModel):
    partnerServer: str = Field(min_length=1, max_length=255)
    relationshipName: str = Field(min_length=1, max_length=64)
    mode: Literal["HotStandby", "LoadBalance"]
    serverRole: Optional[Literal["Active", "Standby"]] = None
    reservePercent: int = Field(default=0, ge=0, le=100)
    loadBalancePercent: Optional[int] = Field(default=None, ge=0, le=100)
    maxClientLeadTimeMinutes: int = Field(ge=1, le=1440)
    sharedSecret: Optional[str] = Field(default=None, max_length=256, exclude=True)

    @field_validator("sharedSecret", mode="before")
    @classmethod
    def normalize_shared_secret(cls, v: object) -> object:
        if v == "":
            return None
        return v

    @model_validator(mode="after")
    def enforce_mode_fields(self) -> "DhcpFailover":
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


class DhcpScopePayload(BaseModel):
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
        if v is None:
            return ""
        return v
    gateway: IPv4Address
    dnsServers: list[IPv4Address] = Field(default_factory=list, min_length=1)
    dnsDomain: str = Field(default="", max_length=256)
    exclusions: list[DhcpExclusion] = Field(default_factory=list)
    failover: Optional[DhcpFailover] = None

    @model_validator(mode="after")
    def no_duplicate_exclusions(self) -> "DhcpScopePayload":
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
    def no_overlapping_exclusions(self) -> "DhcpScopePayload":
        if len(self.exclusions) < 2:
            return self
        sorted_excl = sorted(
            self.exclusions,
            key=lambda x: (_ip_to_int(x.startAddress), _ip_to_int(x.endAddress)),
        )
        for i in range(len(sorted_excl) - 1):
            a = sorted_excl[i]
            b = sorted_excl[i + 1]
            if _ip_to_int(a.endAddress) >= _ip_to_int(b.startAddress):
                raise ValueError(
                    f"exclusions overlap: {a.startAddress}-{a.endAddress} "
                    f"overlaps with {b.startAddress}-{b.endAddress}"
                )
        return self

    @model_validator(mode="after")
    def end_range_gte_start_range(self) -> "DhcpScopePayload":
        if int(self.endRange) < int(self.startRange):
            raise ValueError(
                f"endRange {self.endRange} must be >= startRange {self.startRange}"
            )
        return self

    @model_validator(mode="after")
    def validate_subnet_consistency(self) -> "DhcpScopePayload":
        try:
            subnet = IPv4Network(f"{self.network}/{self.subnetMask}", strict=True)
        except ValueError as exc:
            raise ValueError(
                f"network {self.network} with subnetMask {self.subnetMask} "
                f"is not a valid subnet: {exc}"
            ) from exc

        for field, ip in [
            ("startRange", self.startRange),
            ("endRange", self.endRange),
            ("gateway", self.gateway),
        ]:
            if ip not in subnet:
                raise ValueError(f"{field} {ip} is not within subnet {subnet}")

        for i, excl in enumerate(self.exclusions):
            for attr in ("startAddress", "endAddress"):
                ip = getattr(excl, attr)
                if ip not in subnet:
                    raise ValueError(
                        f"exclusions[{i}].{attr} {ip} is not within subnet {subnet}"
                    )

        net_addr = subnet.network_address
        bcast_addr = subnet.broadcast_address
        for field, ip in [
            ("gateway", self.gateway),
            ("startRange", self.startRange),
            ("endRange", self.endRange),
        ]:
            if ip == net_addr:
                raise ValueError(
                    f"{field} {ip} must not be the network address {net_addr}"
                )
            if ip == bcast_addr:
                raise ValueError(
                    f"{field} {ip} must not be the broadcast address {bcast_addr}"
                )

        return self


# ---------------------------------------------------------------------------
# Values file loading and merging
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place — mirrors Helm -f merge semantics.

    - null in override replaces the key (how you remove an inherited map).
    - dict in override is merged recursively into the same dict in base.
    - Any other value in override replaces base.
    """
    for key, value in override.items():
        if value is None:
            base[key] = None
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _load_and_merge(*paths: str) -> dict:
    merged: dict = {}
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: values file not found: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            print(f"ERROR: YAML parse error in {path}:\n  {exc}", file=sys.stderr)
            sys.exit(1)
        _deep_merge(merged, data)
    return merged


# ---------------------------------------------------------------------------
# Field mapping: values.yaml structure → DhcpScopePayload kwargs
# ---------------------------------------------------------------------------

def _to_payload_kwargs(dv: dict) -> dict:
    """Map dhcp_values to DhcpScopePayload constructor kwargs.

    The values file uses a nested dns: {servers, domain} structure.
    The model uses flat dnsServers and dnsDomain fields.
    Everything else is 1-to-1.
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {Path(sys.argv[0]).name} values.yaml [override.yaml ...]")
        print()
        print("Validates dhcp_values from one or more Helm values files.")
        print("Multiple files are deep-merged left to right (last file wins),")
        print("matching the behaviour of 'helm install -f base.yaml -f override.yaml'.")
        sys.exit(1)

    merged = _load_and_merge(*sys.argv[1:])

    dv = merged.get("dhcp_values")
    if not isinstance(dv, dict):
        print("ERROR: 'dhcp_values' key is missing or not a mapping.", file=sys.stderr)
        sys.exit(1)

    kwargs = _to_payload_kwargs(dv)
    network = kwargs.get("network", "<unknown>")

    try:
        DhcpScopePayload(**kwargs)
    except ValidationError as exc:
        print(f"ERROR: dhcp_values validation failed for network '{network}':", file=sys.stderr)
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            print(f"  {field}: {error['msg']}", file=sys.stderr)
        sys.exit(1)

    print(f"OK  dhcp_values for network {network} passed all validation checks.")
    sys.exit(0)


if __name__ == "__main__":
    main()
