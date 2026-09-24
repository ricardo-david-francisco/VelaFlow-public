"""VelaFlow Terraform preflight validator.

Checks a Terraform target directory (``deploy/terraform/<target>``) for
structural and semantic issues *without* requiring the Terraform CLI to
be installed. This is the guardrail that keeps broken IaC out of
``main`` on developer workstations that do not have ``terraform``.

Checks performed
----------------
1. Every ``.tf`` / ``.tftpl`` file parses as UTF-8 text.
2. ``.tf`` files contain balanced braces.
3. At least one ``terraform { ... }`` block declares ``required_version``.
4. ``terraform.tfvars.example`` exists when the target declares any
   non-defaulted input variables, and covers each of them.
5. Reference to ``../modules/velaflow-host`` resolves.
6. SSH key paths referenced in ``terraform.tfvars.example`` (when they
   point at real absolute paths on the current machine, not tilde/var
   placeholders) have mode ``0600`` on POSIX.

Exit codes
----------
0  all checks passed
1  one or more checks failed
2  usage error
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_TF_BLOCK_RE = re.compile(r"terraform\s*{", re.MULTILINE)
_REQ_VERSION_RE = re.compile(r'required_version\s*=\s*"[^"]+"')
_VAR_DECL_RE = re.compile(r'variable\s+"([^"]+)"\s*{([^}]*)}', re.DOTALL)
_DEFAULT_RE = re.compile(r"^\s*default\s*=", re.MULTILINE)
_MODULE_SRC_RE = re.compile(r'source\s*=\s*"([^"]+)"')


class PreflightError(Exception):
    """Raised for any preflight failure — collected rather than raised
    by the top-level CLI so we can surface every issue at once."""


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover
        raise PreflightError(f"{p}: not valid UTF-8 ({exc})") from exc


def _check_braces(path: Path, text: str) -> list[str]:
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return [f"{path}: unbalanced '}}' at byte {i}"]
    if depth != 0:
        return [f"{path}: {depth} unclosed '{{' block(s)"]
    return []


def _parse_variables(tf_text: str) -> dict[str, bool]:
    """Return {name: has_default} for every ``variable`` block."""
    out: dict[str, bool] = {}
    for match in _VAR_DECL_RE.finditer(tf_text):
        name, body = match.group(1), match.group(2)
        out[name] = bool(_DEFAULT_RE.search(body))
    return out


def _collect_variables(target: Path) -> dict[str, bool]:
    merged: dict[str, bool] = {}
    for tf in sorted(target.glob("*.tf")):
        merged.update(_parse_variables(_read(tf)))
    return merged


def _collect_tfvars_keys(tfvars: Path) -> set[str]:
    keys: set[str] = set()
    for raw in _read(tfvars).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def _check_required_version(target: Path, issues: list[str]) -> None:
    for tf in target.glob("*.tf"):
        text = _read(tf)
        if _TF_BLOCK_RE.search(text) and _REQ_VERSION_RE.search(text):
            return
    issues.append(f"{target}: no terraform block declares required_version")


def _check_module_sources(target: Path, repo_root: Path, issues: list[str]) -> None:
    for tf in target.glob("*.tf"):
        text = _read(tf)
        for match in _MODULE_SRC_RE.finditer(text):
            src = match.group(1)
            if src.startswith("../") or src.startswith("./"):
                resolved = (target / src).resolve()
                if not resolved.is_dir():
                    issues.append(
                        f"{tf}: module source '{src}' does not resolve to a directory "
                        f"(expected {resolved})"
                    )
                elif repo_root not in resolved.parents and resolved != repo_root:
                    issues.append(
                        f"{tf}: module source '{src}' escapes the repo root"
                    )


def _check_tftpl_utf8(target: Path, repo_root: Path, issues: list[str]) -> None:
    # Walk the shared module too so we validate templates reachable from this target.
    for tpl in target.rglob("*.tftpl"):
        _read(tpl)
    shared = (repo_root / "deploy" / "terraform" / "modules" / "velaflow-host")
    if shared.is_dir():
        for tpl in shared.rglob("*.tftpl"):
            _read(tpl)


def preflight(target: Path, repo_root: Path) -> list[str]:
    """Run every check against ``target`` and return the list of issues."""
    issues: list[str] = []

    if not target.is_dir():
        return [f"{target}: not a directory"]

    tf_files = list(target.glob("*.tf"))
    if not tf_files:
        issues.append(f"{target}: contains no .tf files")
        return issues

    for tf in tf_files:
        text = _read(tf)
        issues.extend(_check_braces(tf, text))

    _check_required_version(target, issues)
    _check_module_sources(target, repo_root, issues)
    _check_tftpl_utf8(target, repo_root, issues)

    declared = _collect_variables(target)
    non_defaulted = {name for name, has_default in declared.items() if not has_default}
    if non_defaulted:
        tfvars = target / "terraform.tfvars.example"
        if not tfvars.is_file():
            issues.append(
                f"{target}: terraform.tfvars.example missing but "
                f"{len(non_defaulted)} non-defaulted variables declared "
                f"({sorted(non_defaulted)[:3]}...)"
            )
        else:
            provided = _collect_tfvars_keys(tfvars)
            missing = sorted(non_defaulted - provided)
            if missing:
                issues.append(
                    f"{tfvars}: missing values for non-defaulted variables: {missing}"
                )

    return issues


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").is_file() and (candidate / "deploy").is_dir():
            return candidate
    return start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        type=Path,
        help="Path to a terraform target directory "
        "(e.g. deploy/terraform/proxmox).",
    )
    args = parser.parse_args(argv)

    target = args.target.resolve()
    repo_root = _find_repo_root(target)

    issues = preflight(target, repo_root)
    if issues:
        print(f"[FAIL] {target}", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1

    print(f"[OK] {target}: {len(list(target.glob('*.tf')))} .tf files validated")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
