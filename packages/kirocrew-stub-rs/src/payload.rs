//! Register payload: every PoolKey field, computed exactly as
//! ``stub.build_register_payload`` / ``hashing`` compute them.

use crate::args::{Args, crew_home};
use crate::log;
use crate::util::{WARNING, hex, py_strip, sha256_hex};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const KIROCREW_BIN_MCP_SERVERS: &[&str] = &[
    "kirocrew-cron",
    "kirocrew-core",
    "kirocrew-computer",
    "kirocrew-guide",
    "kirocrew-dashboard",
    "kirocrew-work",
    "kirocrew-crew-log",
    "kirocrew-debug",
    "kirocrew-panel",
];
const KIROCREW_MCP_SUBCOMMANDS: &[&str] = &[
    "mcp-core",
    "mcp-cron",
    "mcp-work",
    "mcp-computer",
    "mcp-dashboard",
    "mcp-crew-log",
    "mcp-debug",
    "mcp-panel",
    "mcp-guide",
];
pub const POOLING_REQUIRES_TENANT_NONCE: &[&str] = &["kirocrew-computer"];
pub const STUB_SESSION_TOKEN_ENV: &str = "KIROCREW_STUB_SESSION_TOKEN";
const BINARY_HASH_CAP_BYTES: u64 = 4 * 1024 * 1024;
const ANCESTOR_WALK_MAX: usize = 10;
const ENV_SCRUB_PREFIXES: &[&str] = &["AWS_SECRET", "AWS_SESSION", "OAUTH"];
const CONFIG_SNAPSHOT_PLACEHOLDER: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

// ---------------------------------------------------------------------------
// Install context: what the Python stub learns from its own interpreter.
// ---------------------------------------------------------------------------

#[derive(Debug, Default, Clone)]
pub struct InstallContext {
    /// ``sys.executable`` of the gateway that wrote the overlay.
    pub interpreter: String,
    /// Absolute path of the ``kiro_crew`` package directory.
    pub package_dir: String,
    /// ``code_fingerprint._PACKAGE_ROOT`` (resolved; may differ from the above).
    pub fingerprint_root: String,
    /// ``importlib.metadata.version("kiro-crew")`` or ``"unknown"``.
    pub dist_version: String,
    /// ``platform_compat.isolated_python_argv()``: the interpreter (plus its
    /// user-site flag) a Python stub from this overlay would run under. Empty
    /// when the overlay predates it; then nothing is handed over.
    pub python_argv: Vec<String>,
}

impl InstallContext {
    pub fn from_args(a: &Args) -> Option<Self> {
        let v = crate::args::decode_b64_json(a.install_context_b64.as_deref()?)?;
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let package_dir = s("package_dir");
        let root = s("fingerprint_root");
        Some(Self {
            interpreter: s("interpreter"),
            fingerprint_root: if root.is_empty() {
                package_dir.clone()
            } else {
                root
            },
            package_dir,
            dist_version: s("dist_version"),
            python_argv: v
                .get("python_argv")
                .and_then(Value::as_array)
                .map(|xs| {
                    xs.iter()
                        .filter_map(|x| x.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default(),
        })
    }

    /// ``hashing.install_aliases`` (POSIX: no 8.3 long-name spellings).
    fn alias(&self, token: &str) -> Option<&'static str> {
        let mut out = None;
        if !self.interpreter.is_empty() && token == self.interpreter {
            out = Some("interpreter");
        }
        if !self.package_dir.is_empty() {
            let deps = format!("{}/apps/deps_boot", self.package_dir);
            if token == format!("{deps}.py") || token == format!("{deps}.pyc") {
                out = Some("deps-boot");
            }
            if let Some(parent) = Path::new(&self.package_dir).parent()
                && token == parent.to_string_lossy()
            {
                out = Some("site-packages");
            }
        }
        out
    }
}

/// ``hashing.launch_token_bytes``.
fn launch_token_bytes(ctx: Option<&InstallContext>, token: &str, role: &str) -> Vec<u8> {
    match ctx.and_then(|c| c.alias(token)) {
        Some(r) if r == role => {
            let mut v = vec![0xffu8];
            v.extend_from_slice(b"kirocrew-install:");
            v.extend_from_slice(role.as_bytes());
            v.push(0xff);
            v
        }
        _ => token.as_bytes().to_vec(),
    }
}

/// ``hashing.hash_command``.
pub fn hash_command(ctx: Option<&InstallContext>, command: &str, args: &[String]) -> String {
    let mut h = Sha256::new();
    h.update(launch_token_bytes(ctx, command, "interpreter"));
    h.update(b"\0");
    for a in args {
        h.update(launch_token_bytes(ctx, a, "deps-boot"));
        h.update(b"\0");
    }
    hex(&h.finalize())
}

/// ``hashing.hash_effective_env`` over ``non_secret_env``.
pub fn hash_effective_env(
    ctx: Option<&InstallContext>,
    env: &BTreeMap<String, String>,
    identity_keys: &[String],
) -> String {
    let mut h = Sha256::new();
    // BTreeMap iterates in byte order == Python's code-point order for str.
    for (k, v) in env {
        let secret = ENV_SCRUB_PREFIXES.iter().any(|p| k.starts_with(p));
        if secret && !identity_keys.iter().any(|n| n == k) {
            continue;
        }
        h.update(k.as_bytes());
        h.update(b"=");
        if k == "PYTHONPATH" {
            let parts: Vec<Vec<u8>> = v
                .split(':')
                .map(|seg| launch_token_bytes(ctx, seg, "site-packages"))
                .collect();
            h.update(parts.join(&b':'));
        } else {
            h.update(v.as_bytes());
        }
        h.update(b"\0");
    }
    hex(&h.finalize())
}

/// ``hashing.runs_install_code``.
fn runs_install_code(ctx: Option<&InstallContext>, command: &str, args: &[String]) -> bool {
    let Some(c) = ctx else { return false };
    if c.alias(command) != Some("interpreter") {
        return false;
    }
    let mut expect_module = false;
    for arg in args {
        if expect_module {
            return arg == "kiro_crew" || arg.starts_with("kiro_crew.");
        }
        if arg == "-m" {
            expect_module = true;
            continue;
        }
        if arg.starts_with("-m") && arg.len() > 2 && !arg.starts_with("--") {
            let m = &arg[2..];
            return m == "kiro_crew" || m.starts_with("kiro_crew.");
        }
        if c.alias(arg) == Some("deps-boot") {
            return true;
        }
    }
    false
}

// ---------------------------------------------------------------------------
// Binary identity and the Kiro Crew code fingerprint.
// ---------------------------------------------------------------------------

fn is_exec_file(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p)
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

/// ``shutil.which`` against this process's PATH.
pub fn which(cmd: &str, path_env: Option<&str>) -> Option<PathBuf> {
    if cmd.contains('/') {
        let p = PathBuf::from(cmd);
        return is_exec_file(&p).then_some(p);
    }
    let path = path_env
        .map(str::to_owned)
        .or_else(|| std::env::var("PATH").ok())
        .unwrap_or_default();
    for dir in path.split(':') {
        let dir = if dir.is_empty() { "." } else { dir };
        let cand = Path::new(dir).join(cmd);
        if is_exec_file(&cand) {
            return Some(cand);
        }
    }
    None
}

/// ``stub._binary_version``.
fn binary_version(command: &str) -> String {
    let start = which(command, None).unwrap_or_else(|| PathBuf::from(command));
    let Ok(real) = std::fs::canonicalize(&start) else {
        return "unknown".into();
    };
    let Ok(md) = std::fs::metadata(&real) else {
        return "unknown".into();
    };
    if !md.is_file() {
        return "unknown".into();
    }
    if md.len() > BINARY_HASH_CAP_BYTES {
        use std::os::unix::fs::MetadataExt;
        return format!("sz{}-mt{}", md.len(), md.mtime());
    }
    let Ok(mut f) = std::fs::File::open(&real) else {
        return "unknown".into();
    };
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        match f.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => h.update(&buf[..n]),
            Err(_) => return "unknown".into(),
        }
    }
    hex(&h.finalize())[..24].to_string()
}

const TRUSTED_SYSTEM_BIN_DIRS: &[&str] = &[
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    "/run/current-system/sw/bin",
];

fn trusted_git() -> Option<PathBuf> {
    TRUSTED_SYSTEM_BIN_DIRS
        .iter()
        .map(|d| Path::new(d).join("git"))
        .find(|p| is_exec_file(p))
}

/// Run with a hard wall-clock bound, like ``subprocess.run(timeout=5)``.
fn run_bounded(cmd: &mut Command, timeout: Duration) -> Option<(i32, Vec<u8>)> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut out = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut v = Vec::new();
        let _ = out.read_to_end(&mut v);
        v
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(2)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let data = reader.join().ok()?;
    Some((status.code().unwrap_or(-1), data))
}

/// ``code_fingerprint._git_fingerprint``.
fn git_fingerprint(root: &str) -> Option<String> {
    let git = trusted_git()?;
    let mk = |extra: &[&str]| {
        let mut c = Command::new(&git);
        c.args(["-C", root, "-c", "core.pager=cat"]).args(extra);
        for (k, _) in std::env::vars_os() {
            if k.to_string_lossy().starts_with("GIT_") {
                c.env_remove(&k);
            }
        }
        c.env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_TERMINAL_PROMPT", "0");
        c
    };
    let (rc, out) = run_bounded(&mut mk(&["rev-parse", "HEAD"]), Duration::from_secs(5))?;
    let sha = py_strip(&String::from_utf8_lossy(&out)).to_string();
    if rc != 0 || sha.is_empty() {
        return None;
    }
    let (rc, diff) = run_bounded(
        &mut mk(&[
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "HEAD",
            "--",
            root,
        ]),
        Duration::from_secs(5),
    )?;
    if rc != 0 {
        return None;
    }
    if diff.is_empty() {
        return Some(sha);
    }
    Some(format!("{sha}+{}", &sha256_hex(&diff)[..16]))
}

/// ``code_fingerprint._mtime_fingerprint``.
fn mtime_fingerprint(root: &str, dist: &str) -> String {
    use std::os::unix::fs::MetadataExt;
    let mut newest: i128 = 0;
    let mut walked = 0usize;
    let mut stack = vec![PathBuf::from(root)];
    'outer: while let Some(dir) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&dir) else {
            continue;
        };
        for e in rd.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            let name = e.file_name();
            let name = name.to_string_lossy();
            if ft.is_dir() {
                if name != "__pycache__" {
                    stack.push(e.path());
                }
            } else if name.ends_with(".py") {
                walked += 1;
                if walked > 20_000 {
                    break 'outer;
                }
                if let Ok(md) = std::fs::metadata(e.path()) {
                    let ns = md.mtime() as i128 * 1_000_000_000 + md.mtime_nsec() as i128;
                    newest = newest.max(ns);
                }
            }
        }
    }
    let dist = if dist.is_empty() { "unknown" } else { dist };
    format!(
        "mtime:{}",
        &sha256_hex(format!("{dist}|{newest}").as_bytes())[..24]
    )
}

fn code_fingerprint(ctx: Option<&InstallContext>) -> String {
    match ctx {
        Some(c) if !c.fingerprint_root.is_empty() => git_fingerprint(&c.fingerprint_root)
            .unwrap_or_else(|| mtime_fingerprint(&c.fingerprint_root, &c.dist_version)),
        // No context: nothing to fingerprint. Folded in as a constant so the
        // pool still splits from a Python stub rather than silently sharing.
        _ => "unknown-install".into(),
    }
}

/// ``stub._pool_binary_identity``.
fn pool_binary_identity(
    ctx: Option<&InstallContext>,
    command: &str,
    args: &[String],
) -> (String, String) {
    let base = binary_version(command);
    let managed = args
        .iter()
        .any(|a| KIROCREW_MCP_SUBCOMMANDS.contains(&a.as_str()));
    if !managed && !runs_install_code(ctx, command, args) {
        return (base, String::new());
    }
    let fp = code_fingerprint(ctx);
    (
        format!("{base}+{fp}"),
        if managed { fp } else { String::new() },
    )
}

// ---------------------------------------------------------------------------
// Env / approval parsing.
// ---------------------------------------------------------------------------

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

pub fn parse_env_json(raw: &str) -> BTreeMap<String, String> {
    if raw.is_empty() {
        return BTreeMap::new();
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(m)) => m
            .into_iter()
            .filter(|(k, _)| !k.is_empty())
            .map(|(k, v)| (k, py_str(&v)))
            .collect(),
        Ok(_) => {
            log!(
                WARNING,
                "stub --env-json not a JSON object; dropping env block"
            );
            BTreeMap::new()
        }
        Err(_) => {
            log!(WARNING, "stub --env-json malformed; dropping env block");
            BTreeMap::new()
        }
    }
}

pub fn parse_env_file(path: &str) -> BTreeMap<String, String> {
    if path.is_empty() {
        return BTreeMap::new();
    }
    match std::fs::read_to_string(path) {
        Ok(s) => parse_env_json(&s),
        Err(_) => {
            log!(WARNING, "stub --env-file unreadable; dropping env block");
            BTreeMap::new()
        }
    }
}

fn parse_env_csv(raw: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    if raw.is_empty() {
        return out;
    }
    for pair in raw.split(',') {
        if let Some((k, v)) = pair.split_once('=')
            && !k.is_empty()
        {
            out.insert(k.to_string(), v.to_string());
        }
    }
    out
}

fn parse_auto_approve(raw: &str) -> Vec<String> {
    if raw.is_empty() {
        return Vec::new();
    }
    if let Ok(Value::Array(a)) = serde_json::from_str::<Value>(raw) {
        return a
            .iter()
            .filter(|v| match v {
                Value::Null | Value::Bool(false) => false,
                Value::String(s) => !s.is_empty(),
                Value::Number(n) => n.as_f64() != Some(0.0),
                Value::Array(x) => !x.is_empty(),
                Value::Object(x) => !x.is_empty(),
                _ => true,
            })
            .map(py_str)
            .collect();
    }
    raw.split(',')
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

fn hash_permission_profile(tools: &[String], mode: &str, trust_all: bool) -> String {
    let mut sorted = tools.to_vec();
    sorted.sort();
    let mut h = Sha256::new();
    for t in &sorted {
        h.update(t.as_bytes());
        h.update(b"\0");
    }
    h.update(b"mode=");
    h.update(mode.as_bytes());
    h.update(b"\0trust_all=");
    h.update(if trust_all { b"1" } else { b"0" });
    hex(&h.finalize())
}

// ---------------------------------------------------------------------------
// Process tree and caller identity.
// ---------------------------------------------------------------------------

fn parent_pid(pid: i64) -> i64 {
    let Ok(s) = std::fs::read_to_string(format!("/proc/{pid}/status")) else {
        return 0;
    };
    s.lines()
        .find_map(|l| l.strip_prefix("PPid:"))
        .and_then(|v| v.trim().parse::<i64>().ok())
        .filter(|p| *p > 0)
        .unwrap_or(0)
}

/// ``stub._ancestor_pids``.
fn ancestor_pids() -> Vec<i64> {
    let mut chain = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut pid = unsafe { libc::getppid() } as i64;
    while pid > 1 && !seen.contains(&pid) && chain.len() < ANCESTOR_WALK_MAX {
        chain.push(pid);
        seen.insert(pid);
        pid = parent_pid(pid);
    }
    chain
}

/// ``config.paths.config_dir`` without its mkdir maintenance.
fn config_dir() -> PathBuf {
    crew_home()
}

/// ``session_pid_sig._read_regular_nofollow``: O_NOFOLLOW, regular, <= 4096 B.
fn read_regular_nofollow(path: &Path) -> Option<String> {
    use std::os::unix::fs::OpenOptionsExt;
    const MAX: u64 = 4096;
    let f = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .ok()?;
    let md = f.metadata().ok()?;
    if !md.is_file() || md.len() > MAX {
        return None;
    }
    let mut data = Vec::new();
    f.take(MAX + 1).read_to_end(&mut data).ok()?;
    if data.len() as u64 > MAX {
        return None;
    }
    String::from_utf8(data).ok()
}

/// ``session_token_sig.verify_session_token``.
fn verify_session_token(token: &str) -> String {
    use hmac::{Hmac, Mac};
    type HmacSha256 = Hmac<Sha256>;
    if token.is_empty() {
        return String::new();
    }
    let cfg = config_dir();
    let path = cfg.join(format!(
        "session_token_{}.sig",
        sha256_hex(token.as_bytes())
    ));
    let Some(raw) = read_regular_nofollow(&path) else {
        return String::new();
    };
    let (mac, body) = match raw.split_once('\n') {
        Some((m, b)) => (py_strip(m), py_strip(b)),
        None => (py_strip(&raw), ""),
    };
    if mac.is_empty() || body.is_empty() {
        return String::new();
    }
    let key = std::fs::read(cfg.join("trust").join("sel_hmac.key")).unwrap_or_default();
    if key.len() < 32 {
        log!(
            WARNING,
            "SEL trust-root key absent/short — refusing session-token identity"
        );
        return String::new();
    }
    let Ok(mut sub) = HmacSha256::new_from_slice(&key) else {
        return String::new();
    };
    sub.update(b"kirocrew.session_token.sig.v1");
    let subkey = sub.finalize().into_bytes();
    let Ok(mut m) = HmacSha256::new_from_slice(&subkey) else {
        return String::new();
    };
    m.update(format!("{token}:{body}").as_bytes());
    let expected = hex(&m.finalize().into_bytes());
    // Constant-time compare over bytes.
    let (a, b) = (expected.as_bytes(), mac.as_bytes());
    if a.len() != b.len() || a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) != 0 {
        log!(
            WARNING,
            "identity mapping signature mismatch — refusing identity"
        );
        return String::new();
    }
    body.to_string()
}

/// ``CallerContext.from_env().session_key``, rungs 2 and 3.
///
/// NOT ported: rung 1 (the protected member binding: the shipped hook always
/// answers ``None``), the ``host_pid_is_shared`` co-tenancy veto on rung 3,
/// and rung 4 (the ``session_pid_<pid>.txt`` walk). ``main`` therefore hands a
/// stub without a verified token to the Python stub before it gets here
/// (``handoff::run_python_stub``), so rung 3 is reached only when that
/// hand-over was impossible (an overlay that recorded no interpreter).
pub fn resolve_session_key() -> String {
    let k = verified_token_session_key();
    if !k.is_empty() {
        return k;
    }
    std::env::var("KIROCREW_SESSION_KEY").unwrap_or_default()
}

/// Rung 2 alone: the session the signed ``KIROCREW_STUB_SESSION_TOKEN`` names,
/// or ``""`` when there is no token or it does not verify.
pub fn verified_token_session_key() -> String {
    let token = std::env::var(STUB_SESSION_TOKEN_ENV).unwrap_or_default();
    if token.is_empty() {
        return String::new();
    }
    verify_session_token(&token)
}

pub fn resolve_channel_id(cli: Option<&str>) -> Option<String> {
    cli.filter(|s| !s.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            std::env::var("KIROCREW_CHANNEL_ID")
                .ok()
                .filter(|s| !s.is_empty())
        })
}

/// ``stub._build_caller_block``.
pub fn caller_block(channel_id: Option<&str>) -> Map<String, Value> {
    let session_key = resolve_session_key();
    let principal = std::env::var("USER")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("USERNAME").ok())
        .unwrap_or_default();
    let session_type = if session_key.starts_with("cron:") {
        "cron"
    } else if session_key.starts_with("hook:") {
        "hook"
    } else if session_key.starts_with("dashboard:") {
        "dashboard"
    } else if !session_key.is_empty() {
        "slack-thread"
    } else {
        "unknown"
    };
    let mut m = Map::new();
    m.insert("session_key".into(), json!(session_key));
    m.insert("session_type".into(), json!(session_type));
    m.insert("principal_id".into(), json!(principal));
    m.insert("channel_id".into(), json!(channel_id.unwrap_or("")));
    m
}

/// ``Path(p).resolve()`` (non-strict): resolve the existing prefix, keep the rest.
fn resolve_path(p: &str) -> String {
    let path = Path::new(p);
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    if let Ok(c) = std::fs::canonicalize(&abs) {
        return c.to_string_lossy().into_owned();
    }
    // Walk up to the longest existing ancestor, then re-append lexically.
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    let mut cur = abs.clone();
    loop {
        if let Ok(c) = std::fs::canonicalize(&cur) {
            let mut out = c;
            for comp in tail.iter().rev() {
                if comp == ".." {
                    out.pop();
                } else if comp != "." {
                    out.push(comp);
                }
            }
            return out.to_string_lossy().into_owned();
        }
        match (cur.file_name().map(|s| s.to_os_string()), cur.parent()) {
            (Some(name), Some(parent)) => {
                tail.push(name);
                cur = parent.to_path_buf();
            }
            _ => break,
        }
    }
    let mut out = PathBuf::new();
    for c in abs.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out.to_string_lossy().into_owned()
}

/// ``stub.build_register_payload``.
pub fn build_register_payload(a: &Args) -> Map<String, Value> {
    let ctx = InstallContext::from_args(a);
    let ctx = ctx.as_ref();
    let target_args = a.target_args();
    let (binary_version, stub_fp) = pool_binary_identity(ctx, &a.target_command, &target_args);
    let env_pairs = if !a.env_file.is_empty() {
        parse_env_file(&a.env_file)
    } else if !a.env_json.is_empty() {
        parse_env_json(&a.env_json)
    } else {
        parse_env_csv(&a.env)
    };
    let auto_approve = parse_auto_approve(&a.auto_approve);
    let channel_id = resolve_channel_id(a.channel_id.as_deref());
    let identity_keys = a.pool_identity_env();
    let work_dir = resolve_path(&a.work_dir);
    let caller = caller_block(channel_id.as_deref());
    let session_token = std::env::var(STUB_SESSION_TOKEN_ENV).unwrap_or_default();
    let principal = caller["principal_id"].as_str().unwrap_or("").to_string();

    let mut p = Map::new();
    p.insert("type".into(), json!("register"));
    p.insert("stub_uuid".into(), json!(uuid::Uuid::new_v4().to_string()));
    p.insert("server_name".into(), json!(a.server));
    p.insert("agent_name".into(), json!(a.agent));
    p.insert(
        "command_args_hash".into(),
        json!(hash_command(ctx, &a.target_command, &target_args)),
    );
    p.insert(
        "effective_env_hash".into(),
        json!(hash_effective_env(ctx, &env_pairs, &identity_keys)),
    );
    p.insert("work_dir".into(), json!(work_dir));
    p.insert("binary_version".into(), json!(binary_version));
    p.insert("os_uid".into(), json!(unsafe { libc::getuid() }));
    p.insert("sandbox_mode".into(), json!(a.sandbox_mode));
    p.insert(
        "autoapprove_set_hash".into(),
        json!(hash_permission_profile(
            &auto_approve,
            &a.approval_mode,
            a.trust_all
        )),
    );
    p.insert("approval_mode".into(), json!(a.approval_mode));
    p.insert("trust_all_tools".into(), json!(a.trust_all));
    p.insert("poolable".into(), json!(a.poolable));
    p.insert(
        "user_identity".into(),
        json!(if principal.is_empty() {
            "unknown".to_string()
        } else {
            principal.clone()
        }),
    );
    p.insert("channel_id".into(), json!(channel_id));
    p.insert(
        "config_snapshot_hash".into(),
        json!(CONFIG_SNAPSHOT_PLACEHOLDER),
    );
    p.insert("ancestor_pids".into(), json!(ancestor_pids()));
    p.insert("session_key".into(), caller["session_key"].clone());
    p.insert("session_type".into(), caller["session_type"].clone());
    p.insert("principal_id".into(), caller["principal_id"].clone());
    p.insert("caller".into(), Value::Object(caller));
    if !stub_fp.is_empty() && KIROCREW_BIN_MCP_SERVERS.contains(&a.server.as_str()) {
        p.insert("stub_code_fingerprint".into(), json!(stub_fp));
    }
    if !session_token.is_empty() {
        p.insert("stub_session_token".into(), json!(session_token));
    }
    p
}

/// ``hashing.format_pool_label``.
pub fn pool_label(p: &Map<String, Value>) -> String {
    let short = |k: &str| {
        let s = match &p[k] {
            Value::String(s) => s.clone(),
            v => v.to_string(),
        };
        if s.chars().count() > 8 {
            format!("{}\u{2026}", s.chars().take(8).collect::<String>())
        } else {
            s
        }
    };
    format!(
        "{} uid={} cmd={} env={} ws={}",
        p["server_name"].as_str().unwrap_or(""),
        p["os_uid"],
        short("command_args_hash"),
        short("effective_env_hash"),
        p["work_dir"].as_str().unwrap_or("")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_command_matches_python_literal() {
        // python3 -c "from kiro_crew.mcp_gateway.hashing import hash_command as h; print(h('/bin/x',['a','']))"
        // computed by hand: sha256(b"/bin/x\0a\0\0")
        let mut h = Sha256::new();
        h.update(b"/bin/x\0a\0\0");
        assert_eq!(
            hash_command(None, "/bin/x", &["a".into(), "".into()]),
            hex(&h.finalize())
        );
    }

    #[test]
    fn env_hash_drops_secrets_unless_named() {
        let mut env = BTreeMap::new();
        env.insert("AWS_SECRET_ACCESS_KEY".to_string(), "s".to_string());
        env.insert("A".to_string(), "1".to_string());
        let plain = hash_effective_env(None, &env, &[]);
        let mut only_a = BTreeMap::new();
        only_a.insert("A".to_string(), "1".to_string());
        assert_eq!(plain, hash_effective_env(None, &only_a, &[]));
        assert_ne!(
            plain,
            hash_effective_env(None, &env, &["AWS_SECRET_ACCESS_KEY".into()])
        );
    }

    #[test]
    fn interpreter_alias_folds_only_in_command_slot() {
        let ctx = InstallContext {
            interpreter: "/v/1/python".into(),
            package_dir: "/v/1/site/kiro_crew".into(),
            fingerprint_root: String::new(),
            dist_version: String::new(),
            python_argv: Vec::new(),
        };
        let ctx2 = InstallContext {
            interpreter: "/v/2/python".into(),
            ..ctx.clone()
        };
        assert_eq!(
            hash_command(Some(&ctx), "/v/1/python", &["x".into()]),
            hash_command(Some(&ctx2), "/v/2/python", &["x".into()])
        );
        assert_ne!(
            hash_command(Some(&ctx), "/bin/x", &["/v/1/python".into()]),
            hash_command(Some(&ctx2), "/bin/x", &["/v/2/python".into()])
        );
    }
}
