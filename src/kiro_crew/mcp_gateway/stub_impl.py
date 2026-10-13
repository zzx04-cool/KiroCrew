"""EXPERIMENTAL opt-in: launch the Rust port of the stub instead of the Python one.

``packages/kirocrew-stub-rs`` builds ``kirocrew-mcp-stub-rs``, a drop-in for
``python -m kiro_crew.mcp_gateway.stub`` that speaks the same Register / bridge /
reconnect / fallback protocol to gatewayd. The Python stub stays the default;
the Rust one is chosen only when the operator asks for it:

* ``KIROCREW_MCP_STUB_IMPL=rust`` (env, wins), or
* ``mcp_gateway.stub_impl: "rust"`` in ``config.json``.

The binary is ``KIROCREW_MCP_STUB_RS_BIN``, else ``mcp_gateway.stub_rs_binary``,
else the crate's own release build inside this checkout. A missing or
non-executable binary keeps the Python stub and logs why, so the opt-in can
never cost a session its servers.

The config keys are read RAW (``_raw_config``) rather than declared on
``McpGatewayConfig``: this is an experiment, and a declared field would put it
on the Settings surface and in the config baseline.

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
_BIN_ENV = "KIROCREW_MCP_STUB_RS_BIN"
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
    """The Rust stub to launch, or ``None`` to keep the Python stub."""
    if sys.platform == "win32":
        return None
    section = _raw_gateway_section()
    impl = (os.environ.get(_IMPL_ENV) or str(section.get("stub_impl") or "python")).strip().lower()
    if impl != "rust":
        return None
    raw = os.environ.get(_BIN_ENV) or str(section.get("stub_rs_binary") or "")
    path = Path(raw).expanduser() if raw else _default_binary()
    if not (path.is_file() and os.access(path, os.X_OK)):
        _warn_once(
            f"mcp_gateway.stub_impl=rust but {path} is not an executable file; "
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
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            dist = version("kiro-crew")
        except PackageNotFoundError:
            dist = "unknown"
    except Exception:  # pragma: no cover - importlib.metadata is stdlib
        dist = "unknown"
    from kiro_crew.code_fingerprint import _PACKAGE_ROOT
    from kiro_crew.mcp_gateway.hashing import _package_dir

    ctx = {
        "interpreter": sys.executable,
        # The two spellings the Python stub uses: ``hashing`` takes abspath,
        # ``code_fingerprint`` takes resolve(); they differ under a symlink.
        "package_dir": _package_dir(),
        "fingerprint_root": str(_PACKAGE_ROOT),
        "dist_version": dist,
    }
    raw = json.dumps(ctx, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")
