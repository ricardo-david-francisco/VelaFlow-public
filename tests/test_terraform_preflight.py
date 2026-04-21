"""Terraform preflight — pure-Python structural validation.

Runs in every pytest invocation. Validates that each shipped Terraform
target (``deploy/terraform/<name>/``) declares the expected blocks,
pins provider versions, does not accidentally reintroduce a cloud
vendor we've chosen to keep out of the free-tier matrix, and carries
matching ``*.tfvars.example`` files where required.

This is deliberately CLI-free: it does not require the ``terraform``
binary. The real ``terraform init``/``terraform validate`` sweep is a
separate operator step documented in ``docs/SECURITY-AUDIT.md`` and
``deploy/terraform/README.md``; this test guards the invariants that
matter regardless of whether Terraform is installed on the runner.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TF_ROOT = REPO_ROOT / "deploy" / "terraform"

# The three free-forever targets we ship. Adding a fourth requires a
# conscious edit to this list and a doc update in
# ``docs/adr/0003-terraform-iac-vs-bash-install.md``.
EXPECTED_TARGETS = {"proxmox", "generic-vm", "oracle-cloud"}

# Provider source names we expect to see. Anything else (e.g.
# hashicorp/aws, hashicorp/azurerm, hashicorp/google) would silently
# reintroduce a paid cloud surface, which the zero-cost invariant
# forbids at the shipped-targets level.
ALLOWED_PROVIDER_SOURCES = {
    "Telmate/proxmox",
    "oracle/oci",
    "hashicorp/null",
    "hashicorp/tls",
    "hashicorp/random",
    "hashicorp/local",
    "hashicorp/template",
    "hashicorp/http",
}

FORBIDDEN_PROVIDER_PATTERNS = (
    re.compile(r"hashicorp/aws", re.IGNORECASE),
    re.compile(r"hashicorp/azurerm", re.IGNORECASE),
    re.compile(r"hashicorp/google", re.IGNORECASE),
    re.compile(r"databricks/databricks", re.IGNORECASE),
)


def _tf_files(target: Path) -> list[Path]:
    return sorted(target.glob("*.tf"))


def _concat_tf(target: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _tf_files(target))


@pytest.fixture(scope="module")
def shipped_targets() -> list[Path]:
    return sorted(
        p for p in TF_ROOT.iterdir() if p.is_dir() and p.name != "modules"
    )


def test_terraform_root_exists() -> None:
    assert TF_ROOT.is_dir(), f"missing terraform root at {TF_ROOT}"


def test_expected_targets_match(shipped_targets: list[Path]) -> None:
    names = {p.name for p in shipped_targets}
    assert names == EXPECTED_TARGETS, (
        f"shipped targets drifted: got {names}, expected {EXPECTED_TARGETS}. "
        "Adding a paid-cloud target breaks the free-forever invariant; "
        "removing one needs an ADR update."
    )


@pytest.mark.parametrize("target_name", sorted(EXPECTED_TARGETS))
def test_target_has_main_and_variables(target_name: str) -> None:
    target = TF_ROOT / target_name
    assert (target / "main.tf").is_file(), f"{target_name}/main.tf missing"
    assert (target / "variables.tf").is_file(), (
        f"{target_name}/variables.tf missing"
    )


@pytest.mark.parametrize("target_name", sorted(EXPECTED_TARGETS))
def test_target_pins_terraform_version(target_name: str) -> None:
    source = _concat_tf(TF_ROOT / target_name)
    assert "required_version" in source, (
        f"{target_name} is missing a required_version pin; Terraform major "
        "compatibility must be stated explicitly."
    )


@pytest.mark.parametrize("target_name", sorted(EXPECTED_TARGETS))
def test_target_pins_provider_versions(target_name: str) -> None:
    source = _concat_tf(TF_ROOT / target_name)
    # Every provider block inside required_providers must have both a
    # source and a version constraint. We smoke-test by requiring at
    # least one ``version = "~> X.Y"`` line in the file.
    assert re.search(r'version\s*=\s*"[~>=<\s\d.]+', source), (
        f"{target_name} does not pin any provider version"
    )


@pytest.mark.parametrize("target_name", sorted(EXPECTED_TARGETS))
def test_no_forbidden_providers(target_name: str) -> None:
    source = _concat_tf(TF_ROOT / target_name)
    for pattern in FORBIDDEN_PROVIDER_PATTERNS:
        match = pattern.search(source)
        assert match is None, (
            f"{target_name} references a paid-cloud provider "
            f"({match.group(0)!r}). Shipped Terraform targets must be "
            "free-forever; paid clouds live only in documented future "
            "scaling options, never in shipped IaC."
        )


@pytest.mark.parametrize("target_name", sorted(EXPECTED_TARGETS))
def test_provider_sources_are_allowlisted(target_name: str) -> None:
    source = _concat_tf(TF_ROOT / target_name)
    declared = set(re.findall(r'source\s*=\s*"([^"]+)"', source))
    # Provider sources are registry addresses of the form ``vendor/name``
    # with alphanumeric/dash identifiers. Other ``source = "..."`` uses
    # (egress CIDRs, ingress security-list rules, etc.) do not match
    # this shape and are ignored here.
    provider_shape = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*/[A-Za-z][A-Za-z0-9_-]*$")
    provider_sources = {s for s in declared if provider_shape.match(s)}
    unexpected = provider_sources - ALLOWED_PROVIDER_SOURCES
    assert not unexpected, (
        f"{target_name} declares unexpected provider source(s) {unexpected}; "
        "add to ALLOWED_PROVIDER_SOURCES only after confirming the target "
        "remains free-tier compatible."
    )


@pytest.mark.parametrize("target_name", sorted(EXPECTED_TARGETS))
def test_tfvars_example_exists(target_name: str) -> None:
    target = TF_ROOT / target_name
    examples = list(target.glob("*.tfvars.example"))
    assert examples, (
        f"{target_name} must ship at least one *.tfvars.example so "
        "operators can populate inputs without guessing."
    )


def test_gitignore_excludes_state_files() -> None:
    gitignore = TF_ROOT / ".gitignore"
    assert gitignore.is_file(), "deploy/terraform/.gitignore missing"
    body = gitignore.read_text(encoding="utf-8")
    for pattern in (".terraform/", "*.tfstate", "*.tfplan"):
        assert pattern in body, (
            f"deploy/terraform/.gitignore is missing {pattern!r}; "
            "state/plan files must never be committed."
        )


def test_proxmox_provider_pin_is_stable_major() -> None:
    source = (TF_ROOT / "proxmox" / "main.tf").read_text(encoding="utf-8")
    # Telmate/proxmox 3.x is RC-only at time of writing; pin 2.x.
    assert re.search(r'Telmate/proxmox[^}]*version\s*=\s*"\s*~>\s*2\.', source), (
        "proxmox target must pin Telmate/proxmox ~> 2.x (3.x is pre-release only)."
    )
