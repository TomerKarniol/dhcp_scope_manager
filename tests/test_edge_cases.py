"""Edge-case and failure-mode tests.

Covers bugs found during the aggressive audit:
  1. ps_executor: json.JSONDecodeError → PowerShellError
  2. dhcp_service: TimeoutExpired in _check_powershell_binary / _check_dhcp_cmdlets → DhcpEnvironmentError
  3. scope_service.update_scope: startRange/endRange included in diff
  4. scope_service._remove_scope_from_failover: handles list return from Get-DhcpServerv4Failover
  5. ps_parsers.assemble_scope_state: sort exclusions by (startAddress, endAddress)
  6. models.DhcpScopePayload: whitespace-only scopeName rejected
  7. models.DhcpScopePayload: duplicate exclusions rejected
"""
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.dhcp_service import (
    DhcpEnvironmentError,
    DhcpEnvReason,
    _check_dhcp_cmdlets,
    _check_powershell_binary,
    _reset_validation_cache,
)
from app.services.ps_executor import PowerShellError, run_ps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        dnsServers=["10.0.0.53"],
        dnsDomain="lab.local",
        exclusions=[],
        failover=None,
    )
    defaults.update(overrides)
    return DhcpScopePayload(**defaults)


# ---------------------------------------------------------------------------
# 1. ps_executor: non-JSON stdout → PowerShellError, not json.JSONDecodeError
# ---------------------------------------------------------------------------

class TestPsExecutorJsonError:
    def setup_method(self):
        _reset_validation_cache()

    def test_non_json_stdout_raises_powershell_error(self):
        """PS command succeeds (rc=0) but stdout is not JSON → PowerShellError, not ValueError."""
        mock_result = MagicMock(returncode=0, stdout="This is plain text\n", stderr="")
        with patch("app.services.dhcp_service.validate_dhcp_environment"), \
             patch("subprocess.run", return_value=mock_result):
            with pytest.raises(PowerShellError) as exc_info:
                run_ps("Get-DhcpServerv4Scope -ScopeId 10.20.30.0")
        assert "non-JSON" in str(exc_info.value) or "JSONDecodeError" in str(exc_info.value) or "PowerShell" in str(exc_info.value)

    def test_non_json_error_preserves_command(self):
        """PowerShellError from JSON parse should report rc=0 (command succeeded but output was bad)."""
        mock_result = MagicMock(returncode=0, stdout="WARNING: something\n", stderr="")
        with patch("app.services.dhcp_service.validate_dhcp_environment"), \
             patch("subprocess.run", return_value=mock_result):
            with pytest.raises(PowerShellError) as exc_info:
                run_ps("Get-DhcpServerv4Scope")
        assert exc_info.value.returncode == 0

    def test_empty_stdout_returns_none(self):
        """Empty stdout must return None, not raise."""
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.services.dhcp_service.validate_dhcp_environment"), \
             patch("subprocess.run", return_value=mock_result):
            result = run_ps("Get-Something")
        assert result is None

    def test_valid_json_parses_correctly(self):
        """Well-formed JSON stdout must be returned as parsed object."""
        mock_result = MagicMock(returncode=0, stdout='{"Name": "Test", "ScopeId": "10.20.30.0"}\n', stderr="")
        with patch("app.services.dhcp_service.validate_dhcp_environment"), \
             patch("subprocess.run", return_value=mock_result):
            result = run_ps("Get-DhcpServerv4Scope -ScopeId 10.20.30.0")
        assert result == {"Name": "Test", "ScopeId": "10.20.30.0"}


# ---------------------------------------------------------------------------
# 2. dhcp_service: TimeoutExpired → DhcpEnvironmentError, not unhandled exception
# ---------------------------------------------------------------------------

class TestDhcpEnvTimeout:
    def setup_method(self):
        _reset_validation_cache()

    def test_powershell_binary_check_timeout_raises_env_error(self):
        """TimeoutExpired during _check_powershell_binary → DhcpEnvironmentError."""
        with patch("shutil.which", return_value="C:\\powershell.exe"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=15)):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _check_powershell_binary()
        assert exc_info.value.reason == DhcpEnvReason.POWERSHELL_EXEC_FAILED
        assert "timed out" in exc_info.value.detail.lower()

    def test_dhcp_cmdlets_check_timeout_raises_env_error(self):
        """TimeoutExpired during _check_dhcp_cmdlets → DhcpEnvironmentError."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=15)):
            with pytest.raises(DhcpEnvironmentError) as exc_info:
                _check_dhcp_cmdlets()
        assert exc_info.value.reason == DhcpEnvReason.POWERSHELL_EXEC_FAILED
        assert "timed out" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# 3. scope_service.update_scope: startRange/endRange changes are not silently dropped
# ---------------------------------------------------------------------------

class TestUpdateScopeRangeDiff:
    def _run_update(self, current_scope, desired_scope):
        from app.services import scope_service
        with (
            patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
            patch("app.services.scope_service.run_ps") as mock_ps,
        ):
            mock_assemble.side_effect = [current_scope, desired_scope]
            scope_service.update_scope(str(current_scope.network), desired_scope)
            return [c.args[0] for c in mock_ps.call_args_list if c.args]

    def test_start_range_changed_triggers_set_scope(self):
        """Changing startRange must trigger Set-DhcpServerv4Scope."""
        current = _make_scope(startRange="10.20.30.100", endRange="10.20.30.200")
        desired = _make_scope(startRange="10.20.30.110", endRange="10.20.30.200")
        cmds = self._run_update(current, desired)
        assert any("Set-DhcpServerv4Scope" in c for c in cmds), (
            "Set-DhcpServerv4Scope must be called when startRange changes"
        )

    def test_end_range_changed_triggers_set_scope(self):
        """Changing endRange must trigger Set-DhcpServerv4Scope."""
        current = _make_scope(startRange="10.20.30.100", endRange="10.20.30.200")
        desired = _make_scope(startRange="10.20.30.100", endRange="10.20.30.210")
        cmds = self._run_update(current, desired)
        assert any("Set-DhcpServerv4Scope" in c for c in cmds), (
            "Set-DhcpServerv4Scope must be called when endRange changes"
        )

    def test_set_scope_includes_start_and_end_range(self):
        """Set-DhcpServerv4Scope command must include -StartRange and -EndRange parameters."""
        current = _make_scope(startRange="10.20.30.100", endRange="10.20.30.200")
        desired = _make_scope(startRange="10.20.30.110", endRange="10.20.30.190")
        cmds = self._run_update(current, desired)
        set_scope_cmd = next(c for c in cmds if "Set-DhcpServerv4Scope" in c)
        assert "-StartRange" in set_scope_cmd
        assert "-EndRange" in set_scope_cmd
        assert "10.20.30.110" in set_scope_cmd
        assert "10.20.30.190" in set_scope_cmd

    def test_no_change_in_ranges_skips_set_scope(self):
        """Identical startRange/endRange must NOT trigger Set-DhcpServerv4Scope (if other params equal)."""
        scope = _make_scope()
        cmds = self._run_update(scope, scope)
        assert not any("Set-DhcpServerv4Scope" in c for c in cmds), (
            "Set-DhcpServerv4Scope must not be called when nothing changed"
        )


# ---------------------------------------------------------------------------
# 4. _remove_scope_from_failover: list return from Get-DhcpServerv4Failover
# ---------------------------------------------------------------------------

class TestRemoveScopeFromFailoverListReturn:
    def test_list_return_does_not_raise_attribute_error(self):
        """Get-DhcpServerv4Failover returns list → must not AttributeError on .get()."""
        from app.services.scope_service import _remove_scope_from_failover

        list_return = [{"Name": "rel1", "ScopeId": None}]  # list, not dict
        ps_responses = [
            None,               # Remove-DhcpServerv4FailoverScope succeeds
            list_return,        # Get-DhcpServerv4Failover returns list
            None,               # Remove-DhcpServerv4Failover succeeds (ScopeId is None)
        ]
        with patch("app.services.scope_service.run_ps", side_effect=ps_responses):
            _remove_scope_from_failover("10.20.30.0", "rel1")  # must not raise

    def test_list_return_with_remaining_scope_does_not_delete_relationship(self):
        """If Get-DhcpServerv4Failover returns list and ScopeId is set, don't delete relationship."""
        from app.services.scope_service import _remove_scope_from_failover

        list_return = [{"Name": "rel1", "ScopeId": "10.20.40.0"}]  # still has a scope
        ps_responses = [
            None,           # Remove-DhcpServerv4FailoverScope succeeds
            list_return,    # Get-DhcpServerv4Failover returns list with remaining scope
        ]
        with patch("app.services.scope_service.run_ps", side_effect=ps_responses) as mock_ps:
            _remove_scope_from_failover("10.20.30.0", "rel1")
        # Remove-DhcpServerv4Failover must NOT have been called
        all_cmds = [c.args[0] for c in mock_ps.call_args_list if c.args]
        assert not any("Remove-DhcpServerv4Failover " in c for c in all_cmds), (
            "Relationship must not be deleted when other scopes remain"
        )

    def test_dict_return_still_works(self):
        """Original dict return path still works correctly."""
        from app.services.scope_service import _remove_scope_from_failover

        dict_return = {"Name": "rel1", "ScopeId": None}
        ps_responses = [
            None,           # Remove-DhcpServerv4FailoverScope
            dict_return,    # Get-DhcpServerv4Failover → dict (single result)
            None,           # Remove-DhcpServerv4Failover
        ]
        with patch("app.services.scope_service.run_ps", side_effect=ps_responses):
            _remove_scope_from_failover("10.20.30.0", "rel1")  # must not raise


# ---------------------------------------------------------------------------
# 5. ps_parsers: exclusion sort stability by (startAddress, endAddress)
# ---------------------------------------------------------------------------

class TestExclusionSortDeterminism:
    def test_exclusions_sorted_by_start_ascending(self):
        """PS may return exclusions in any order; assemble_scope_state must sort them ascending."""
        from app.services.ps_parsers import assemble_scope_state
        from app.services.ps_executor import PowerShellError as PSError

        scope_data = {
            "Name": "Test", "SubnetMask": "255.255.255.0",
            "StartRange": "10.20.30.100", "EndRange": "10.20.30.200",
            "LeaseDuration": "8.00:00:00", "Description": "",
        }
        options_data = [
            {"OptionId": 3, "Value": ["10.20.30.1"]},
            {"OptionId": 6, "Value": ["10.0.0.53"]},
            {"OptionId": 15, "Value": ["lab.local"]},
        ]
        # PS returns them in reverse IP order
        excl_data = [
            {"StartRange": "10.20.30.51", "EndRange": "10.20.30.60"},  # higher IP first
            {"StartRange": "10.20.30.1", "EndRange": "10.20.30.10"},   # lower IP second
        ]

        with patch("app.services.ps_parsers.run_ps") as mock_ps:
            mock_ps.side_effect = [scope_data, options_data, excl_data, PSError("failover", "not found", 1)]
            result = assemble_scope_state("10.20.30.0")

        assert len(result.exclusions) == 2
        assert str(result.exclusions[0].startAddress) == "10.20.30.1"
        assert str(result.exclusions[1].startAddress) == "10.20.30.51"

    def test_exclusions_sorted_by_start_primarily(self):
        """Primary sort key is startAddress."""
        from app.services.ps_parsers import assemble_scope_state
        from app.services.ps_executor import PowerShellError as PSError

        scope_data = {
            "Name": "Test", "SubnetMask": "255.255.255.0",
            "StartRange": "10.20.30.100", "EndRange": "10.20.30.200",
            "LeaseDuration": "8.00:00:00", "Description": "",
        }
        options_data = [
            {"OptionId": 3, "Value": ["10.20.30.1"]},
            {"OptionId": 6, "Value": ["10.0.0.53"]},
            {"OptionId": 15, "Value": ["lab.local"]},
        ]
        excl_data = [
            {"StartRange": "10.20.30.50", "EndRange": "10.20.30.60"},
            {"StartRange": "10.20.30.10", "EndRange": "10.20.30.20"},
        ]

        with patch("app.services.ps_parsers.run_ps") as mock_ps:
            mock_ps.side_effect = [scope_data, options_data, excl_data, PSError("failover", "not found", 1)]
            result = assemble_scope_state("10.20.30.0")

        assert str(result.exclusions[0].startAddress) == "10.20.30.10"
        assert str(result.exclusions[1].startAddress) == "10.20.30.50"


# ---------------------------------------------------------------------------
# 6. models: whitespace-only scopeName rejected
# ---------------------------------------------------------------------------

class TestScopeNameValidation:
    def test_whitespace_only_scope_name_rejected(self):
        """scopeName of only spaces must be rejected with a clear validation error."""
        with pytest.raises(ValidationError) as exc_info:
            _make_scope(scopeName="   ")
        assert "blank" in str(exc_info.value).lower() or "whitespace" in str(exc_info.value).lower()

    def test_tab_only_scope_name_rejected(self):
        with pytest.raises(ValidationError):
            _make_scope(scopeName="\t\t")

    def test_newline_only_scope_name_rejected(self):
        with pytest.raises(ValidationError):
            _make_scope(scopeName="\n")

    def test_empty_scope_name_rejected(self):
        """Empty scopeName rejected by min_length=1."""
        with pytest.raises(ValidationError):
            _make_scope(scopeName="")

    def test_scope_name_with_leading_trailing_spaces_accepted(self):
        """A name with content and surrounding spaces is valid (not whitespace-only)."""
        scope = _make_scope(scopeName="  valid name  ")
        assert scope.scopeName == "  valid name  "


# ---------------------------------------------------------------------------
# 7. models: duplicate exclusion ranges rejected
# ---------------------------------------------------------------------------

class TestDuplicateExclusionValidation:
    def test_duplicate_exclusion_rejected(self):
        """Identical exclusion range appearing twice must be rejected."""
        excl = DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10")
        with pytest.raises(ValidationError) as exc_info:
            _make_scope(exclusions=[excl, excl])
        assert "duplicate" in str(exc_info.value).lower()

    def test_two_identical_exclusion_ranges_rejected(self):
        """Two separately constructed but equal exclusions must be rejected."""
        with pytest.raises(ValidationError):
            _make_scope(exclusions=[
                DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10"),
                DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10"),
            ])

    def test_overlapping_exclusions_rejected(self):
        """Overlapping exclusion ranges must be rejected (even if not identical)."""
        with pytest.raises(ValidationError) as exc_info:
            _make_scope(exclusions=[
                DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.20"),
                DhcpExclusion(startAddress="10.20.30.10", endAddress="10.20.30.30"),
            ])
        assert "overlap" in str(exc_info.value).lower()

    def test_single_exclusion_accepted(self):
        scope = _make_scope(exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10"),
        ])
        assert len(scope.exclusions) == 1

    def test_no_exclusions_accepted(self):
        scope = _make_scope(exclusions=[])
        assert scope.exclusions == []


# ---------------------------------------------------------------------------
# 8. models: exclusion order normalized to ascending IP
# ---------------------------------------------------------------------------

class TestExclusionOrderNormalization:
    def test_unsorted_exclusions_normalized_to_ascending_order(self):
        """Exclusions provided in descending IP order must be stored in ascending order."""
        scope = _make_scope(exclusions=[
            DhcpExclusion(startAddress="10.20.30.51", endAddress="10.20.30.60"),
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10"),
        ])
        assert str(scope.exclusions[0].startAddress) == "10.20.30.1"
        assert str(scope.exclusions[1].startAddress) == "10.20.30.51"

    def test_already_sorted_exclusions_unchanged(self):
        """Exclusions already in ascending order must remain unchanged."""
        scope = _make_scope(exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.10"),
            DhcpExclusion(startAddress="10.20.30.51", endAddress="10.20.30.60"),
        ])
        assert str(scope.exclusions[0].startAddress) == "10.20.30.1"
        assert str(scope.exclusions[1].startAddress) == "10.20.30.51"


# ---------------------------------------------------------------------------
# 9. models: extra fields rejected
# ---------------------------------------------------------------------------

class TestExtraFieldsRejected:
    def test_scope_payload_extra_field_rejected(self):
        """Unknown fields on DhcpScopePayload must be rejected."""
        with pytest.raises(ValidationError):
            _make_scope(unknownField="unexpected")

    def test_failover_extra_field_rejected(self):
        """Unknown fields on DhcpFailover must be rejected."""
        with pytest.raises(ValidationError):
            DhcpFailover(
                partnerServer="dhcp02.lab.local",
                relationshipName="rel1",
                mode="HotStandby",
                serverRole="Active",
                maxClientLeadTimeMinutes=60,
                unknownField="unexpected",
            )

    def test_exclusion_extra_field_rejected(self):
        """Unknown fields on DhcpExclusion must be rejected."""
        with pytest.raises(ValidationError):
            DhcpExclusion(
                startAddress="10.20.30.1",
                endAddress="10.20.30.10",
                unknownField="unexpected",
            )
