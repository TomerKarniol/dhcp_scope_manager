from __future__ import annotations
from ipaddress import IPv4Address, IPv4Network
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.ip_utils import ip_to_int

from app.models.exclusion import DhcpExclusion
from app.models.failover import DhcpFailover


class DhcpScopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Field ordering is CRITICAL — Crossplane compares GET response to PUT body byte-for-byte.
    # Do NOT reorder these fields.
    scopeName: str = Field(
        min_length=1,
        max_length=256,
        description="Human-readable display name for the scope",
        examples=["Cluster-A Management"],
    )

    @field_validator("scopeName")
    @classmethod
    def scope_name_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scopeName must not be blank or whitespace-only")
        return v
    network: IPv4Address = Field(
        description="Network address — also the DHCP scope ID used in all PowerShell cmdlets",
        examples=["10.20.30.0"],
    )
    subnetMask: IPv4Address = Field(
        description="Subnet mask",
        examples=["255.255.255.0"],
    )
    startRange: IPv4Address = Field(
        description="First IP address in the DHCP distribution range",
        examples=["10.20.30.100"],
    )
    endRange: IPv4Address = Field(
        description="Last IP address in the DHCP distribution range",
        examples=["10.20.30.200"],
    )
    leaseDurationDays: int = Field(
        ge=1,
        le=3650,
        description="Lease duration in days (1–3650)",
        examples=[8],
    )
    description: str = Field(
        default="",
        max_length=1024,
        description="Optional scope description",
    )

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v: object) -> object:
        """Normalize null/None to '' — prevents Crossplane drift when description is unset."""
        if v is None:
            return ""
        return v
    gateway: IPv4Address = Field(
        description="Default gateway sent to clients (DHCP option 3)",
        examples=["10.20.30.1"],
    )
    dnsServers: list[IPv4Address] = Field(
        default_factory=list,
        description="Ordered list of DNS server IPs sent to clients (DHCP option 6)",
        examples=[["10.0.0.53", "10.0.0.54"]],
    )
    dnsDomain: str = Field(
        default="",
        max_length=256,
        description="DNS domain suffix sent to clients (DHCP option 15)",
        examples=["lab.local"],
    )

    @field_validator("dnsDomain", mode="before")
    @classmethod
    def normalize_dns_domain(cls, v: object) -> object:
        """Normalize null/None to '' — consistent with description field behavior."""
        if v is None:
            return ""
        return v

    exclusions: list[DhcpExclusion] = Field(
        default_factory=list,
        description="IP ranges excluded from distribution, sorted by startAddress",
    )
    failover: Optional[DhcpFailover] = Field(
        default=None,
        description="Failover configuration. null = no failover configured.",
    )

    @model_validator(mode="after")
    def sort_exclusions(self) -> "DhcpScopePayload":
        """Normalize exclusion list to ascending IP order — ensures GET/PUT parity."""
        if len(self.exclusions) > 1:
            self.exclusions = sorted(
                self.exclusions,
                key=lambda x: (ip_to_int(x.startAddress), ip_to_int(x.endAddress)),
            )
        return self

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
            key=lambda x: (ip_to_int(x.startAddress), ip_to_int(x.endAddress)),
        )
        for i in range(len(sorted_excl) - 1):
            a = sorted_excl[i]
            b = sorted_excl[i + 1]
            if ip_to_int(a.endAddress) >= ip_to_int(b.startAddress):
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
        """Validate that network/subnetMask is a valid subnet and all IPs fall within it."""
        # strict=True: raises if network has host bits set, or mask is non-contiguous
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

        # Reject network and broadcast addresses in dynamic-assignment and routing fields.
        # These addresses are reserved: assigning them to gateway/range causes DHCP failure.
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
