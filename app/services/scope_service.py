from __future__ import annotations
import logging
from typing import Optional

from app.models import DhcpFailover, DhcpScopePayload
from app.services.ps_executor import PowerShellError, is_not_found_error, run_ps
from app.services.ps_parsers import assemble_scope_state, normalize_list
from app.utils.decorators import handle_http_errors, log_call
from app.utils.ip_utils import ip_to_int

logger = logging.getLogger(__name__)


def _ps_str(value: str) -> str:
    return value.replace("`", "``").replace("$", "`$").replace('"', '`"')


def _is_already_exists_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(kw in lower for kw in ("already exists", "already been added", "already in use"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@log_call
def list_scopes() -> list[DhcpScopePayload]:
    raw = run_ps("Get-DhcpServerv4Scope")
    entries = normalize_list(raw)
    scope_ids: list[str] = sorted(
        (str(e["ScopeId"]) for e in entries if e.get("ScopeId")),
        key=ip_to_int,
    )
    return [assemble_scope_state(scope_id) for scope_id in scope_ids]


def scope_exists(scope_id: str) -> bool:
    try:
        run_ps(f"Get-DhcpServerv4Scope -ScopeId {scope_id}")
        return True
    except PowerShellError as e:
        if is_not_found_error(e.stderr):
            return False
        raise


@log_call
def create_scope(payload: DhcpScopePayload) -> DhcpScopePayload:
    scope_id = str(payload.network)

    if not scope_exists(scope_id):
        run_ps(
            f'Add-DhcpServerv4Scope '
            f'-Name "{_ps_str(payload.scopeName)}" '
            f'-StartRange {payload.startRange} '
            f'-EndRange {payload.endRange} '
            f'-SubnetMask {payload.subnetMask} '
            f'-State Active '
            f'-LeaseDuration (New-TimeSpan -Days {payload.leaseDurationDays}) '
            f'-Description "{_ps_str(payload.description)}"',
            parse_json=False,
        )
    else:
        logger.info("Scope %s already exists — skipping Add-DhcpServerv4Scope", scope_id)

    dns_str = ",".join(str(ip) for ip in payload.dnsServers)
    run_ps(
        f"Set-DhcpServerv4OptionValue -ScopeId {scope_id} "
        f"-Router {payload.gateway} "
        f"-DnsServer {dns_str} "
        f'-DnsDomain "{_ps_str(payload.dnsDomain)}"',
        parse_json=False,
    )

    for excl in payload.exclusions:
        try:
            run_ps(
                f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
                f"-StartRange {excl.startAddress} -EndRange {excl.endAddress}",
                parse_json=False,
            )
        except PowerShellError as e:
            if not _is_already_exists_error(e.stderr):
                raise

    if payload.failover is not None:
        _setup_failover(scope_id, payload.failover)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )

    return assemble_scope_state(scope_id)


@log_call
@handle_http_errors
def get_scope(scope_id: str) -> DhcpScopePayload:
    return assemble_scope_state(scope_id)


@log_call
@handle_http_errors
def update_scope(scope_id: str, desired: DhcpScopePayload) -> DhcpScopePayload:
    current = assemble_scope_state(scope_id)

    if (
        current.scopeName != desired.scopeName
        or current.leaseDurationDays != desired.leaseDurationDays
        or current.description != desired.description
        or current.startRange != desired.startRange
        or current.endRange != desired.endRange
    ):
        logger.info("Scope %s: updating params (name/lease/description/range)", scope_id)
        run_ps(
            f"Set-DhcpServerv4Scope -ScopeId {scope_id} "
            f'-Name "{_ps_str(desired.scopeName)}" '
            f"-LeaseDuration (New-TimeSpan -Days {desired.leaseDurationDays}) "
            f'-Description "{_ps_str(desired.description)}" '
            f"-StartRange {desired.startRange} "
            f"-EndRange {desired.endRange}",
            parse_json=False,
        )

    if (
        current.gateway != desired.gateway
        or current.dnsServers != desired.dnsServers
        or current.dnsDomain != desired.dnsDomain
    ):
        logger.info("Scope %s: updating options (gateway/dns/domain)", scope_id)
        dns_str = ",".join(str(ip) for ip in desired.dnsServers)
        run_ps(
            f"Set-DhcpServerv4OptionValue -ScopeId {scope_id} "
            f"-Router {desired.gateway} "
            f"-DnsServer {dns_str} "
            f'-DnsDomain "{_ps_str(desired.dnsDomain)}"',
            parse_json=False,
        )

    current_excl = {(e.startAddress, e.endAddress) for e in current.exclusions}
    desired_excl = {(e.startAddress, e.endAddress) for e in desired.exclusions}

    for start, end in current_excl - desired_excl:
        logger.info("Scope %s: removing exclusion %s-%s", scope_id, start, end)
        run_ps(
            f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
            f"-StartRange {start} -EndRange {end}",
            parse_json=False,
        )

    for start, end in desired_excl - current_excl:
        logger.info("Scope %s: adding exclusion %s-%s", scope_id, start, end)
        run_ps(
            f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
            f"-StartRange {start} -EndRange {end}",
            parse_json=False,
        )

    _handle_failover_diff(scope_id, current.failover, desired.failover)

    return assemble_scope_state(scope_id)


@log_call
def delete_scope(scope_id: str) -> None:
    if not scope_exists(scope_id):
        logger.info("Scope %s does not exist — nothing to delete", scope_id)
        return

    try:
        current = assemble_scope_state(scope_id)
    except PowerShellError:
        return

    if current.failover is not None:
        _remove_scope_from_failover(scope_id, current.failover.relationshipName)

    for excl in current.exclusions:
        try:
            run_ps(
                f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
                f"-StartRange {excl.startAddress} -EndRange {excl.endAddress}",
                parse_json=False,
            )
        except PowerShellError as e:
            logger.warning("Failed to remove exclusion %s: %s", excl.startAddress, e.stderr)

    run_ps(f"Remove-DhcpServerv4Scope -ScopeId {scope_id} -Force", parse_json=False)
    logger.info("Scope %s deleted", scope_id)


# ---------------------------------------------------------------------------
# Failover helpers
# ---------------------------------------------------------------------------

def _remove_scope_from_failover(scope_id: str, rel_name: str) -> None:
    run_ps(
        f'Remove-DhcpServerv4FailoverScope -Name "{_ps_str(rel_name)}" '
        f"-ScopeId {scope_id} -Force",
        parse_json=False,
    )
    try:
        rel_raw = run_ps(f'Get-DhcpServerv4Failover -Name "{_ps_str(rel_name)}"')
        if rel_raw:
            rel = rel_raw if isinstance(rel_raw, dict) else rel_raw[0]
            if not rel.get("ScopeId"):
                run_ps(
                    f'Remove-DhcpServerv4Failover -Name "{_ps_str(rel_name)}" -Force',
                    parse_json=False,
                )
    except PowerShellError:
        pass


def _setup_failover(scope_id: str, failover: DhcpFailover) -> None:
    existing = None
    try:
        existing = run_ps(f'Get-DhcpServerv4Failover -Name "{_ps_str(failover.relationshipName)}"')
    except PowerShellError:
        pass

    if existing:
        try:
            run_ps(
                f'Add-DhcpServerv4FailoverScope -Name "{_ps_str(failover.relationshipName)}" '
                f"-ScopeId {scope_id}",
                parse_json=False,
            )
        except PowerShellError as e:
            if not _is_already_exists_error(e.stderr):
                raise
    else:
        _create_failover_relationship(scope_id, failover)


def _create_failover_relationship(scope_id: str, failover: DhcpFailover) -> None:
    cmd = (
        f'Add-DhcpServerv4Failover '
        f'-Name "{_ps_str(failover.relationshipName)}" '
        f'-PartnerServer "{_ps_str(failover.partnerServer)}" '
        f'-ScopeId {scope_id} '
        f'-Mode {failover.mode} '
        f'-MaxClientLeadTime (New-TimeSpan -Minutes {failover.maxClientLeadTimeMinutes}) '
        f'-Force'
    )
    if failover.mode == "HotStandby":
        cmd += f" -ServerRole {failover.serverRole}"
        cmd += f" -ReservePercent {failover.reservePercent}"
    else:
        cmd += f" -LoadBalancePercent {failover.loadBalancePercent}"

    if failover.sharedSecret:
        cmd += f' -SharedSecret "{_ps_str(failover.sharedSecret)}"'

    run_ps(cmd, parse_json=False)


def _handle_failover_diff(
    scope_id: str,
    current: Optional[DhcpFailover],
    desired: Optional[DhcpFailover],
) -> None:
    if current is None and desired is None:
        return

    if current is None:
        _setup_failover(scope_id, desired)
        run_ps(f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force", parse_json=False)
        return

    if desired is None:
        _remove_scope_from_failover(scope_id, current.relationshipName)
        return

    if current.mode != desired.mode:
        logger.info(
            "Scope %s: failover mode changed %s→%s — removing '%s' and recreating",
            scope_id, current.mode, desired.mode, current.relationshipName,
        )
        _remove_scope_from_failover(scope_id, current.relationshipName)
        _setup_failover(scope_id, desired)
        run_ps(f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force", parse_json=False)
        return

    identity_changed = (
        current.relationshipName != desired.relationshipName
        or current.partnerServer != desired.partnerServer
        or (current.mode == "HotStandby" and current.serverRole != desired.serverRole)
    )
    if identity_changed:
        logger.info(
            "Scope %s: failover identity changed — removing '%s' and recreating",
            scope_id, current.relationshipName,
        )
        _remove_scope_from_failover(scope_id, current.relationshipName)
        _setup_failover(scope_id, desired)
        run_ps(f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force", parse_json=False)
        return

    if current.mode == "HotStandby":
        mutable_changed = (
            current.reservePercent != desired.reservePercent
            or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
            or current.sharedSecret != desired.sharedSecret
        )
    else:
        mutable_changed = (
            current.loadBalancePercent != desired.loadBalancePercent
            or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
            or current.sharedSecret != desired.sharedSecret
        )

    if mutable_changed:
        logger.info("Scope %s: updating failover params", scope_id)
        cmd = (
            f'Set-DhcpServerv4Failover -Name "{_ps_str(current.relationshipName)}" '
            f"-MaxClientLeadTime (New-TimeSpan -Minutes {desired.maxClientLeadTimeMinutes})"
        )
        if desired.mode == "HotStandby":
            cmd += f" -ReservePercent {desired.reservePercent}"
        else:
            cmd += f" -LoadBalancePercent {desired.loadBalancePercent}"
        if desired.sharedSecret is not None:
            cmd += f' -SharedSecret "{_ps_str(desired.sharedSecret)}"'
        elif current.sharedSecret is not None:
            cmd += ' -SharedSecret ""'
        run_ps(cmd, parse_json=False)
        run_ps(f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force", parse_json=False)
