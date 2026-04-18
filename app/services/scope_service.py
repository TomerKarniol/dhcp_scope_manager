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
# PS execution helpers  (encapsulate recurring error-handling patterns)
# ---------------------------------------------------------------------------

def _try_run_ps(cmd: str) -> dict | list | None:
    """Run a PS command; return None on not-found errors instead of raising.

    Intentionally defined here rather than imported from ps_executor.
    Tests patch 'scope_service.run_ps'; an imported function would close over
    ps_executor's binding of run_ps and bypass that patch.
    """
    try:
        return run_ps(cmd)
    except PowerShellError as exc:
        if is_not_found_error(exc.stderr):
            return None
        raise


def _try_run_ps_best_effort(cmd: str) -> dict | list | None:
    """Run a PS command; return None on ANY PowerShellError (best-effort / idempotent ops).

    Use when the absence of an object — for any reason — is acceptable and the
    caller should simply proceed rather than fail. Distinct from _try_run_ps which
    only suppresses not-found errors and propagates unexpected failures.
    """
    try:
        return run_ps(cmd)
    except PowerShellError:
        return None


def _run_ps_allow_existing(cmd: str) -> None:
    """Run a fire-and-forget PS command; silently accept 'already exists' errors.

    Used for idempotent create operations where the object may already be
    present from a previous (partial) reconciliation cycle.
    """
    try:
        run_ps(cmd, parse_json=False)
    except PowerShellError as exc:
        if not _is_already_exists_error(exc.stderr):
            raise


def _run_ps_warn_on_error(cmd: str, warning_prefix: str) -> None:
    """Run a best-effort PS command; log a warning on failure and continue.

    Used for cleanup operations where partial failure is acceptable
    (e.g. removing individual exclusion ranges during scope deletion).
    """
    try:
        run_ps(cmd, parse_json=False)
    except PowerShellError as exc:
        logger.warning("%s: %s", warning_prefix, exc.stderr)


def _try_assemble_scope(scope_id: str) -> Optional[DhcpScopePayload]:
    """Assemble scope state; return None if PowerShell cannot read the scope.

    Intentionally catches all PowerShellErrors (not just not-found) because this
    is used only inside delete_scope, where any assembly failure means we cannot
    safely determine what to clean up.  Returning None causes delete_scope to
    abort early, which is safe — Crossplane will retry the DELETE on the next cycle.
    Do NOT use this helper in read or update paths where data accuracy matters.
    """
    try:
        return assemble_scope_state(scope_id)
    except PowerShellError:
        return None


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
    return _try_run_ps(f"Get-DhcpServerv4Scope -ScopeId {scope_id}") is not None


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
        _run_ps_allow_existing(
            f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
            f"-StartRange {excl.startAddress} -EndRange {excl.endAddress}"
        )

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

    current = _try_assemble_scope(scope_id)
    if current is None:
        return  # scope disappeared between existence check and assembly

    if current.failover is not None:
        _remove_scope_from_failover(scope_id, current.failover.relationshipName)

    for excl in current.exclusions:
        _run_ps_warn_on_error(
            f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
            f"-StartRange {excl.startAddress} -EndRange {excl.endAddress}",
            f"Failed to remove exclusion {excl.startAddress}",
        )

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
    rel_raw = _try_run_ps_best_effort(f'Get-DhcpServerv4Failover -Name "{_ps_str(rel_name)}"')
    if rel_raw:
        rel = rel_raw if isinstance(rel_raw, dict) else rel_raw[0]
        if not rel.get("ScopeId"):
            run_ps(
                f'Remove-DhcpServerv4Failover -Name "{_ps_str(rel_name)}" -Force',
                parse_json=False,
            )


def _setup_failover(scope_id: str, failover: DhcpFailover) -> None:
    existing = _try_run_ps(f'Get-DhcpServerv4Failover -Name "{_ps_str(failover.relationshipName)}"')
    if existing:
        _run_ps_allow_existing(
            f'Add-DhcpServerv4FailoverScope -Name "{_ps_str(failover.relationshipName)}" '
            f"-ScopeId {scope_id}"
        )
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
