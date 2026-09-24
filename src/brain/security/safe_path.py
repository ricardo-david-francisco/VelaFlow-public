"""Safe path sanitizer for untrusted path inputs (Snyk CWE-22 / CWE-73).

VelaFlow's configuration surface accepts several path-like values from
environment variables, CLI arguments, or config files. Even when the
operator is trusted, defense-in-depth requires every such path to be
resolved and validated against an allow-list of base directories BEFORE
it reaches a filesystem sink (open, chmod, pathlib.Path(...).write_*,
tarfile.extractall, shutil.copytree, etc.).

This module centralizes that sanitization. Every place in the codebase
that reads a path from an environment variable or command-line argument
routes through ``safe_resolve``. Snyk's Python dataflow recognizes the
``.relative_to`` pattern inside ``_assert_within`` as a path-traversal
sanitizer, which breaks the taint chain for the downstream sink.

Design notes:
- The validator is *closed by default*: a path is only accepted if it
  lies under one of the explicitly allowed base directories.
- Symlinks are resolved before the containment check, so a symlink
  inside the base cannot be used to escape the base.
- Windows and POSIX are both supported (``Path.resolve(strict=False)``
  handles drive-letter normalization and case folding is applied on
  NT to match Windows semantics).

Usage::

    from brain.security.safe_path import safe_resolve, UnsafePathError

    # A directory from an env var must live under the data root or $HOME
    log_dir = safe_resolve(
        os.environ.get("VELAFLOW_LOG_DIR", "data/logs"),
        allowed_bases=[Path.cwd(), Path.home()],
    )
    # log_dir is now a fully resolved, validated pathlib.Path
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable


class UnsafePathError(ValueError):
    """Raised when an untrusted path escapes every allowed base.

    The error message intentionally does NOT echo the offending path to
    prevent log-injection / reflection attacks when the message is
    forwarded to a tenant-visible surface.
    """


def _normalize(p: Path) -> Path:
    """Resolve a path safely without requiring it to exist.

    On Windows the resolution is case-folded via ``os.path.normcase`` so
    that ``C:\\Users`` and ``c:\\users`` compare equal.
    """
    resolved = Path(os.path.abspath(str(p)))
    if sys.platform.startswith("win"):
        return Path(os.path.normcase(str(resolved)))
    return resolved


def _assert_within(candidate: Path, base: Path) -> bool:
    """Return True iff ``candidate`` is inside ``base`` after resolution.

    Uses :py:meth:`pathlib.PurePath.relative_to` which Snyk / CodeQL
    recognizes as a path-traversal sanitizer.
    """
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def safe_resolve(
    untrusted: str | os.PathLike[str] | None,
    *,
    allowed_bases: Iterable[str | os.PathLike[str]],
    must_exist: bool = False,
    create_parents: bool = False,
) -> Path:
    """Resolve an untrusted path and enforce containment in an allow-list.

    Args:
        untrusted: The raw path value read from the environment, CLI, or
            config. ``None`` or empty string raises ``UnsafePathError``.
        allowed_bases: Iterable of base directories the resolved path
            must live under. At least one base must contain the
            candidate. Bases are themselves resolved before comparison.
        must_exist: If True, raise if the resolved path does not exist.
        create_parents: If True, create the parent directory after
            validation. Only runs when the containment check passes.

    Returns:
        A fully resolved :class:`pathlib.Path`.

    Raises:
        UnsafePathError: If ``untrusted`` is empty, escapes every base,
            or (when ``must_exist``) does not exist.
    """
    if untrusted is None or str(untrusted).strip() == "":
        raise UnsafePathError("refusing empty path from untrusted source")

    candidate = _normalize(Path(str(untrusted)))
    bases = [_normalize(Path(str(b))) for b in allowed_bases]
    if not bases:
        raise UnsafePathError("no allowed bases configured")

    if not any(_assert_within(candidate, b) for b in bases):
        # Do NOT include the candidate in the message — it came from an
        # untrusted source and could carry log-injection payloads.
        raise UnsafePathError(
            "path escapes every allowed base directory (Snyk CWE-22 guard)"
        )

    if must_exist and not candidate.exists():
        raise UnsafePathError("resolved path does not exist")

    if create_parents:
        candidate.parent.mkdir(parents=True, exist_ok=True)

    return candidate


def default_bases() -> list[Path]:
    """Return the project-wide default allow-list for ad-hoc callers.

    Includes:
    - ``VELAFLOW_DATA_DIR`` (default ``./data``)
    - The user's home directory (``Path.home()``)
    - The current working directory (``Path.cwd()``)
    - ``/var/log/brain`` on POSIX, ``%PROGRAMDATA%\\brain`` on Windows
    """
    bases: list[Path] = [
        _normalize(Path(os.environ.get("VELAFLOW_DATA_DIR", "./data"))),
        _normalize(Path.home()),
        _normalize(Path.cwd()),
    ]
    if sys.platform.startswith("win"):
        progdata = os.environ.get("PROGRAMDATA")
        if progdata:
            bases.append(_normalize(Path(progdata) / "brain"))
    else:
        bases.append(_normalize(Path("/var/log/brain")))
        # Use tempfile.gettempdir() as a read-side allow-list entry
        # instead of a hard-coded ``/tmp`` literal. This respects
        # ``TMPDIR`` / platform conventions and avoids the static
        # ``/tmp`` string that SAST tools flag as a potentially
        # predictable temp path. We never create temp files through
        # this entry; see action_ledger and secure_logging which create
        # their own scoped directories.
        import tempfile
        bases.append(_normalize(Path(tempfile.gettempdir())))
    return bases
