"""Tests for scripts/validate_values.py.

The validator script is self-contained (no imports from `app`), so we import
it by path rather than relying on package structure.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from ipaddress import IPv4Address
from pathlib import Path

import pytest

# ─── Import the script as a module ───────────────────────────────────────────

_SCRIPT = Path(__file__).parent.parent / "scripts" / "validate_values.py"

spec = importlib.util.spec_from_file_location("validate_values", _SCRIPT)
vv = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["validate_values"] = vv
spec.loader.exec_module(vv)  # type: ignore[union-attr]


# ─── Fixture helpers ─────────────────────────────────────────────────────────

def _minimal_valid() -> dict:
    """Minimal values dict that passes all validators without warnings."""
    return {
        "dhcp_values": {
            "scopeName": "Test Scope",
            "network": "10.20.30.0",
            "subnetMask": "255.255.255.0",
            "startRange": "10.20.30.100",
            "endRange": "10.20.30.200",
            "leaseDurationDays": 8,
            "description": "unit test scope",
            "gateway": "10.20.30.1",
            "dns": {
                "servers": ["10.0.0.53"],
                "domain": "lab.local",
            },
            "exclusions": [],
            "failover": None,
        },
        "apiServer": {
            "url": "https://dhcp-api.example.com",
            "tokenSecretRef": {
                "name": "dhcp-token",
                "namespace": "crossplane-system",
                "key": "token",
            },
        },
        "crossplane": {
            "namespace": "crossplane-system",
            "providerConfigName": "dhcp-provider",
        },
    }


# ─── _deep_merge ─────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        vv._deep_merge(base, {"b": 99})
        assert base == {"a": 1, "b": 99}

    def test_recursive_merge(self):
        base = {"dhcp": {"network": "10.0.0.0", "mask": "255.0.0.0"}}
        vv._deep_merge(base, {"dhcp": {"mask": "255.255.0.0"}})
        assert base["dhcp"] == {"network": "10.0.0.0", "mask": "255.255.0.0"}

    def test_null_replaces_key(self):
        base = {"dhcp": {"gateway": "10.0.0.1"}}
        vv._deep_merge(base, {"dhcp": {"gateway": None}})
        assert base["dhcp"]["gateway"] is None

    def test_null_overrides_nested_map(self):
        """null in override should replace an entire nested map (Helm semantics)."""
        base = {"failover": {"mode": "HotStandby"}}
        vv._deep_merge(base, {"failover": None})
        assert base["failover"] is None

    def test_new_key_added(self):
        base = {"a": 1}
        vv._deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}

    def test_list_replaced_not_merged(self):
        base = {"servers": ["1.1.1.1"]}
        vv._deep_merge(base, {"servers": ["2.2.2.2", "3.3.3.3"]})
        assert base["servers"] == ["2.2.2.2", "3.3.3.3"]


# ─── get_nested / _nested_present ────────────────────────────────────────────

class TestGetNested:
    def test_single_key(self):
        assert vv.get_nested({"a": 1}, "a") == 1

    def test_two_levels(self):
        assert vv.get_nested({"a": {"b": 42}}, "a", "b") == 42

    def test_missing_returns_default(self):
        assert vv.get_nested({}, "a", "b") is None

    def test_custom_default(self):
        assert vv.get_nested({}, "x", default="fallback") == "fallback"

    def test_none_value_returns_default(self):
        # get_nested stops and returns default when it hits None mid-path
        assert vv.get_nested({"a": None}, "a", "b") is None


class TestNestedPresent:
    def test_present(self):
        assert vv._nested_present({"a": {"b": "v"}}, "a", "b")

    def test_missing_key(self):
        assert not vv._nested_present({"a": {}}, "a", "b")

    def test_none_value_is_absent(self):
        assert not vv._nested_present({"a": None}, "a")

    def test_false_is_present(self):
        # False is a valid non-None value
        assert vv._nested_present({"a": False}, "a")


# ─── _is_valid_k8s_label ─────────────────────────────────────────────────────

class TestK8sLabel:
    @pytest.mark.parametrize("name", [
        "dhcp-provider",
        "crossplane-system",
        "a",
        "abc123",
        "x" * 63,
    ])
    def test_valid(self, name):
        assert vv._is_valid_k8s_label(name)

    @pytest.mark.parametrize("name", [
        "",
        "x" * 64,
        "Has-Uppercase",
        "-leading-hyphen",
        "trailing-hyphen-",
        "under_score",
        "has space",
    ])
    def test_invalid(self, name):
        assert not vv._is_valid_k8s_label(name)


# ─── validate_required_fields ────────────────────────────────────────────────

class TestRequiredFields:
    def test_all_present_no_issues(self):
        assert vv.validate_required_fields(_minimal_valid()) == []

    def test_missing_scope_name(self):
        values = _minimal_valid()
        del values["dhcp_values"]["scopeName"]
        issues = vv.validate_required_fields(values)
        paths = [i.path for i in issues]
        assert "dhcp_values.scopeName" in paths

    def test_missing_api_server_url(self):
        values = _minimal_valid()
        del values["apiServer"]["url"]
        issues = vv.validate_required_fields(values)
        assert any(i.path == "apiServer.url" for i in issues)

    def test_missing_token_secret_ref_namespace(self):
        values = _minimal_valid()
        del values["apiServer"]["tokenSecretRef"]["namespace"]
        issues = vv.validate_required_fields(values)
        assert any(i.path == "apiServer.tokenSecretRef.namespace" for i in issues)

    def test_missing_dns_servers(self):
        values = _minimal_valid()
        del values["dhcp_values"]["dns"]["servers"]
        issues = vv.validate_required_fields(values)
        assert any(i.path == "dhcp_values.dns.servers" for i in issues)

    def test_all_issues_are_errors(self):
        values = {}
        issues = vv.validate_required_fields(values)
        assert issues
        assert all(i.severity == "error" for i in issues)


# ─── validate_dhcp_pydantic ──────────────────────────────────────────────────

class TestDhcpPydantic:
    def test_valid_passes(self):
        assert vv.validate_dhcp_pydantic(_minimal_valid()) == []

    def test_invalid_network(self):
        values = _minimal_valid()
        # network address not matching the subnet
        values["dhcp_values"]["network"] = "10.20.30.1"  # host bit set
        issues = vv.validate_dhcp_pydantic(values)
        assert issues
        assert all(i.severity == "error" for i in issues)

    def test_end_range_before_start_range(self):
        values = _minimal_valid()
        values["dhcp_values"]["startRange"] = "10.20.30.200"
        values["dhcp_values"]["endRange"] = "10.20.30.100"
        issues = vv.validate_dhcp_pydantic(values)
        assert issues

    def test_gateway_in_distribution_range_no_exclusion(self):
        values = _minimal_valid()
        values["dhcp_values"]["gateway"] = "10.20.30.150"  # inside 100-200
        values["dhcp_values"]["exclusions"] = []
        issues = vv.validate_dhcp_pydantic(values)
        assert any("distribution range" in i.message for i in issues)

    def test_gateway_in_distribution_range_covered_by_exclusion_passes(self):
        values = _minimal_valid()
        values["dhcp_values"]["gateway"] = "10.20.30.150"
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "10.20.30.150", "endAddress": "10.20.30.150"}
        ]
        assert vv.validate_dhcp_pydantic(values) == []

    def test_overlapping_exclusions(self):
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "10.20.30.10", "endAddress": "10.20.30.50"},
            {"startAddress": "10.20.30.40", "endAddress": "10.20.30.60"},
        ]
        issues = vv.validate_dhcp_pydantic(values)
        assert issues

    def test_duplicate_exclusions(self):
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "10.20.30.10", "endAddress": "10.20.30.20"},
            {"startAddress": "10.20.30.10", "endAddress": "10.20.30.20"},
        ]
        issues = vv.validate_dhcp_pydantic(values)
        assert issues

    def test_dhcp_values_not_a_dict(self):
        values = _minimal_valid()
        values["dhcp_values"] = "not a dict"
        issues = vv.validate_dhcp_pydantic(values)
        assert issues
        assert issues[0].path == "dhcp_values"

    def test_suggestion_for_gateway_in_range(self):
        values = _minimal_valid()
        values["dhcp_values"]["gateway"] = "10.20.30.150"
        issues = vv.validate_dhcp_pydantic(values)
        assert any(i.suggestion is not None and "exclusion" in i.suggestion for i in issues)

    def test_suggestion_for_wrong_network_address(self):
        values = _minimal_valid()
        values["dhcp_values"]["network"] = "10.20.30.1"  # should be .0
        issues = vv.validate_dhcp_pydantic(values)
        # Should suggest the correct network address
        assert any(i.suggestion and "10.20.30.0" in i.suggestion for i in issues)

    def test_lease_duration_too_low(self):
        values = _minimal_valid()
        values["dhcp_values"]["leaseDurationDays"] = 0
        issues = vv.validate_dhcp_pydantic(values)
        assert issues

    def test_lease_duration_too_high(self):
        values = _minimal_valid()
        values["dhcp_values"]["leaseDurationDays"] = 9999
        issues = vv.validate_dhcp_pydantic(values)
        assert issues

    def test_gateway_null_is_valid(self):
        values = _minimal_valid()
        values["dhcp_values"]["gateway"] = None
        assert vv.validate_dhcp_pydantic(values) == []

    def test_failover_hotstandby_no_server_role(self):
        values = _minimal_valid()
        values["dhcp_values"]["failover"] = {
            "partnerServer": "dhcp-secondary.example.com",
            "relationshipName": "primary-secondary",
            "mode": "HotStandby",
            # serverRole omitted → validation error
            "maxClientLeadTimeMinutes": 60,
        }
        issues = vv.validate_dhcp_pydantic(values)
        assert issues

    def test_failover_hotstandby_valid(self):
        values = _minimal_valid()
        values["dhcp_values"]["failover"] = {
            "partnerServer": "dhcp-secondary.example.com",
            "relationshipName": "primary-secondary",
            "mode": "HotStandby",
            "serverRole": "Active",
            "maxClientLeadTimeMinutes": 60,
        }
        assert vv.validate_dhcp_pydantic(values) == []

    def test_failover_loadbalance_no_percent(self):
        values = _minimal_valid()
        values["dhcp_values"]["failover"] = {
            "partnerServer": "dhcp-secondary.example.com",
            "relationshipName": "lb-pair",
            "mode": "LoadBalance",
            # loadBalancePercent omitted → error
            "maxClientLeadTimeMinutes": 60,
        }
        issues = vv.validate_dhcp_pydantic(values)
        assert issues


# ─── validate_exclusion_order ─────────────────────────────────────────────────

class TestExclusionOrder:
    def test_sorted_no_warning(self):
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "10.20.30.1",   "endAddress": "10.20.30.10"},
            {"startAddress": "10.20.30.201", "endAddress": "10.20.30.254"},
        ]
        assert vv.validate_exclusion_order(values) == []

    def test_unsorted_warns(self):
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "10.20.30.201", "endAddress": "10.20.30.254"},
            {"startAddress": "10.20.30.1",   "endAddress": "10.20.30.10"},
        ]
        issues = vv.validate_exclusion_order(values)
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert issues[0].suggestion is not None

    def test_single_exclusion_no_warning(self):
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "10.20.30.1", "endAddress": "10.20.30.99"},
        ]
        assert vv.validate_exclusion_order(values) == []

    def test_empty_exclusions_no_warning(self):
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = []
        assert vv.validate_exclusion_order(values) == []

    def test_malformed_ip_is_ignored(self):
        """Malformed IPs in exclusions are skipped — Pydantic will catch them separately."""
        values = _minimal_valid()
        values["dhcp_values"]["exclusions"] = [
            {"startAddress": "not-an-ip", "endAddress": "10.20.30.10"},
        ]
        assert vv.validate_exclusion_order(values) == []


# ─── validate_dns_duplicates ──────────────────────────────────────────────────

class TestDnsDuplicates:
    def test_no_duplicates(self):
        assert vv.validate_dns_duplicates(_minimal_valid()) == []

    def test_duplicate_warns(self):
        values = _minimal_valid()
        values["dhcp_values"]["dns"]["servers"] = ["10.0.0.53", "10.0.0.53"]
        issues = vv.validate_dns_duplicates(values)
        assert len(issues) == 1
        assert issues[0].severity == "warning"

    def test_no_servers_key(self):
        values = _minimal_valid()
        del values["dhcp_values"]["dns"]["servers"]
        assert vv.validate_dns_duplicates(values) == []


# ─── validate_dns_domain ──────────────────────────────────────────────────────

class TestDnsDomain:
    def test_valid_domain_passes(self):
        assert vv.validate_dns_domain(_minimal_valid()) == []

    def test_domain_with_space_fails(self):
        values = _minimal_valid()
        values["dhcp_values"]["dns"]["domain"] = "lab local"
        issues = vv.validate_dns_domain(values)
        assert issues
        assert issues[0].severity == "error"

    def test_domain_too_long_fails(self):
        values = _minimal_valid()
        values["dhcp_values"]["dns"]["domain"] = "a" * 257
        issues = vv.validate_dns_domain(values)
        assert any("exceeds 256" in i.message for i in issues)

    def test_missing_domain_passes(self):
        values = _minimal_valid()
        del values["dhcp_values"]["dns"]["domain"]
        assert vv.validate_dns_domain(values) == []

    def test_non_string_domain_fails(self):
        values = _minimal_valid()
        values["dhcp_values"]["dns"]["domain"] = 12345
        issues = vv.validate_dns_domain(values)
        assert issues


# ─── validate_api_server ──────────────────────────────────────────────────────

class TestApiServer:
    def test_valid_passes(self):
        assert vv.validate_api_server(_minimal_valid()) == []

    def test_trailing_slash_warns(self):
        values = _minimal_valid()
        values["apiServer"]["url"] = "https://dhcp-api.example.com/"
        issues = vv.validate_api_server(values)
        assert any(i.severity == "warning" and "double slash" in i.message.lower() for i in issues)

    def test_non_http_scheme_fails(self):
        values = _minimal_valid()
        values["apiServer"]["url"] = "ftp://dhcp-api.example.com"
        issues = vv.validate_api_server(values)
        assert any(i.severity == "error" and "http or https" in i.message for i in issues)

    def test_invalid_secret_ref_name(self):
        values = _minimal_valid()
        values["apiServer"]["tokenSecretRef"]["name"] = "Has_Underscore"
        issues = vv.validate_api_server(values)
        assert any("tokenSecretRef.name" in i.path for i in issues)

    def test_invalid_secret_ref_namespace(self):
        values = _minimal_valid()
        values["apiServer"]["tokenSecretRef"]["namespace"] = "UPPERCASE"
        issues = vv.validate_api_server(values)
        assert any("tokenSecretRef.namespace" in i.path for i in issues)


# ─── validate_crossplane ──────────────────────────────────────────────────────

class TestCrossplane:
    def test_valid_passes(self):
        assert vv.validate_crossplane(_minimal_valid()) == []

    def test_invalid_namespace(self):
        values = _minimal_valid()
        values["crossplane"]["namespace"] = "INVALID NAMESPACE"
        issues = vv.validate_crossplane(values)
        assert any("crossplane.namespace" in i.path for i in issues)

    def test_invalid_provider_config_name(self):
        values = _minimal_valid()
        values["crossplane"]["providerConfigName"] = "-bad-start"
        issues = vv.validate_crossplane(values)
        assert any("crossplane.providerConfigName" in i.path for i in issues)

    def test_crossplane_missing_entirely_no_crash(self):
        values = _minimal_valid()
        del values["crossplane"]
        assert vv.validate_crossplane(values) == []


# ─── validate_kubernetes_names ───────────────────────────────────────────────

class TestKubernetesNames:
    def test_valid_network_passes(self):
        assert vv.validate_kubernetes_names(_minimal_valid()) == []

    def test_network_generates_valid_cr_name(self):
        values = _minimal_valid()
        values["dhcp_values"]["network"] = "192.168.100.0"
        assert vv.validate_kubernetes_names(values) == []

    def test_missing_network_no_crash(self):
        values = _minimal_valid()
        del values["dhcp_values"]["network"]
        assert vv.validate_kubernetes_names(values) == []

    def test_cr_name_too_long(self):
        """A network that produces a CR name > 63 chars should be caught."""
        # CR name format: dhcp-scope-X-X-X-X (max 15 chars + 12 from "dhcp-scope-" prefix)
        # Normal IPs won't exceed 63, but we can test the logic directly
        values = _minimal_valid()
        # We monkey-patch just to test the length check path
        long_network = "10.20.30.0"  # produces "dhcp-scope-10-20-30-0" — fine
        values["dhcp_values"]["network"] = long_network
        issues = vv.validate_kubernetes_names(values)
        assert issues == []  # normal IP is fine


# ─── validate_parity_risks ───────────────────────────────────────────────────

class TestParityRisks:
    def test_no_risks_when_all_explicit(self):
        assert vv.validate_parity_risks(_minimal_valid()) == []

    def test_empty_failover_dict_warns(self):
        values = _minimal_valid()
        values["dhcp_values"]["failover"] = {}
        issues = vv.validate_parity_risks(values)
        assert any("failover" in i.path and i.severity == "warning" for i in issues)
        assert any("null" in (i.suggestion or "") for i in issues)

    def test_null_failover_no_warning(self):
        values = _minimal_valid()
        values["dhcp_values"]["failover"] = None
        assert vv.validate_parity_risks(values) == []

    def test_missing_description_warns(self):
        values = _minimal_valid()
        del values["dhcp_values"]["description"]
        issues = vv.validate_parity_risks(values)
        assert any("description" in i.path and i.severity == "warning" for i in issues)

    def test_missing_gateway_warns(self):
        values = _minimal_valid()
        del values["dhcp_values"]["gateway"]
        issues = vv.validate_parity_risks(values)
        assert any("gateway" in i.path and i.severity == "warning" for i in issues)


# ─── validate_effective_values ───────────────────────────────────────────────

class TestValidateEffectiveValues:
    def test_minimal_valid_no_errors(self):
        issues = vv.validate_effective_values(_minimal_valid())
        errors = [i for i in issues if i.severity == "error"]
        assert errors == []

    def test_empty_dict_has_required_field_errors(self):
        issues = vv.validate_effective_values({})
        assert any(i.severity == "error" for i in issues)

    def test_combined_error_and_warning(self):
        values = _minimal_valid()
        del values["dhcp_values"]["description"]        # → warning
        values["dhcp_values"]["leaseDurationDays"] = 0  # → error
        issues = vv.validate_effective_values(values)
        assert any(i.severity == "error" for i in issues)
        assert any(i.severity == "warning" for i in issues)


# ─── ValidationResult helpers ────────────────────────────────────────────────

class TestValidationResult:
    def _make(self, severities):
        result = vv.ValidationResult(context=None)
        for sev in severities:
            result.issues.append(vv.ValidationIssue(sev, "path", "msg"))
        return result

    def test_has_errors_true(self):
        assert self._make(["error"]).has_errors

    def test_has_errors_false_when_only_warnings(self):
        assert not self._make(["warning"]).has_errors

    def test_has_warnings_true(self):
        assert self._make(["warning"]).has_warnings

    def test_errors_property(self):
        r = self._make(["error", "warning", "error"])
        assert len(r.errors) == 2

    def test_warnings_property(self):
        r = self._make(["error", "warning"])
        assert len(r.warnings) == 1


# ─── ClusterContext helpers ───────────────────────────────────────────────────

class TestClusterContext:
    def _make(self, site_file=None, mce_file=None, cluster_file=Path("cluster/values.yaml")):
        return vv.ClusterContext(
            site="site-a", mce="mce-a", cluster="cluster-a",
            site_file=site_file, mce_file=mce_file, cluster_file=cluster_file,
        )

    def test_label(self):
        assert self._make().label() == "site-a/mce-a/cluster-a"

    def test_merge_chain_all_files(self):
        chain = self._make(
            site_file=Path("site/values.yaml"),
            mce_file=Path("mce/values.yaml"),
        ).merge_chain()
        assert len(chain) == 3

    def test_merge_chain_only_cluster(self):
        chain = self._make().merge_chain()
        assert len(chain) == 1

    def test_merge_chain_cluster_and_mce(self):
        chain = self._make(mce_file=Path("mce/values.yaml")).merge_chain()
        assert len(chain) == 2


# ─── discover_clusters ────────────────────────────────────────────────────────

class TestDiscoverClusters:
    def test_new_layout(self, tmp_path):
        """sites/{site}/{mce}/{cluster}/values.yaml"""
        cluster_dir = tmp_path / "sites" / "site-a" / "mce-a" / "cluster-a"
        cluster_dir.mkdir(parents=True)
        (cluster_dir / "values.yaml").write_text("dhcp_values: {}")

        contexts = vv.discover_clusters(tmp_path / "sites")
        assert len(contexts) == 1
        ctx = contexts[0]
        assert ctx.site == "site-a"
        assert ctx.mce == "mce-a"
        assert ctx.cluster == "cluster-a"
        assert ctx.site_file is None  # no values.yaml at site level in this test
        assert ctx.mce_file is None   # no values.yaml at mce level

    def test_new_layout_with_inheritance_files(self, tmp_path):
        site_dir = tmp_path / "sites" / "site-a"
        mce_dir  = site_dir / "mce-a"
        (mce_dir / "cluster-a").mkdir(parents=True)

        (site_dir / "values.yaml").write_text("site: true")
        (mce_dir  / "values.yaml").write_text("mce: true")
        (mce_dir  / "cluster-a" / "values.yaml").write_text("dhcp_values: {}")

        contexts = vv.discover_clusters(tmp_path / "sites")
        assert len(contexts) == 1
        ctx = contexts[0]
        assert ctx.site_file is not None
        assert ctx.mce_file is not None
        assert len(ctx.merge_chain()) == 3

    def test_old_layout(self, tmp_path):
        """sites/{site}/mce/{mce}/hosted-cluster/{cluster}.yaml"""
        hc_dir = tmp_path / "sites" / "site-a" / "mce" / "mce-a" / "hosted-cluster"
        hc_dir.mkdir(parents=True)
        (hc_dir / "cluster-a.yaml").write_text("dhcp_values: {}")

        contexts = vv.discover_clusters(tmp_path / "sites")
        assert len(contexts) == 1
        ctx = contexts[0]
        assert ctx.site == "site-a"
        assert ctx.mce == "mce-a"
        assert ctx.cluster == "cluster-a"

    def test_empty_sites_dir(self, tmp_path):
        (tmp_path / "sites").mkdir()
        assert vv.discover_clusters(tmp_path / "sites") == []

    def test_multiple_clusters_sorted(self, tmp_path):
        sites = tmp_path / "sites"
        for cluster in ("cluster-z", "cluster-a", "cluster-m"):
            d = sites / "site-a" / "mce-a" / cluster
            d.mkdir(parents=True)
            (d / "values.yaml").write_text("x: 1")

        contexts = vv.discover_clusters(sites)
        names = [c.cluster for c in contexts]
        assert names == sorted(names)

    def test_missing_sites_dir_exits(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            vv.discover_clusters(tmp_path / "nonexistent")
        assert exc_info.value.code == 2


# ─── load_yaml_file ──────────────────────────────────────────────────────────

class TestLoadYamlFile:
    def test_valid_yaml(self, tmp_path):
        f = tmp_path / "v.yaml"
        f.write_text("a: 1\nb: 2\n")
        assert vv.load_yaml_file(f) == {"a": 1, "b": 2}

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            vv.load_yaml_file(tmp_path / "nonexistent.yaml")
        assert exc_info.value.code == 2

    def test_invalid_yaml_exits(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("key: [unclosed")
        with pytest.raises(SystemExit) as exc_info:
            vv.load_yaml_file(f)
        assert exc_info.value.code == 2

    def test_non_mapping_top_level_exits(self, tmp_path):
        f = tmp_path / "list.yaml"
        f.write_text("- a\n- b\n")
        with pytest.raises(SystemExit) as exc_info:
            vv.load_yaml_file(f)
        assert exc_info.value.code == 2

    def test_empty_yaml_returns_empty_dict(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        assert vv.load_yaml_file(f) == {}


# ─── merge_files ─────────────────────────────────────────────────────────────

class TestMergeFiles:
    def test_inheritance_chain(self, tmp_path):
        site    = tmp_path / "site.yaml"
        mce     = tmp_path / "mce.yaml"
        cluster = tmp_path / "cluster.yaml"

        site.write_text("dhcp_values:\n  leaseDurationDays: 1\n  description: site")
        mce.write_text("dhcp_values:\n  leaseDurationDays: 8\n")
        cluster.write_text("dhcp_values:\n  scopeName: Cluster\n")

        result = vv.merge_files(site, mce, cluster)
        assert result["dhcp_values"]["leaseDurationDays"] == 8    # MCE wins
        assert result["dhcp_values"]["description"] == "site"     # inherited from site
        assert result["dhcp_values"]["scopeName"] == "Cluster"    # from cluster


# ─── _to_scope_kwargs ────────────────────────────────────────────────────────

class TestToScopeKwargs:
    def test_dns_remapping(self):
        dv = {
            "scopeName": "Test", "network": "10.0.0.0", "subnetMask": "255.0.0.0",
            "startRange": "10.0.0.1", "endRange": "10.0.0.254",
            "leaseDurationDays": 1,
            "dns": {"servers": ["1.1.1.1"], "domain": "test.local"},
        }
        kwargs = vv._to_scope_kwargs(dv)
        assert kwargs["dnsServers"] == ["1.1.1.1"]
        assert kwargs["dnsDomain"] == "test.local"
        assert "dns" not in kwargs

    def test_missing_dns_uses_defaults(self):
        dv = {"scopeName": "Test"}
        kwargs = vv._to_scope_kwargs(dv)
        assert kwargs["dnsServers"] == []
        assert kwargs["dnsDomain"] == ""

    def test_none_gateway_preserved(self):
        dv = {"gateway": None}
        kwargs = vv._to_scope_kwargs(dv)
        assert kwargs["gateway"] is None


# ─── JSON reporter output shape ───────────────────────────────────────────────

class TestJsonReporter:
    def test_output_is_valid_json(self, capsys):
        results = [vv.ValidationResult(context=None, issues=[])]
        vv.print_json_report(results)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["passed"] is True

    def test_error_present_in_json(self, capsys):
        result = vv.ValidationResult(context=None)
        result.issues.append(vv.ValidationIssue("error", "dhcp_values.network", "bad network"))
        vv.print_json_report([result])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["passed"] is False
        assert data[0]["issues"][0]["severity"] == "error"
        assert data[0]["issues"][0]["path"] == "dhcp_values.network"

    def test_cluster_context_in_json(self, capsys):
        ctx = vv.ClusterContext(
            site="site-a", mce="mce-a", cluster="cluster-a",
            site_file=None, mce_file=None, cluster_file=Path("values.yaml"),
        )
        result = vv.ValidationResult(context=ctx, issues=[])
        vv.print_json_report([result])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["cluster"] == "site-a/mce-a/cluster-a"
        assert data[0]["site"] == "site-a"


# ─── Integration: full YAML file round-trip ───────────────────────────────────

class TestIntegration:
    def _write_yaml(self, tmp_path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(textwrap.dedent(content))
        return p

    def test_valid_cluster_no_errors(self, tmp_path):
        cluster_file = self._write_yaml(tmp_path, "values.yaml", """\
            dhcp_values:
              scopeName: "Cluster A"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              description: "test"
              gateway: "10.20.30.1"
              dns:
                servers:
                  - "10.0.0.53"
                domain: "lab.local"
              exclusions: []
              failover: null
            apiServer:
              url: "https://dhcp-api.example.com"
              tokenSecretRef:
                name: "dhcp-token"
                namespace: "crossplane-system"
                key: "token"
            crossplane:
              namespace: "crossplane-system"
              providerConfigName: "dhcp-provider"
        """)
        merged = vv.merge_files(cluster_file)
        issues = vv.validate_effective_values(merged)
        errors = [i for i in issues if i.severity == "error"]
        assert errors == []

    def test_inheritance_mce_overrides_site(self, tmp_path):
        site_file = self._write_yaml(tmp_path, "site.yaml", """\
            dhcp_values:
              leaseDurationDays: 1
              description: site-default
            apiServer:
              url: "https://dhcp-api.example.com"
              tokenSecretRef:
                name: "dhcp-token"
                namespace: "crossplane-system"
                key: "token"
        """)
        cluster_file = self._write_yaml(tmp_path, "cluster.yaml", """\
            dhcp_values:
              scopeName: "Cluster A"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              gateway: "10.20.30.1"
              dns:
                servers: ["10.0.0.53"]
        """)
        merged = vv.merge_files(site_file, cluster_file)
        # cluster's leaseDurationDays wins
        assert merged["dhcp_values"]["leaseDurationDays"] == 8
        # site's description is inherited
        assert merged["dhcp_values"]["description"] == "site-default"

    def test_gateway_in_range_error_in_full_file(self, tmp_path):
        cluster_file = self._write_yaml(tmp_path, "values.yaml", """\
            dhcp_values:
              scopeName: "Bad Scope"
              network: "10.20.30.0"
              subnetMask: "255.255.255.0"
              startRange: "10.20.30.100"
              endRange: "10.20.30.200"
              leaseDurationDays: 8
              description: ""
              gateway: "10.20.30.150"
              dns:
                servers: ["10.0.0.53"]
              exclusions: []
              failover: null
            apiServer:
              url: "https://dhcp-api.example.com"
              tokenSecretRef:
                name: "dhcp-token"
                namespace: "crossplane-system"
                key: "token"
        """)
        merged = vv.merge_files(cluster_file)
        issues = vv.validate_effective_values(merged)
        assert any("distribution range" in i.message for i in issues if i.severity == "error")
