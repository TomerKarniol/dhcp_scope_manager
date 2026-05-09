from __future__ import annotations
from ipaddress import AddressValueError, IPv4Address
import logging
from typing import Any

from app.errors import DhcpConflictError, InvalidScopeIdError
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.ps_executor import run_ps
from app.utils.decorators import log_call
from app.utils.ip_utils import ip_to_int, parse_timespan_days, parse_timespan_minutes
from app.utils.powershell import ps_single_quote

logger = logging.getLogger(__name__)


def normalize_list(result) -> list:
    """Normalize PowerShell JSON None/scalar/object/list output into a list."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, tuple):
        return list(result)
    return [result]


def _validated_scope_id(scope_id: str) -> str:
    scope_text = str(scope_id)
    try:
        return str(IPv4Address(scope_text))
    except (AddressValueError, ValueError):
        raise InvalidScopeIdError(scope_text)


def build_get_scope_state_script(scope_id: str) -> str:
    """Build a single PowerShell script that emits all scope state as JSON."""
    scope_literal = ps_single_quote(_validated_scope_id(scope_id))
    return f"""
$ScopeId = {scope_literal}

function Test-DhcpNoExclusions($ErrorRecord) {{
    $message = [string]$ErrorRecord.Exception.Message
    return (
        $message -match '(?i)exclusion' -and
        $message -match '(?i)(not found|cannot find|does not exist|no .*exclusion)'
    )
}}

function Test-DhcpNoFailover($ErrorRecord) {{
    $message = [string]$ErrorRecord.Exception.Message
    return (
        $message -match '(?i)(failover|relationship)' -and
        $message -match '(?i)(not found|cannot find|does not exist|not configured|not associated|no .*failover)'
    )
}}

$scope = Get-DhcpServerv4Scope -ScopeId $ScopeId -ErrorAction Stop
$options = @(Get-DhcpServerv4OptionValue -ScopeId $ScopeId -ErrorAction Stop)

try {{
    $exclusions = @(Get-DhcpServerv4ExclusionRange -ScopeId $ScopeId -ErrorAction Stop)
}} catch {{
    if (Test-DhcpNoExclusions $_) {{
        $exclusions = @()
    }} else {{
        throw
    }}
}}

try {{
    $failover = Get-DhcpServerv4Failover -ScopeId $ScopeId -ErrorAction Stop
}} catch {{
    if (Test-DhcpNoFailover $_) {{
        $failover = $null
    }} else {{
        throw
    }}
}}

$result = [PSCustomObject]@{{
    scope = $scope
    options = $options
    exclusions = $exclusions
    failover = $failover
}}

$result | ConvertTo-Json -Depth 10 -Compress
""".strip()


def extract_option(options: list, option_id: int) -> str:
    """Extract the first value of a DHCP option by OptionId."""
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if opt.get("OptionId") == option_id:
            values = normalize_list(opt.get("Value"))
            if values:
                return str(values[0])
    return ""


def extract_option_list(options: list, option_id: int) -> list[str]:
    """Extract all values of a DHCP option by OptionId as a list."""
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if opt.get("OptionId") == option_id:
            return [str(v) for v in normalize_list(opt.get("Value"))]
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
    )


def normalize_get_scope_state(raw: Any) -> dict[str, Any]:
    """Normalize the combined JSON object emitted by build_get_scope_state_script."""
    if not isinstance(raw, dict):
        raise ValueError("Expected DHCP scope state JSON object")

    return {
        "scope": raw.get("scope") or {},
        "options": normalize_list(raw.get("options")),
        "exclusions": normalize_list(raw.get("exclusions")),
        "failover": raw.get("failover"),
    }


def build_payload_from_scope_state(scope_id: str, state: dict[str, Any]) -> DhcpScopePayload:
    scope_id = _validated_scope_id(scope_id)
    scope = state["scope"]
    options = state["options"]
    exclusions_list = state["exclusions"]

    failover_raw = state["failover"]
    failover_obj: DhcpFailover | None = None
    if failover_raw:
        failover_entries = normalize_list(failover_raw)
        if failover_entries:
            failover_obj = parse_failover(failover_entries[0])

    # Parse lease duration: "8.00:00:00" → 8
    lease_days = parse_timespan_days(str(scope.get("LeaseDuration", "8.00:00:00")))
    dns_servers = extract_option_list(options, 6)
    if not dns_servers:
        raise DhcpConflictError(
            f"Observed DHCP scope {scope_id} is missing required DNS servers"
        )

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
        dnsServers=dns_servers,
        dnsDomain=extract_option(options, 15),
        exclusions=exclusions,
        failover=failover_obj,
    )


@log_call
async def assemble_scope_state(scope_id: str) -> DhcpScopePayload:
    """Query Windows DHCP once and assemble the canonical DhcpScopePayload.

    This is the single source of truth for GET responses. The output MUST be
    byte-for-byte comparable to the PUT/POST body Crossplane sends.
    """
    script = build_get_scope_state_script(scope_id)
    raw = await run_ps(
        script,
        append_error_action=False,
        append_convert_to_json=False,
        scope_id=scope_id,
        operation="get_scope_state",
    )
    state = normalize_get_scope_state(raw)
    return build_payload_from_scope_state(scope_id, state)
