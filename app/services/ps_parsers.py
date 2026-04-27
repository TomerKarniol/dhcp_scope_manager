from __future__ import annotations
import logging
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.ps_executor import PowerShellError, is_not_found_error, run_ps
from app.utils.ip_utils import ip_to_int, parse_timespan_days, parse_timespan_minutes

logger = logging.getLogger(__name__)


def normalize_list(result) -> list:
    """PowerShell ConvertTo-Json returns an object (not array) for single results.
    Always normalize to a list.
    """
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    return result


def extract_option(options: list, option_id: int) -> str:
    """Extract the first value of a DHCP option by OptionId."""
    for opt in options:
        if opt.get("OptionId") == option_id:
            values = opt.get("Value", [])
            if values:
                return str(values[0])
    return ""


def extract_option_list(options: list, option_id: int) -> list[str]:
    """Extract all values of a DHCP option by OptionId as a list."""
    for opt in options:
        if opt.get("OptionId") == option_id:
            return [str(v) for v in opt.get("Value", [])]
    return []


def parse_failover(raw: dict) -> DhcpFailover:
    """Parse a PowerShell Get-DhcpServerv4Failover result into DhcpFailover."""
    return DhcpFailover(
        partnerServer=str(raw.get("PartnerServer", "")),
        relationshipName=str(raw.get("Name", "")),
        mode=raw.get("Mode", "HotStandby"),
        serverRole=raw.get("ServerRole", "Active"),
        reservePercent=int(raw.get("ReservePercent", 0)),
        loadBalancePercent=int(raw.get("LoadBalancePercent", 0)),
        maxClientLeadTimeMinutes=parse_timespan_minutes(
            str(raw.get("MaxClientLeadTime", "1:00:00"))
        ),
        sharedSecret=raw.get("SharedSecret") or None,
    )


async def assemble_scope_state(scope_id: str) -> DhcpScopePayload:
    """Query Windows DHCP via PowerShell and assemble the canonical DhcpScopePayload.

    This is the single source of truth for GET responses. The output MUST be
    byte-for-byte comparable to the PUT/POST body Crossplane sends.
    """
    # 1. Scope basic info
    scope = await run_ps(f"Get-DhcpServerv4Scope -ScopeId {scope_id}")

    # 2. Scope options (gateway, DNS, domain)
    options_raw = await run_ps(f"Get-DhcpServerv4OptionValue -ScopeId {scope_id}")
    options = normalize_list(options_raw)

    # 3. Exclusion ranges
    # A scope with no exclusions returns empty output (not an error).
    # Re-raise unexpected errors (permission denied, PS crash) — silently returning
    # no exclusions would cause the next reconciliation to delete them from the server.
    exclusions_raw = None
    try:
        exclusions_raw = await run_ps(f"Get-DhcpServerv4ExclusionRange -ScopeId {scope_id}")
    except PowerShellError as exc:
        if not is_not_found_error(exc.stderr):
            raise
        # Scope exists but has no exclusion ranges — treat as empty.

    exclusions_list = normalize_list(exclusions_raw)

    # 4. Failover (may not exist — that is normal)
    # Windows DHCP raises a "not found" error when the scope has no failover relationship.
    # Re-raise unexpected errors (permission denied, PS crash) — silently returning
    # null failover would cause the next reconciliation to remove the relationship.
    failover_obj: DhcpFailover | None = None
    try:
        failover_raw = await run_ps(f"Get-DhcpServerv4Failover -ScopeId {scope_id}")
        if failover_raw:
            failover_obj = parse_failover(
                failover_raw if isinstance(failover_raw, dict) else failover_raw[0]
            )
    except PowerShellError as exc:
        if not is_not_found_error(exc.stderr):
            raise
        # No failover relationship for this scope.

    # Parse lease duration: "8.00:00:00" → 8
    lease_days = parse_timespan_days(str(scope.get("LeaseDuration", "8.00:00:00")))

    # Build sorted exclusions
    exclusions = sorted(
        [
            DhcpExclusion(
                startAddress=str(e["StartRange"]),
                endAddress=str(e["EndRange"]),
            )
            for e in exclusions_list
        ],
        key=lambda x: (ip_to_int(x.startAddress), ip_to_int(x.endAddress)),
    )

    return DhcpScopePayload(
        scopeName=str(scope.get("Name") or ""),
        network=scope_id,
        subnetMask=str(scope.get("SubnetMask", "")),
        startRange=str(scope.get("StartRange", "")),
        endRange=str(scope.get("EndRange", "")),
        leaseDurationDays=lease_days,
        description=str(scope.get("Description") or ""),
        gateway=extract_option(options, 3),
        dnsServers=extract_option_list(options, 6),
        dnsDomain=extract_option(options, 15),
        exclusions=exclusions,
        failover=failover_obj,
    )
