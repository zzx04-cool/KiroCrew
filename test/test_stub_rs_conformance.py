"""Conformance: the EXPERIMENTAL Rust stub behaves like ``kiro_crew.mcp_gateway.stub``.

``packages/kirocrew-stub-rs`` is an opt-in drop-in (``mcp_gateway.stub_impl``).
"Drop-in" is a claim about behaviour seen from OUTSIDE the process, so every
case here drives a real stub PROCESS -- the Python module and the Rust binary
side by side, through the same assertions -- against either a real gatewayd or a
scripted fake gateway on a unix socket:

* the Register payload (every PoolKey field), so the two implementations land
  in ONE pool instead of silently splitting it;
* the bridge round trip through a real gatewayd, carrying the session identity;
* control frames (``pong`` / ``keepalive`` / ``queued``) never reach kiro-cli;
* a frame far above asyncio's 64 KiB default passes intact;
* the degrade paths: no gateway, a stale code generation, a capacity refusal;
* a clean stdin EOF exits 0 and unregisters.

The Rust cases skip when the binary is not built (``cargo build --release`` in
``packages/kirocrew-stub-rs``), or run against ``KIROCREW_TEST_STUB_BIN``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest
from fake_pool_mcp_server import recorded

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import stub as pystub
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.hashing import encode_target_args
from kiro_crew.mcp_gateway.stub_impl import RUST_STUB_BIN_NAME, install_context_b64

pytestmark = [
    pytest.mark.xdist_group("mcp_gateway"),
    pytest.mark.skipif(sys.platform == "win32", reason="the Rust stub is POSIX-only"),
]

_REPO = Path(__file__).resolve().parent.parent
_RUST_BIN = Path(
    os.environ.get("KIROCREW_TEST_STUB_BIN")
    or _REPO / "packages" / "kirocrew-stub-rs" / "target" / "release" / RUST_STUB_BIN_NAME
)
_FAKE_SERVER = Path(__file__).with_name("fake_pool_mcp_server.py")
_TIMEOUT = 30.0

IMPLS = [
    "python",
    pytest.param(
        "rust",
        marks=pytest.mark.skipif(
            not (_RUST_BIN.is_file() and os.access(_RUST_BIN, os.X_OK)),
            reason=f"Rust stub not built at {_RUST_BIN}",
        ),
    ),
]


def _launcher(impl: str) -> list[str]:
    if impl == "python":
        return [sys.executable, "-m", "kiro_crew.mcp_gateway.stub"]
    return [str(_RUST_BIN)]


def _flags(impl: str, flags: list[str]) -> list[str]:
    """One envelope, exactly as the rewriter writes it (Rust gets its context)."""
    if impl == "rust":
        flags = [*flags, "--install-context-b64", install_context_b64()]
    return [f"--stub-flags-b64={encode_target_args(flags)}"]


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("KIROCREW_CHANNEL_ID", "KIROCREW_SESSION_KEY", "KIROCREW_STUB_SESSION_TOKEN")
        and not k.startswith("KIROCREW_MCP_")
    }
    env["KIROCREW_HOME"] = str(home)
    env.update(extra)
    return env


# --- 1. Register payload parity ----------------------------------------------

#: Per-process, so not comparable: a fresh uuid per registration, and the
#: ancestor chain of two different processes.
_PER_PROCESS = {"stub_uuid", "ancestor_pids"}


def _publish_token(home: Path, token: str, key: str) -> None:
    """Write a signed token mapping with the gateway's own MAC function."""
    from kiro_crew import session_token_sig as sig

    trust = home / "trust"
    trust.mkdir(parents=True, exist_ok=True)
    root = secrets.token_bytes(32)
    (trust / "sel_hmac.key").write_bytes(root)
    path = sig._sig_path(token, home)
    path.write_text(f"{sig._compute_sig(root, token, key)}\n{key}", encoding="utf-8")


def _rust_payload(flags: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
    out = subprocess.run(
        [str(_RUST_BIN), *_flags("rust", flags)],
        env={**env, "KIROCREW_STUB_RS_DUMP_REGISTER": "1"},
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


def _py_payload(flags: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
    """The Python stub's own payload, from a FRESH process like a real stub:
    an in-process call would see this test process's SEL singleton and memos."""
    code = (
        "import json, sys\n"
        "from kiro_crew.mcp_gateway import stub\n"
        "print(json.dumps(stub.build_register_payload(stub._parse_args(sys.argv[1:]))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, *_flags("python", flags)],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _cases(tmp: Path) -> dict[str, tuple[list[str], dict[str, str]]]:
    venv_kirocrew = Path(sys.executable).with_name("kirocrew")
    site = str(Path(pystub.__file__).resolve().parent.parent.parent)
    env_file = tmp / "declared.json"
    env_file.write_text(
        json.dumps(
            {
                "PYTHONPATH": f"{site}:/opt/extra",
                "AWS_SECRET_ACCESS_KEY": "rotating",
                "OAUTH_TOKEN": "never-hashed",
                "PLAIN": "v,with=commas",
                "NUMBER": 7,
            }
        ),
        encoding="utf-8",
    )
    token = "tok-" + secrets.token_hex(8)
    _publish_token(tmp / "home", token, "dashboard:token-session")
    return {
        "managed_control_plane": (
            [
                "--server", "kirocrew-core", "--agent", "kirocrew",
                "--target-command", str(venv_kirocrew),
                f"--target-args-b64={encode_target_args(['mcp-core'])}",
                "--sandbox-mode", "off", "--work-dir", str(tmp),
                "--approval-mode", "auto", "--poolable", "--trust-all",
                "--env-file", str(env_file),
                "--pool-identity-env-b64", encode_target_args(["AWS_SECRET_ACCESS_KEY"]),
                "--auto-approve", json.dumps(["b", "a,c"]),
                "--channel-id", "C123",
            ],
            {"KIROCREW_STUB_SESSION_TOKEN": token},
        ),
        "install_interpreter_app": (
            [
                "--server", "some-app", "--agent", "probe",
                "--target-command", sys.executable,
                f"--target-args-b64={encode_target_args(['-m', 'kiro_crew.apps.x', ''])}",
                "--work-dir", "relative/does/not/exist",
                "--env-json", json.dumps({"K": "V", "AWS_SESSION_TOKEN": "x"}),
            ],
            {"KIROCREW_SESSION_KEY": "cron:nightly", "KIROCREW_CHANNEL_ID": "D9"},
        ),
        "legacy_plain_flags": (
            [
                "--server", "third-party", "--agent", "a",
                "--target-command", "definitely-not-on-path",
                "--target-args", "x|y||z",
                "--work-dir", str(tmp),
                "--env", "A=1,B=2=3,broken",
                "--auto-approve", "t1,t2",
            ],
            {"KIROCREW_SESSION_KEY": "U123:thread"},
        ),
    }


@pytest.mark.skipif(not _RUST_BIN.is_file(), reason="Rust stub not built")
@pytest.mark.parametrize("case", ["managed_control_plane", "install_interpreter_app", "legacy_plain_flags"])
def test_register_payload_matches_python(case: str, tmp_path: Path) -> None:
    """Every PoolKey field equal, so a Rust and a Python stub share one pool."""
    (tmp_path / "home").mkdir()
    flags, extra = _cases(tmp_path)[case]
    env = _env(tmp_path / "home", **extra)
    py = _py_payload(flags, env, tmp_path)
    rs = _rust_payload(flags, env, tmp_path)
    for k in _PER_PROCESS:
        py.pop(k, None)
        rs.pop(k, None)
    assert rs == py
    if case == "managed_control_plane":
        # The ported identity rung really verified the signed token.
        assert rs["session_key"] == "dashboard:token-session"
        assert rs["stub_code_fingerprint"]


# --- 2. Real gatewayd round trip ----------------------------------------------


def _init(i: int) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "conformance", "version": "0"}},
            }
        )
        + "\n"
    ).encode()


def _call(i: int, name: str = "noop") -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": name, "arguments": {}}})
        + "\n"
    ).encode()


async def _reply(proc: asyncio.subprocess.Process, req_id: Any) -> dict:
    assert proc.stdout is not None

    async def _read() -> dict:
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise AssertionError(f"stub closed stdout before replying to {req_id!r}")
            msg = json.loads(line)
            if msg.get("id") == req_id:
                return msg

    return await asyncio.wait_for(_read(), timeout=_TIMEOUT)


async def _send(proc: asyncio.subprocess.Process, data: bytes) -> None:
    assert proc.stdin is not None
    proc.stdin.write(data)
    await proc.stdin.drain()


async def _spawn(impl: str, flags: list[str], env: dict[str, str], cwd: Path):
    return await asyncio.create_subprocess_exec(
        *_launcher(impl), *_flags(impl, flags),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=env,
        limit=64 * 1024 * 1024,
    )


async def _finish(proc: asyncio.subprocess.Process, timeout: float = 15.0) -> int:
    if proc.stdin is not None and not proc.stdin.is_closing():
        proc.stdin.close()
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AssertionError("stub did not exit on stdin EOF")


def _base_flags(sock: Path, wd: Path, server: str = "fake") -> list[str]:
    return [
        "--server", server, "--agent", "probe",
        "--target-command", sys.executable,
        # The fake backend needs its launch-log path when exec'd directly.
        "--target-args", f"{_FAKE_SERVER}|{wd / 'direct-launch.txt'}",
        "--work-dir", str(wd), "--socket", str(sock),
        "--sandbox-mode", "off", "--approval-mode", "auto", "--poolable",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_round_trip_through_a_real_gatewayd(impl: str, tmp_path: Path, short_sock_dir) -> None:
    sock = Path(short_sock_dir) / "gw.sock"
    home = tmp_path / "home"
    home.mkdir()
    launch_log, caller_log = tmp_path / "launch.txt", tmp_path / "callers.txt"

    def resolver(_key: object):
        return sys.executable, [str(_FAKE_SERVER), str(launch_log), str(caller_log)], {}, str(tmp_path)

    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=4, idle_timeout_secs=60,
                        stop_event=stop, target_resolver=resolver, prewarm_count=0)
    )
    try:
        for _ in range(200):
            if transport.endpoint_exists(sock):
                break
            await asyncio.sleep(0.05)
        proc = await _spawn(impl, _base_flags(sock, tmp_path),
                            _env(home, KIROCREW_SESSION_KEY=f"dashboard:{impl}"), tmp_path)
        await _send(proc, _init(1))
        assert "result" in await _reply(proc, 1)
        for i in range(2, 12):
            await _send(proc, _call(i))
            assert "result" in await _reply(proc, i)
        assert await _finish(proc) == 0
        assert recorded(caller_log) == [f"dashboard:{impl}"] * 10
        # Bridged, not degraded: no fallback record was written.
        assert not (home / "logs" / "stub_fallback.jsonl").exists()
    finally:
        stop.set()
        await asyncio.wait_for(daemon, timeout=60)


# --- 3. Scripted fake gateway ---------------------------------------------------

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, dict], Awaitable[None]]


async def _fake_gateway(sock: Path, handler: Handler) -> tuple[asyncio.AbstractServer, list[dict]]:
    frames: list[dict] = []

    async def _on_conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            line = await r.readline()
            reg = json.loads(line)
            frames.append(reg)
            await handler(r, w, reg)
        except (ConnectionError, json.JSONDecodeError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()

    server = await asyncio.start_unix_server(_on_conn, path=str(sock), limit=64 * 1024 * 1024)
    return server, frames


def _w(w: asyncio.StreamWriter, obj: dict) -> None:
    w.write(json.dumps(obj).encode() + b"\n")


async def _echo_loop(r: asyncio.StreamReader, w: asyncio.StreamWriter, seen: list[dict]) -> None:
    """Answer each request; precede each answer with control frames."""
    while True:
        line = await r.readline()
        if not line:
            return
        msg = json.loads(line)
        seen.append(msg)
        if msg.get("type") == "ping":
            _w(w, {"type": "pong"})
        elif "method" in msg and "id" in msg:
            _w(w, {"type": "keepalive"})
            _w(w, {"type": "pong"})
            _w(w, {"type": "queued", "position": 1})
            if msg["method"] == "tools/call" and msg["params"]["name"] == "big":
                _w(w, {"jsonrpc": "2.0", "id": msg["id"],
                       "result": {"content": [{"type": "text", "text": "x" * (5 * 1024 * 1024)}]}})
            else:
                _w(w, {"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg["method"]}})
        await w.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_control_frames_are_swallowed_and_big_frames_pass(impl, tmp_path, short_sock_dir) -> None:
    sock = Path(short_sock_dir) / "fake.sock"
    seen: list[dict] = []

    async def handler(r, w, reg):
        _w(w, {"type": "registered", "capabilities": ["poolable_ack", "bridge_ping", "ensure_backend"]})
        await w.drain()
        ensure = json.loads(await r.readline())
        assert ensure["type"] == "ensure_backend"
        _w(w, {"type": "ready"})
        await w.drain()
        await _echo_loop(r, w, seen)

    server, regs = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        proc = await _spawn(impl, _base_flags(sock, tmp_path), _env(home), tmp_path)
        await _send(proc, _init(1))
        await _send(proc, _call(2, "big"))
        await _send(proc, b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await _send(proc, _call("s-3"))
        # Read EVERYTHING kiro-cli is handed, in order, until the last answer.
        out: list[dict] = []
        assert proc.stdout is not None
        while not out or out[-1].get("id") != "s-3":
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=_TIMEOUT)
            assert line, "stub closed stdout early"
            out.append(json.loads(line))
        assert [m.get("id") for m in out] == [1, 2, "s-3"], "a control frame leaked to kiro-cli"
        assert len(out[1]["result"]["content"][0]["text"]) == 5 * 1024 * 1024
        assert await _finish(proc) == 0
        assert regs[0]["type"] == "register" and regs[0]["poolable"] is True
        # Forwarded verbatim, then the clean-EOF unregister.
        assert [m.get("method") for m in seen if "method" in m] == [
            "initialize", "tools/call", "notifications/initialized", "tools/call"]
        assert seen[-1] == {"type": "unregister"}
    finally:
        server.close()


def _fallback_records(home: Path) -> list[dict]:
    log = home / "logs" / "stub_fallback.jsonl"
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]


async def _assert_direct_backend(proc) -> None:
    """The real backend answers: the stub exec'd it with initialize unread."""
    await _send(proc, _init(7))
    reply = await _reply(proc, 7)
    assert reply["result"]["serverInfo"]["name"] == "fake-pool-mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_no_gateway_degrades_to_the_real_backend(impl, tmp_path, short_sock_dir) -> None:
    home = tmp_path / "home"
    home.mkdir()
    sock = Path(short_sock_dir) / "absent.sock"
    proc = await _spawn(impl, _base_flags(sock, tmp_path), _env(home), tmp_path)
    await _assert_direct_backend(proc)
    await _finish(proc)
    recs = _fallback_records(home)
    assert len(recs) == 1
    assert recs[0]["reason"].startswith("connect failed:")
    assert recs[0]["terminal"] is False and recs[0]["server"] == "fake"
    assert oct((home / "logs" / "stub_fallback.jsonl").stat().st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_stale_code_generation_degrades(impl, tmp_path, short_sock_dir) -> None:
    """A managed server behind a daemon of another generation execs directly."""
    sock = Path(short_sock_dir) / "stale.sock"

    async def handler(r, w, reg):
        assert reg.get("stub_code_fingerprint")
        _w(w, {"type": "registered", "fingerprint": "some-other-generation",
               "capabilities": ["poolable_ack"]})
        await w.drain()
        await r.read()

    server, _ = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        flags = _base_flags(sock, tmp_path, server="kirocrew-core")
        # A managed subcommand in argv is what folds in the code fingerprint.
        flags[flags.index("--target-args") + 1] += "|mcp-core"
        proc = await _spawn(impl, flags, _env(home), tmp_path)
        await _assert_direct_backend(proc)
        await _finish(proc)
        assert "fingerprint does not match" in _fallback_records(home)[0]["reason"]
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_capacity_refusal_answers_typed_errors_and_stays_open(impl, tmp_path, short_sock_dir) -> None:
    sock = Path(short_sock_dir) / "cap.sock"

    async def handler(r, w, reg):
        _w(w, {"type": "registered", "capabilities": ["poolable_ack", "ensure_backend", "spawn_queue"]})
        await w.drain()
        ensure = json.loads(await r.readline())
        assert ensure["wait_budget_secs"] > 0
        _w(w, {"type": "queued", "position": 3})
        _w(w, {"type": "rejected", "class": "capacity", "reason": "host full", "retry_after_secs": 42})
        await w.drain()

    server, _ = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        proc = await _spawn(impl, _base_flags(sock, tmp_path), _env(home), tmp_path)
        for i in (1, 2):
            await _send(proc, _init(i) if i == 1 else _call(i))
            err = (await _reply(proc, i))["error"]
            assert err["code"] == -32001
            assert err["data"] == {"class": "capacity", "retry_after_secs": 42}
        assert proc.returncode is None, "a capacity refusal must keep the transport open"
        assert await _finish(proc) == 0
        rec = _fallback_records(home)[0]
        assert rec["reason"] == "capacity:host full" and rec["terminal"] is True
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_terminal_rejection_exits_without_exec(impl, tmp_path, short_sock_dir) -> None:
    sock = Path(short_sock_dir) / "term.sock"

    async def handler(r, w, reg):
        _w(w, {"type": "registered", "capabilities": ["poolable_ack", "ensure_backend"]})
        await w.drain()
        await r.readline()
        _w(w, {"type": "rejected", "reason": "spawn failed: boom"})
        await w.drain()

    server, _ = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        proc = await _spawn(impl, _base_flags(sock, tmp_path), _env(home), tmp_path)
        assert await asyncio.wait_for(proc.wait(), timeout=_TIMEOUT) == 1
        rec = _fallback_records(home)[0]
        assert rec["reason"] == "terminal:spawn failed: boom" and rec["terminal"] is True
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_untagged_unknown_target_falls_back(impl, tmp_path, short_sock_dir) -> None:
    sock = Path(short_sock_dir) / "unk.sock"

    async def handler(r, w, reg):
        _w(w, {"type": "registered", "capabilities": ["poolable_ack", "ensure_backend"]})
        await w.drain()
        await r.readline()
        _w(w, {"type": "rejected", "reason": "no target mapping for server 'fake'; set ..."})
        await w.drain()

    server, _ = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        proc = await _spawn(impl, _base_flags(sock, tmp_path), _env(home), tmp_path)
        await _assert_direct_backend(proc)
        await _finish(proc)
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("impl", IMPLS)
async def test_unshareable_server_refuses_a_daemon_without_poolable_ack(impl, tmp_path, short_sock_dir) -> None:
    sock = Path(short_sock_dir) / "ack.sock"

    async def handler(r, w, reg):
        assert reg["poolable"] is False
        _w(w, {"type": "registered", "capabilities": []})
        await w.drain()
        await r.read()

    server, _ = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        flags = [f for f in _base_flags(sock, tmp_path) if f != "--poolable"]
        proc = await _spawn(impl, flags, _env(home), tmp_path)
        await _assert_direct_backend(proc)
        await _finish(proc)
        assert _fallback_records(home)[0]["reason"] == "gateway does not honour the poolable field"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_rust_liveness_fails_a_call_to_a_silent_peer(tmp_path, short_sock_dir) -> None:
    """Rust-only (the Python intervals are not tunable from outside the process):
    a peer that never pongs is declared dead after 3 misses, and the in-flight
    call is answered with the retryable -32603 rather than left hanging."""
    if not _RUST_BIN.is_file():
        pytest.skip("Rust stub not built")
    sock = Path(short_sock_dir) / "silent.sock"
    pings: list[int] = []

    async def handler(r, w, reg):
        _w(w, {"type": "registered", "capabilities": ["poolable_ack", "bridge_ping"]})
        await w.drain()
        while True:
            line = await r.readline()
            if not line:
                return
            msg = json.loads(line)
            if msg.get("type") == "ping":
                pings.append(1)
            elif msg.get("method") == "initialize":
                _w(w, {"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
                await w.drain()

    server, _ = await _fake_gateway(sock, handler)
    try:
        home = tmp_path / "home"
        home.mkdir()
        env = _env(home, KIROCREW_STUB_RS_PING_INTERVAL="0.2", KIROCREW_STUB_RS_RECONNECT_BUDGET="0.5")
        proc = await _spawn("rust", _base_flags(sock, tmp_path), env, tmp_path)
        await _send(proc, _init(1))
        await _reply(proc, 1)
        await _send(proc, _call(2))
        err = (await _reply(proc, 2))["error"]
        assert err["code"] == -32603 and "Retry it" in err["message"]
        assert len(pings) >= 3
        await _finish(proc)
    finally:
        server.close()


def test_rewriter_launches_the_rust_stub_only_when_opted_in(tmp_path, monkeypatch) -> None:
    """Default stays Python; the opt-in swaps only the launch line."""
    from kiro_crew.mcp_gateway import stub_impl

    fake_bin = tmp_path / RUST_STUB_BIN_NAME
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.delenv("KIROCREW_MCP_STUB_IMPL", raising=False)
    monkeypatch.setattr(stub_impl, "_raw_gateway_section", lambda: {})
    assert stub_impl.rust_stub_binary() is None
    monkeypatch.setenv("KIROCREW_MCP_STUB_IMPL", "rust")
    monkeypatch.setenv("KIROCREW_MCP_STUB_RS_BIN", str(fake_bin))
    assert stub_impl.rust_stub_binary() == str(fake_bin)
    monkeypatch.setenv("KIROCREW_MCP_STUB_RS_BIN", str(tmp_path / "missing"))
    assert stub_impl.rust_stub_binary() is None, "a missing binary must keep the Python stub"
    monkeypatch.delenv("KIROCREW_MCP_STUB_IMPL")
    monkeypatch.delenv("KIROCREW_MCP_STUB_RS_BIN")
    monkeypatch.setattr(stub_impl, "_raw_gateway_section",
                        lambda: {"stub_impl": "rust", "stub_rs_binary": str(fake_bin)})
    assert stub_impl.rust_stub_binary() == str(fake_bin)
    ctx = json.loads(base64.urlsafe_b64decode(stub_impl.install_context_b64()))
    assert ctx["interpreter"] == sys.executable
