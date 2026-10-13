# kirocrew-stub-rs (EXPERIMENTAL)

A Rust port of `kiro_crew.mcp_gateway.stub`, the shim kiro-cli runs for each stubbed MCP server. It is opt-in, and the Python stub stays the default.

## Build and opt in

```bash
cd packages/kirocrew-stub-rs && cargo build --release
# then either:
export KIROCREW_MCP_STUB_IMPL=rust                  # env, wins
# or in config.json:  "mcp_gateway": {"stub_impl": "rust"}
# optional binary path: KIROCREW_MCP_STUB_RS_BIN / mcp_gateway.stub_rs_binary
```

If the binary is missing, the Python stub runs. This works on POSIX only.

## Tests

```bash
cargo test
.venv/bin/python -m pytest test/test_stub_rs_conformance.py
KIROCREW_TEST_STUB_BIN=$PWD/packages/kirocrew-stub-rs/target/release/kirocrew-mcp-stub-rs \
  .venv/bin/python -m pytest test/test_stub_broker_reconnect.py::test_session_survives_a_broker_restart
```

## Measured (Linux, isolated pod, 3 sessions x kirocrew-core + kirocrew-guide)

| | Python | Rust |
|---|---|---|
| RSS per stub | 29.3–29.9 MB | 3.0–3.6 MB |
| Stub RSS per session (2 stubs) | 59.3–59.4 MB | 6.6–6.7 MB |
| Start to `initialize` reply, p50 | 122–127 ms | 21–23 ms |
| `tools/list` p95, 6000 calls pooled | 1.37 ms | 1.23 ms |
| `learn_list` / `find_setting` p95 | 103.4–106.2 ms | 103.0–105.1 ms |

## Not ported

- Windows (named pipe transport, child-process fallback).
- Identity rung 4 (`session_pid_<pid>.txt` walk) and the shared-host-pid veto on rung 3. The gateway's peer check and claim-push still name the session.
- Keeping the session token on a control-plane fallback. The Rust stub always drops it.
- `KIROCREW_MCP_RECONNECTS` metric and the fallback-rate reader (`fallback_counts`, which gatewayd still runs from Python).
