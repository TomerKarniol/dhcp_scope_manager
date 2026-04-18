"""Tests for the PUT diff logic in scope_service.update_scope."""
from unittest.mock import patch
import pytest
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.ps_executor import PowerShellError


def _make_scope(**overrides):
    defaults = dict(
        scopeName="Cluster-A",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="desc",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")],
        failover=None,
    )
    defaults.update(overrides)
    return DhcpScopePayload(**defaults)


def _run_update(current_scope, desired_scope):
    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        # First call returns current, second call returns "fresh" state after update
        mock_assemble.side_effect = [current_scope, desired_scope]
        scope_service.update_scope(current_scope.network, desired_scope)
        return mock_ps.call_args_list


def test_no_op_when_identical():
    scope = _make_scope()
    calls = _run_update(scope, scope)
    assert calls == [], "No PowerShell calls expected when desired == current"


def test_scope_name_changed():
    current = _make_scope(scopeName="Old Name")
    desired = _make_scope(scopeName="New Name")
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


def test_lease_changed():
    current = _make_scope(leaseDurationDays=8)
    desired = _make_scope(leaseDurationDays=14)
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


def test_description_changed():
    current = _make_scope(description="old")
    desired = _make_scope(description="new")
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


def test_gateway_changed():
    current = _make_scope(gateway="10.20.30.1")
    desired = _make_scope(gateway="10.20.30.2")
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)


def test_dns_changed():
    current = _make_scope(dnsServers=["10.0.0.53"])
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"])
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)


def test_exclusion_added():
    current = _make_scope(exclusions=[])
    desired = _make_scope(exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")])
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Add-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)


def test_exclusion_removed():
    current = _make_scope(exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")])
    desired = _make_scope(exclusions=[])
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Remove-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)


def _make_failover(**overrides):
    defaults = dict(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        maxClientLeadTimeMinutes=60,
        sharedSecret=None,
    )
    defaults.update(overrides)
    return DhcpFailover(**defaults)


def test_failover_add_new_relationship():
    """current=None, desired=failover, relationship doesn't exist → Add-DhcpServerv4Failover"""
    current = _make_scope(failover=None)
    desired = _make_scope(failover=_make_failover())

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        # First run_ps in _setup_failover (Get-DhcpServerv4Failover) raises → new relationship
        mock_ps.side_effect = [
            PowerShellError("Get-DhcpServerv4Failover", "Not found", 1),  # relationship check
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Add-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


def test_failover_add_existing_relationship():
    """current=None, desired=failover, relationship exists → Add-DhcpServerv4FailoverScope"""
    current = _make_scope(failover=None)
    desired = _make_scope(failover=_make_failover())

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        # Get-DhcpServerv4Failover returns existing relationship
        mock_ps.side_effect = [
            {"Name": "mce1-failover", "ScopeId": "10.20.20.0"},  # existing relationship
            None,  # Add-DhcpServerv4FailoverScope
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Add-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)


def test_failover_remove():
    """current=failover, desired=None → Remove-DhcpServerv4FailoverScope"""
    current = _make_scope(failover=_make_failover())
    desired = _make_scope(failover=None)

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.side_effect = [
            None,  # Remove-DhcpServerv4FailoverScope
            PowerShellError("Get-DhcpServerv4Failover", "gone", 1),  # check remaining
        ]
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)


def test_failover_params_updated():
    """Failover params changed → Set-DhcpServerv4Failover + Replication"""
    current = _make_scope(failover=_make_failover(reservePercent=5))
    desired = _make_scope(failover=_make_failover(reservePercent=10))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


def test_failover_unchanged_no_calls():
    """Identical failover config → no failover cmdlets at all"""
    failover = _make_failover()
    current = _make_scope(failover=failover)
    desired = _make_scope(failover=_make_failover())  # same values, new object

    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    failover_cmds = [c for c in ps_commands if "Failover" in c]
    assert failover_cmds == []


def test_failover_relationship_name_change_triggers_recreate():
    """Changing relationshipName is an identity change — must remove + recreate, not Set."""
    current = _make_scope(failover=_make_failover(relationshipName="old-rel"))
    desired = _make_scope(failover=_make_failover(relationshipName="new-rel"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    # Must remove from old relationship
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    # Must NOT use Set-DhcpServerv4Failover (not a rename operation)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


def test_failover_partner_server_change_triggers_recreate():
    """Changing partnerServer is an identity change — must remove + recreate."""
    current = _make_scope(failover=_make_failover(partnerServer="dhcp01.lab.local"))
    desired = _make_scope(failover=_make_failover(partnerServer="dhcp02.lab.local"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


def test_failover_shared_secret_change_triggers_update():
    """Adding a sharedSecret is a mutable change — must use Set-DhcpServerv4Failover."""
    current = _make_scope(failover=_make_failover(sharedSecret=None))
    desired = _make_scope(failover=_make_failover(sharedSecret="new-secret"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    # SharedSecret must appear in the update command
    set_cmd = next(cmd for cmd in ps_commands if "Set-DhcpServerv4Failover" in cmd)
    assert "-SharedSecret" in set_cmd


def test_failover_shared_secret_not_logged_in_plain(caplog):
    """Secret values must not appear in log output."""
    import logging
    current = _make_scope(failover=_make_failover(sharedSecret=None))
    desired = _make_scope(failover=_make_failover(sharedSecret="super-secret-value"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
        caplog.at_level(logging.INFO),
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope(current.network, desired)

    # Secret value must NOT appear anywhere in captured log output
    assert "super-secret-value" not in caplog.text


def test_scope_exists_reraises_on_permission_error():
    """scope_exists must not return False on permission errors — it must propagate."""
    from app.services.scope_service import scope_exists
    from app.services.ps_executor import PowerShellError

    with patch(
        "app.services.scope_service.run_ps",
        side_effect=PowerShellError("Get-DhcpServerv4Scope", "Access is denied", 1),
    ):
        with pytest.raises(PowerShellError):
            scope_exists("10.20.30.0")


def test_scope_exists_returns_false_on_not_found():
    """scope_exists returns False for legitimate not-found errors."""
    from app.services.scope_service import scope_exists
    from app.services.ps_executor import PowerShellError

    with patch(
        "app.services.scope_service.run_ps",
        side_effect=PowerShellError("Get-DhcpServerv4Scope", "No DHCP scope found", 1),
    ):
        assert scope_exists("10.20.30.0") is False


# ---------------------------------------------------------------------------
# Failover mode-specific PS command correctness
# ---------------------------------------------------------------------------

def test_create_failover_loadbalance_excludes_server_role():
    """Add-DhcpServerv4Failover for LoadBalance must NOT include -ServerRole.

    The Windows DHCP cmdlet does not accept -ServerRole for LoadBalance mode.
    Passing it would either cause a cmdlet error or silently corrupt the relationship.
    """
    from app.services import scope_service

    desired = _make_scope(failover=_make_failover(
        mode="LoadBalance",
        serverRole=None,       # not set by caller — model normalises to Active
        loadBalancePercent=50,
        reservePercent=0,
    ))

    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [_make_scope(failover=None), desired]
        mock_ps.side_effect = [
            PowerShellError("Get-DhcpServerv4Failover", "Not found", 1),  # relationship check
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        scope_service.update_scope("10.20.30.0", desired)

    add_cmd = next(
        c.args[0] for c in mock_ps.call_args_list
        if c.args and "Add-DhcpServerv4Failover" in c.args[0]
    )
    assert "-ServerRole" not in add_cmd, (
        "Add-DhcpServerv4Failover for LoadBalance must not include -ServerRole"
    )
    assert "-LoadBalancePercent 50" in add_cmd


def test_create_failover_hotstandby_includes_server_role():
    """Add-DhcpServerv4Failover for HotStandby must include -ServerRole and -ReservePercent."""
    from app.services import scope_service

    desired = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Active", reservePercent=5,
    ))

    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [_make_scope(failover=None), desired]
        mock_ps.side_effect = [
            PowerShellError("Get-DhcpServerv4Failover", "Not found", 1),
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        scope_service.update_scope("10.20.30.0", desired)

    add_cmd = next(
        c.args[0] for c in mock_ps.call_args_list
        if c.args and "Add-DhcpServerv4Failover" in c.args[0]
    )
    assert "-ServerRole Active" in add_cmd
    assert "-ReservePercent 5" in add_cmd
    assert "-LoadBalancePercent" not in add_cmd


# ---------------------------------------------------------------------------
# Mode switch: always remove + recreate (never Set)
# ---------------------------------------------------------------------------

def test_failover_mode_switch_hotstandby_to_loadbalance_triggers_recreate():
    """Switching from HotStandby to LoadBalance must remove + recreate, not Set.

    Set-DhcpServerv4Failover cannot safely handle mode transitions: it does not
    accept -ServerRole, leaving role semantics undefined after the switch.
    """
    current = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Active", reservePercent=5,
    ))
    desired = _make_scope(failover=_make_failover(
        mode="LoadBalance",
        serverRole=None,       # model normalises to Active
        loadBalancePercent=50,
        reservePercent=0,
    ))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands), (
        "Mode switch must remove scope from old relationship first"
    )
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands), (
        "Mode switch must not use Set-DhcpServerv4Failover"
    )


def test_failover_mode_switch_loadbalance_to_hotstandby_triggers_recreate():
    """Switching from LoadBalance to HotStandby must remove + recreate, not Set.

    When both current and desired serverRole are 'Active' (LoadBalance normalises
    to Active, HotStandby-Active is explicitly Active), the old identity check
    would not detect a change.  The mode field must be compared explicitly.
    """
    current = _make_scope(failover=_make_failover(
        mode="LoadBalance",
        serverRole=None,       # model normalises to Active
        loadBalancePercent=50,
        reservePercent=0,
    ))
    desired = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Active", reservePercent=10,
    ))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands), (
        "Mode switch must remove scope from old relationship first"
    )
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands), (
        "Mode switch must not use Set-DhcpServerv4Failover"
    )


def test_failover_mode_switch_loadbalance_to_hotstandby_standby_role_triggers_recreate():
    """LoadBalance→HotStandby(Standby): serverRole also changes — must remove + recreate."""
    current = _make_scope(failover=_make_failover(
        mode="LoadBalance", serverRole=None, loadBalancePercent=50, reservePercent=0,
    ))
    desired = _make_scope(failover=_make_failover(
        mode="HotStandby", serverRole="Standby", reservePercent=5,
    ))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


# ---------------------------------------------------------------------------
# Same-mode mutable updates: only mode-relevant fields trigger Set
# ---------------------------------------------------------------------------

def _make_lb_failover(**overrides):
    """Minimal valid LoadBalance failover."""
    base = dict(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="LoadBalance",
        loadBalancePercent=50,
        maxClientLeadTimeMinutes=60,
        sharedSecret=None,
    )
    base.update(overrides)
    return DhcpFailover(**base)


def test_loadbalance_percent_change_triggers_set():
    """Changing loadBalancePercent within LoadBalance must use Set-DhcpServerv4Failover."""
    current = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))
    desired = _make_scope(failover=_make_lb_failover(loadBalancePercent=70))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    set_cmd = next(c for c in ps_commands if "Set-DhcpServerv4Failover" in c)
    assert "-LoadBalancePercent 70" in set_cmd
    assert "-ReservePercent" not in set_cmd
    assert "-ServerRole" not in set_cmd


def test_loadbalance_unchanged_no_calls():
    """Identical LoadBalance config (including normalized fields) must produce no cmdlets."""
    failover = _make_lb_failover(loadBalancePercent=50)
    current = _make_scope(failover=failover)
    desired = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))

    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    assert not any("Failover" in cmd for cmd in ps_commands), (
        "No failover cmdlets expected when LoadBalance config is unchanged"
    )


def test_hotstandby_role_change_triggers_recreate():
    """Changing serverRole within HotStandby is identity-level — must remove + recreate."""
    current = _make_scope(failover=_make_failover(mode="HotStandby", serverRole="Active"))
    desired = _make_scope(failover=_make_failover(mode="HotStandby", serverRole="Standby"))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope("10.20.30.0", desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)
    assert not any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)


def test_normalized_fields_do_not_trigger_spurious_update():
    """Normalized cross-mode fields must never cause a false-positive Set call.

    When mode is LoadBalance, reservePercent is always 0 and serverRole is always
    Active on both sides.  Comparing them would always be 0==0 / Active==Active,
    which is harmless — but this test proves no cmdlet fires at all.
    """
    current = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))
    desired = _make_scope(failover=_make_lb_failover(loadBalancePercent=50))

    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    assert ps_commands == [], (
        "No PowerShell calls expected — identical LoadBalance config with normalized fields"
    )
