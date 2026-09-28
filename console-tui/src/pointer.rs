//! "Where is my gateway": the gateway address this console connects to.
//!
//! In order:
//!   1. `--gateway-url` (alias `--url`);
//!   2. `ABSTRACTGATEWAY_URL` (legacy alias of the flag);
//!   3. the local gateway pointer `~/.abstractframework/gateway.json`, which
//!      the installer and `abstractgateway serve` write for THIS computer's
//!      gateway (root backlog 0943);
//!   4. `http://127.0.0.1:8080`.
//!
//! This console keeps no saved connection (tokens stay in memory), so there
//! is no "saved login" step. The pointer is believed only when `schema` is 1,
//! the url is http(s) on 127.0.0.1 / ::1 / localhost with nothing after the
//! port and no user info, and — on POSIX — the file is a regular file (not a
//! symlink) owned by the current user and writable by nobody else (no
//! group/world write bit). The checks run on the OPENED file (POSIX:
//! `O_NOFOLLOW | O_NONBLOCK`, then `fstat`) and the bytes are read from that
//! same descriptor, so the file cannot be swapped between the check and the
//! read and a FIFO in its place cannot hang the console. A pointer is a few
//! hundred bytes of configuration: a file over [`MAX_POINTER_BYTES`] is
//! refused, never read (a config-file bound, not a model-input cap). A bad
//! file is ignored with ONE visible notice; a missing one is silent. The
//! reader matches abstractcode's `tui/src/gateway_pointer.rs` and the kit's
//! app-server `gateway_pointer.js` (v0.1.14). The same rules and the same shared case
//! table (`tests/fixtures/gateway_pointer/`, byte-identical to AbstractUIC's
//! `ui-kit/scripts/fixtures/gateway_pointer/`) as every other reader.
//!
//! A URL that came from the pointer or the default is re-resolved after a
//! failed connection, so a gateway restarted on a new port is followed; a
//! flag or the environment stays what the person chose.

use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

pub const POINTER_SCHEMA: u64 = 1;
/// The largest pointer file this reader opens (64 KiB). The real file is a
/// few hundred bytes; anything this large is not a pointer.
pub const MAX_POINTER_BYTES: u64 = 64 * 1024;
pub const BUILTIN_GATEWAY_URL: &str = "http://127.0.0.1:8080";

/// Where the URL in use came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UrlSource {
    Flag,
    Env,
    Pointer,
    Default,
}

impl UrlSource {
    /// A URL that follows the pointer when a connection fails.
    pub fn follows_pointer(self) -> bool {
        matches!(self, UrlSource::Pointer | UrlSource::Default)
    }
}

/// One pointer read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PointerRead {
    Ok {
        url: String,
    },
    /// No file: silent.
    Missing,
    /// Ignored, with the one notice to show.
    Refused {
        warning: String,
    },
}

/// The resolved address.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub url: String,
    pub source: UrlSource,
    /// The notice for a pointer file that was ignored (None otherwise).
    pub warning: Option<String>,
}

/// `~/.abstractframework/gateway.json`.
pub fn pointer_path(home: &Path) -> PathBuf {
    home.join(".abstractframework").join("gateway.json")
}

/// The home directory the pointer lives under (`HOME`, `USERPROFILE` on
/// Windows); None when neither is set.
pub fn home_dir() -> Option<PathBuf> {
    let var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    std::env::var_os(var)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

/// The current user's id (POSIX); None on Windows, where ownership is not
/// checked.
pub fn current_uid() -> Option<u32> {
    #[cfg(unix)]
    {
        // SAFETY: getuid has no preconditions and cannot fail.
        Some(unsafe { libc::getuid() })
    }
    #[cfg(not(unix))]
    {
        None
    }
}

fn refused(path: &Path, reason: &str) -> PointerRead {
    PointerRead::Refused {
        warning: format!("Ignoring the gateway pointer {}: {reason}.", path.display()),
    }
}

/// Why the pointer could not be opened.
enum OpenError {
    Missing,
    Symlink,
    Other(std::io::Error),
}

/// Open without following a final symlink and without blocking (a FIFO).
#[cfg(unix)]
fn open_pointer(path: &Path) -> Result<File, OpenError> {
    use std::os::unix::fs::OpenOptionsExt;
    std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| match e.raw_os_error() {
            _ if e.kind() == std::io::ErrorKind::NotFound => OpenError::Missing,
            // ELOOP (Linux, macOS) / EMLINK (FreeBSD): the final component is a symlink.
            Some(code) if code == libc::ELOOP || code == libc::EMLINK => OpenError::Symlink,
            _ => OpenError::Other(e),
        })
}

/// Windows has no O_NOFOLLOW through std: refuse a symlink before opening.
#[cfg(not(unix))]
fn open_pointer(path: &Path) -> Result<File, OpenError> {
    match std::fs::symlink_metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(OpenError::Missing),
        Err(e) => return Err(OpenError::Other(e)),
        Ok(m) if m.file_type().is_symlink() => return Err(OpenError::Symlink),
        Ok(_) => {}
    }
    File::open(path).map_err(|e| match e.kind() {
        std::io::ErrorKind::NotFound => OpenError::Missing,
        _ => OpenError::Other(e),
    })
}

/// Read and check the pointer at `path`. `uid`: the owner it must have
/// (None = not checked, Windows).
pub fn read_pointer(path: &Path, uid: Option<u32>) -> PointerRead {
    let mut file = match open_pointer(path) {
        Ok(f) => f,
        Err(OpenError::Missing) => return PointerRead::Missing,
        Err(OpenError::Symlink) => return refused(path, "it is a symbolic link"),
        Err(OpenError::Other(e)) => return refused(path, &format!("cannot read it ({e})")),
    };
    // Every check below reads the OPENED file (fstat), never the path again.
    let meta = match file.metadata() {
        Ok(m) => m,
        Err(e) => return refused(path, &format!("cannot read it ({e})")),
    };
    if !meta.file_type().is_file() {
        return refused(path, "it is not a regular file");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if let Some(uid) = uid {
            if meta.uid() != uid {
                return refused(path, "it belongs to another user");
            }
        }
        if meta.mode() & 0o022 != 0 {
            return refused(
                path,
                &format!("other users can write it (mode {:o})", meta.mode() & 0o777),
            );
        }
    }
    #[cfg(not(unix))]
    let _ = uid;
    // Read from the same descriptor, at most one byte past the bound: an
    // oversized file (or one that grew after the fstat) is refused, and
    // nothing past the bound is ever read.
    // #[WARNING:TRUNCATION] config-file bound (64 KiB), refused whole — never a partial read
    let mut text = String::new();
    if let Err(e) = (&mut file)
        .take(MAX_POINTER_BYTES + 1)
        .read_to_string(&mut text)
    {
        return refused(path, &format!("cannot read it ({e})"));
    }
    if text.len() as u64 > MAX_POINTER_BYTES {
        return refused(
            path,
            &format!(
                "it is larger than the {} KiB a pointer file may be",
                MAX_POINTER_BYTES / 1024
            ),
        );
    }
    let data: serde_json::Value = match serde_json::from_str(&text) {
        Ok(v) => v,
        Err(e) => {
            let msg: String = e.to_string().chars().take(120).collect();
            return refused(path, &format!("it is not valid JSON ({msg})"));
        }
    };
    let Some(obj) = data.as_object() else {
        return refused(path, "it is not a JSON object");
    };
    let schema = obj.get("schema");
    if schema.and_then(serde_json::Value::as_u64) != Some(POINTER_SCHEMA) {
        return refused(
            path,
            &format!(
                "unknown schema {} (this reader knows {POINTER_SCHEMA})",
                schema
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "undefined".into())
            ),
        );
    }
    let raw = obj
        .get("url")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    match loopback_origin(raw) {
        Ok(url) => PointerRead::Ok { url },
        Err(reason) => refused(path, &reason),
    }
}

/// `scheme://host[:port]` of a loopback http(s) URL with nothing after the
/// port (a bare trailing `/` is allowed), or why not. The default port is
/// dropped, like a URL parser's `host`.
pub fn loopback_origin(raw: &str) -> Result<String, String> {
    let raw = raw.trim();
    let Some((scheme, rest)) = raw.split_once("://") else {
        return Err(format!("url {raw:?} is not a URL"));
    };
    let scheme = scheme.to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return Err(format!("url {raw} is not http(s)"));
    }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(end);
    if authority.contains('@') {
        return Err(format!("url {raw} must be scheme://host:port only"));
    }
    let (host, port) = if let Some(stripped) = authority.strip_prefix('[') {
        let Some((h, after)) = stripped.split_once(']') else {
            return Err(format!("url {raw:?} is not a URL"));
        };
        (
            format!("[{}]", h.to_ascii_lowercase()),
            after.strip_prefix(':'),
        )
    } else {
        match authority.rsplit_once(':') {
            Some((h, p)) => (h.to_ascii_lowercase(), Some(p)),
            None => (authority.to_ascii_lowercase(), None),
        }
    };
    if host.is_empty() {
        return Err(format!("url {raw:?} is not a URL"));
    }
    let port = match port.filter(|p| !p.is_empty()) {
        Some(p) => match p.parse::<u16>() {
            Ok(n) if n > 0 => Some(n),
            _ => return Err(format!("url {raw:?} is not a URL")),
        },
        None => None,
    };
    if !["127.0.0.1", "[::1]", "localhost"].contains(&host.as_str()) {
        return Err(format!(
            "url {raw} is not on this computer (127.0.0.1, ::1 or localhost only)"
        ));
    }
    if !tail.is_empty() && tail != "/" {
        return Err(format!("url {raw} must be scheme://host:port only"));
    }
    let default_port = if scheme == "https" { 443 } else { 80 };
    Ok(match port {
        Some(p) if p != default_port => format!("{scheme}://{host}:{p}"),
        _ => format!("{scheme}://{host}"),
    })
}

fn normalize(v: &str) -> String {
    v.trim().trim_end_matches('/').to_string()
}

/// The URL by the precedence above. `home` None = no pointer can be read.
pub fn resolve(
    flag: &str,
    env_url: Option<&str>,
    home: Option<&Path>,
    uid: Option<u32>,
) -> Resolved {
    let flag = normalize(flag);
    if !flag.is_empty() {
        return Resolved {
            url: flag,
            source: UrlSource::Flag,
            warning: None,
        };
    }
    if let Some(env) = env_url.map(normalize).filter(|e| !e.is_empty()) {
        return Resolved {
            url: env,
            source: UrlSource::Env,
            warning: None,
        };
    }
    let read = home
        .map(|h| read_pointer(&pointer_path(h), uid))
        .unwrap_or(PointerRead::Missing);
    match read {
        PointerRead::Ok { url } => Resolved {
            url,
            source: UrlSource::Pointer,
            warning: None,
        },
        PointerRead::Missing => Resolved {
            url: BUILTIN_GATEWAY_URL.to_string(),
            source: UrlSource::Default,
            warning: None,
        },
        PointerRead::Refused { warning } => Resolved {
            url: BUILTIN_GATEWAY_URL.to_string(),
            source: UrlSource::Default,
            warning: Some(warning),
        },
    }
}

/// [`resolve`] for this process (real `HOME`, uid and environment).
pub fn resolve_now(flag: &str) -> Resolved {
    let env = std::env::var("ABSTRACTGATEWAY_URL").ok();
    resolve(flag, env.as_deref(), home_dir().as_deref(), current_uid())
}

/// After a failed connection on a URL that follows the pointer: the URL
/// the pointer names now (with its notice), or None when nothing changes.
pub fn follow_pointer(
    current_url: &str,
    home: Option<&Path>,
    uid: Option<u32>,
) -> Option<Resolved> {
    let next = resolve("", None, home, uid);
    (normalize(&next.url) != normalize(current_url)).then_some(next)
}
