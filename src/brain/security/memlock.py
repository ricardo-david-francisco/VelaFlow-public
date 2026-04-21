"""Best-effort memory hardening for processes that hold credentials in RAM.

On Linux, call :func:`lock_process_memory()` once at startup. It invokes
``mlockall(MCL_CURRENT | MCL_FUTURE)`` via ``ctypes`` so decrypted
credentials cannot be paged to swap (which would leave plaintext on
disk even after a reboot). Failures (non-Linux, insufficient
``RLIMIT_MEMLOCK``, missing libc) are **logged but not raised** — the
function is strictly defence-in-depth.

No secrets or pointers are logged; the function's log output is a
single line at INFO or WARNING.
"""

from __future__ import annotations

import logging
import platform
from typing import Final

logger = logging.getLogger(__name__)

_MCL_CURRENT: Final[int] = 1
_MCL_FUTURE: Final[int] = 2
_MCL_ONFAULT: Final[int] = 4


def lock_process_memory() -> bool:
    """Lock the process's current and future pages in RAM.

    Returns:
        True if mlockall succeeded; False on any error or on non-Linux.
    """
    if platform.system() != "Linux":
        logger.info("memory-lock skipped: not Linux (%s)", platform.system())
        return False
    try:
        import ctypes  # local — avoid ctypes cost on Windows dev machines

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        flags = _MCL_CURRENT | _MCL_FUTURE
        rc = libc.mlockall(flags)
        if rc != 0:
            errno = ctypes.get_errno()
            logger.warning(
                "mlockall failed (errno=%d); raise LimitMEMLOCK=infinity in "
                "the systemd unit to enable swap-prevention for decrypted "
                "credentials",
                errno,
            )
            return False
    except (OSError, AttributeError) as exc:
        logger.warning("mlockall not available: %s", exc)
        return False
    # Best-effort: also raise the dump limit to 0 so the kernel will not
    # write a core file that would contain decrypted credentials.
    try:
        import resource  # Linux-only; guarded by the platform check above

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError, ImportError) as exc:
        logger.debug("RLIMIT_CORE tightening skipped: %s", exc)
    logger.info("memory-lock active (mlockall MCL_CURRENT|MCL_FUTURE)")
    return True
