"""Launch the Rust port of the stub when it is built; else the Python one.

``packages/kirocrew-stub-rs`` builds ``kirocrew-mcp-stub-rs``, a drop-in for
``python -m kiro_crew.mcp_gateway.stub`` that speaks the same Register / bridge /
reconnect / fallback protocol to gatewayd. It uses about a tenth of the Python
stub's memory per session. Selection:

* ``KIROCREW_MCP_STUB_IMPL`` (env, wins), else ``mcp_gateway.stub_impl`` in
  ``config.json``: ``auto`` (default), ``rust`` or ``python``.
* ``auto`` takes the Rust stub when its binary exists and is executable and
  silently keeps the Python stub otherwise; ``python`` always keeps it.

Build: ``cargo build --release --locked`` in ``packages/kirocrew-stub-rs``.
The binary is the crate's own release build inside this checkout. A missing or
non-executable binary keeps the Python stub, so the Rust stub can never cost a
session its servers.

The Rust stub hands itself to the Python stub (same flags, env and stdio) for
what it does not port: a stub without a verified session token, and the
direct-exec fallback while the gateway cannot serve.

The config key is read RAW (``_raw_config``) rather than declared on
``McpGatewayConfig``: it is an operator escape hatch, not a Settings control.

POSIX only: the Rust port has no named-pipe transport.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Executable name of the Rust stub, and therefore the cmdline substring that
#: identifies one (the Python stub is identified by ``STUB_MODULE``).
RUST_STUB_BIN_NAME = "kirocrew-mcp-stub-rs"

_IMPL_ENV = "KIROCREW_MCP_STUB_IMPL"
_PACKAGE_DIR = Path(__file__).resolve().parent.parent

_warned: set[str] = set()


def _raw_gateway_section() -> dict:
    try:
        # circular import: config.loader imports the rewriter at module top.
        from kiro_crew.config.loader import _raw_config

        section = _raw_config().get("mcp_gateway") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _default_binary() -> Path:
    # <checkout>/src/kiro_crew -> <checkout>/packages/kirocrew-stub-rs/...
    return (
        _PACKAGE_DIR.parent.parent
        / "packages"
        / "kirocrew-stub-rs"
        / "target"
        / "release"
        / RUST_STUB_BIN_NAME
    )


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)


def rust_stub_binary() -> Optional[str]:
    """The Rust stub to launch, or ``None`` to keep the Python stub.

    ``auto`` (the default) uses the Rust stub whenever its binary is present
    and executable and otherwise keeps the Python stub without a warning: an
    install without the built binary is normal. ``rust`` asks for it
    explicitly, so a missing binary is logged. ``python`` never uses it.
    """
    if sys.platform == "win32":
        return None
    section = _raw_gateway_section()
    impl = (os.environ.get(_IMPL_ENV) or str(section.get("stub_impl") or "auto")).strip().lower()
    if impl not in ("rust", "auto"):
        return None
    path = _default_binary()
    if not (path.is_file() and os.access(path, os.X_OK)):
        if impl == "rust":
            _warn_once(
                f"mcp_gateway.stub_impl={impl} but {path} is not an executable file; "
                "keeping the Python stub"
            )
        return None
    return str(path)


def install_context_b64() -> str:
    """What the Python stub reads from its own interpreter, for the Rust one.

    ``hashing.install_aliases`` and ``code_fingerprint`` derive from
    ``sys.executable`` and the package location of the process that runs them.
    The Rust stub has neither, so the rewriter hands it this process's values:
    the same ones a Python stub launched from this overlay would compute.

    ``python_argv`` is the interpreter prefix the rewriter would launch a Python
    stub with. The Rust stub execs into the Python one through it for what the
    port leaves to Python: a stub without a verified session token, and the
    direct-exec fallback while the gateway cannot serve.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            dist = version("kiro-crew")
        except PackageNotFoundError:
            dist = "unknown"
    except Exception:  # pragma: no cover - importlib.metadata is stdlib
        dist = "unknown"
    from kiro_crew import platform_compat
    from kiro_crew.code_fingerprint import _PACKAGE_ROOT
    from kiro_crew.mcp_gateway.hashing import _package_dir

    ctx = {
        "interpreter": sys.executable,
        # The two spellings the Python stub uses: ``hashing`` takes abspath,
        # ``code_fingerprint`` takes resolve(); they differ under a symlink.
        "package_dir": _package_dir(),
        "fingerprint_root": str(_PACKAGE_ROOT),
        "dist_version": dist,
        "python_argv": platform_compat.isolated_python_argv(),
    }
    raw = json.dumps(ctx, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")
