"""Pytest wrapper around ``scripts/terraform_preflight.py``.

Ensures every Terraform target under ``deploy/terraform/`` is structurally
valid (balanced braces, required_version declared, tfvars.example covers
every non-defaulted variable, module source paths resolve).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TF_ROOT = REPO_ROOT / "deploy" / "terraform"
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
from terraform_preflight import preflight  # type: ignore[import-not-found]  # noqa: E402


def _targets() -> list[Path]:
    if not TF_ROOT.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(TF_ROOT.iterdir()):
        if not child.is_dir() or child.name in {"modules"}:
            continue
        if list(child.glob("*.tf")):
            out.append(child)
    return out


@pytest.mark.parametrize("target", _targets(), ids=lambda p: p.name)
def test_terraform_target_passes_preflight(target: Path) -> None:
    issues = preflight(target, REPO_ROOT)
    assert not issues, "Preflight failures:\n" + "\n".join(f"  - {i}" for i in issues)


def test_shared_module_is_present() -> None:
    shared = TF_ROOT / "modules" / "velaflow-host"
    assert shared.is_dir(), "shared velaflow-host module missing"
    for required in ("main.tf", "variables.tf", "outputs.tf", "versions.tf"):
        assert (shared / required).is_file(), f"{required} missing from shared module"
    assert (shared / "templates" / "cloud-init.yaml.tftpl").is_file()


def test_at_least_three_targets_exist() -> None:
    """Guardrail: Proxmox, generic-vm, and oracle-cloud must all ship."""
    names = {t.name for t in _targets()}
    assert {"proxmox", "generic-vm", "oracle-cloud"}.issubset(names), (
        f"expected proxmox/generic-vm/oracle-cloud, found {sorted(names)}"
    )
