from unittest.mock import patch
import pytest
from app.services.ps_executor import PowerShellError
from app.services.ps_parsers import (
    normalize_list,
    extract_option,
    extract_option_list,
    parse_failover,
    assemble_scope_state,
)
from app.utils.ip_utils import ip_to_int, parse_timespan_days, parse_timespan_minutes


def test_parse_timespan_days_full():
    assert parse_timespan_days("8.00:00:00") == 8


def test_parse_timespan_days_one_day():
    assert parse_timespan_days("1.00:00:00") == 1


def test_parse_timespan_days_no_dot():
    # No days component → 0
    assert parse_timespan_days("8:00:00") == 0


def test_parse_timespan_minutes_one_hour():
    assert parse_timespan_minutes("1:00:00") == 60


def test_parse_timespan_minutes_half_hour():
    assert parse_timespan_minutes("0:30:00") == 30


def test_parse_timespan_minutes_ninety():
    assert parse_timespan_minutes("1:30:00") == 90


def test_parse_timespan_minutes_day_format_one_day():
    """PowerShell emits d.HH:MM:SS for maxClientLeadTime >= 24h.
    1.00:00:00 = 1 day = 1440 minutes — previously crashed with ValueError."""
    assert parse_timespan_minutes("1.00:00:00") == 1440


def test_parse_timespan_minutes_day_format_half_day():
    """0 days 12 hours — verifies day + time combination."""
    assert parse_timespan_minutes("0.12:00:00") == 720


def test_parse_timespan_minutes_day_format_one_day_thirty():
    """1 day 0 hours 30 minutes = 1470 minutes."""
    assert parse_timespan_minutes("1.00:30:00") == 1470


def test_ip_to_int_ordering():
    assert ip_to_int("10.20.30.0") < ip_to_int("10.20.40.0")
    assert ip_to_int("10.20.30.1") < ip_to_int("10.20.30.2")
    assert ip_to_int("10.0.0.0") < ip_to_int("11.0.0.0")


def test_normalize_list_dict():
    d = {"key": "val"}
    assert normalize_list(d) == [d]


def test_normalize_list_none():
    assert normalize_list(None) == []


def test_normalize_list_list():
    lst = [{"a": 1}, {"b": 2}]
    assert normalize_list(lst) == lst


def test_extract_option_router(mock_ps_options_raw):
    result = extract_option(mock_ps_options_raw, 3)
    assert result == "10.20.30.1"


def test_extract_option_dns_list(mock_ps_options_raw):
    result = extract_option_list(mock_ps_options_raw, 6)
    assert result == ["10.0.0.53", "10.0.0.54"]


def test_extract_option_domain(mock_ps_options_raw):
    result = extract_option(mock_ps_options_raw, 15)
    assert result == "lab.local"


def test_extract_option_missing(mock_ps_options_raw):
    assert extract_option(mock_ps_options_raw, 999) == ""
    assert extract_option_list(mock_ps_options_raw, 999) == []


@pytest.mark.asyncio
async def test_assemble_scope_state(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw, mock_ps_failover_raw
):
    def fake_run_ps(cmd, parse_json=True):
        if "Get-DhcpServerv4Scope" in cmd:
            return mock_ps_scope_raw
        if "Get-DhcpServerv4OptionValue" in cmd:
            return mock_ps_options_raw
        if "Get-DhcpServerv4ExclusionRange" in cmd:
            return mock_ps_exclusions_raw
        if "Get-DhcpServerv4Failover" in cmd:
            return mock_ps_failover_raw
        return None

    with patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        result = await assemble_scope_state("10.20.30.0")

    assert result.scopeName == "Cluster-A Management"
    assert str(result.network) == "10.20.30.0"
    assert str(result.subnetMask) == "255.255.255.0"
    assert result.leaseDurationDays == 8
    assert str(result.gateway) == "10.20.30.1"
    assert [str(ip) for ip in result.dnsServers] == ["10.0.0.53", "10.0.0.54"]
    assert result.dnsDomain == "lab.local"
    assert len(result.exclusions) == 1
    assert str(result.exclusions[0].startAddress) == "10.20.30.1"
    assert result.failover is not None
    assert result.failover.relationshipName == "mce1-failover"
    assert result.failover.maxClientLeadTimeMinutes == 60


@pytest.mark.asyncio
async def test_assemble_scope_no_failover(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    def fake_run_ps(cmd, parse_json=True):
        if "Get-DhcpServerv4Scope" in cmd:
            return mock_ps_scope_raw
        if "Get-DhcpServerv4OptionValue" in cmd:
            return mock_ps_options_raw
        if "Get-DhcpServerv4ExclusionRange" in cmd:
            return mock_ps_exclusions_raw
        if "Get-DhcpServerv4Failover" in cmd:
            raise PowerShellError(cmd, "Cannot find failover relationship for scope", 1)
        return None

    with patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        result = await assemble_scope_state("10.20.30.0")

    assert result.failover is None


@pytest.mark.asyncio
async def test_assemble_scope_empty_exclusions(mock_ps_scope_raw, mock_ps_options_raw):
    def fake_run_ps(cmd, parse_json=True):
        if "Get-DhcpServerv4Scope" in cmd:
            return mock_ps_scope_raw
        if "Get-DhcpServerv4OptionValue" in cmd:
            return mock_ps_options_raw
        if "Get-DhcpServerv4ExclusionRange" in cmd:
            raise PowerShellError(cmd, "Cannot find exclusion range for scope", 1)
        if "Get-DhcpServerv4Failover" in cmd:
            raise PowerShellError(cmd, "Cannot find failover relationship for scope", 1)
        return None

    with patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        result = await assemble_scope_state("10.20.30.0")

    assert result.exclusions == []


@pytest.mark.asyncio
async def test_exclusions_sorted_by_ip(mock_ps_scope_raw, mock_ps_options_raw):
    """Exclusions must come out sorted by IP numeric order regardless of PS output order."""
    unsorted_exclusions = [
        {"StartRange": "10.20.30.201", "EndRange": "10.20.30.254"},
        {"StartRange": "10.20.30.1", "EndRange": "10.20.30.99"},
    ]

    def fake_run_ps(cmd, parse_json=True):
        if "Get-DhcpServerv4Scope" in cmd:
            return mock_ps_scope_raw
        if "Get-DhcpServerv4OptionValue" in cmd:
            return mock_ps_options_raw
        if "Get-DhcpServerv4ExclusionRange" in cmd:
            return unsorted_exclusions
        if "Get-DhcpServerv4Failover" in cmd:
            raise PowerShellError(cmd, "Cannot find failover relationship for scope", 1)
        return None

    with patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        result = await assemble_scope_state("10.20.30.0")

    assert str(result.exclusions[0].startAddress) == "10.20.30.1"
    assert str(result.exclusions[1].startAddress) == "10.20.30.201"
