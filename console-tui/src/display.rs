//! Is there a screen in front of the person using this console?
//!
//! A URL opener (`open` / `xdg-open` / `explorer`) run without one either
//! fails obscurely or — worse, over SSH — opens a browser on the REMOTE
//! machine where nobody sees it. So the console never runs one when:
//!
//! - the session is SSH (`SSH_CONNECTION` or `SSH_TTY` set): the screen
//!   in front of the person is on another computer, whatever the OS;
//! - on Linux (and the BSDs), no graphical display is set
//!   (`DISPLAY` / `WAYLAND_DISPLAY` both unset).
//!
//! It shows the link to copy instead (and the SSH tunnel when the link
//! names a loopback address).

/// `None` = a browser can open here; `Some(reason)` = never run a URL
/// opener, say this instead. `env` reads one variable; `os` is
/// `std::env::consts::OS`.
pub fn no_display_reason(env: impl Fn(&str) -> Option<String>, os: &str) -> Option<String> {
    let set = |name: &str| env(name).map(|v| !v.trim().is_empty()).unwrap_or(false);
    if set("SSH_CONNECTION") || set("SSH_TTY") {
        return Some(
            "this is an SSH session: a browser opened here would appear on the remote machine, not in front of you"
                .into(),
        );
    }
    let has_display_server = !matches!(os, "macos" | "windows");
    if has_display_server && !set("DISPLAY") && !set("WAYLAND_DISPLAY") {
        return Some("this machine has no display (DISPLAY and WAYLAND_DISPLAY are unset)".into());
    }
    None
}

/// [`no_display_reason`] for this process.
pub fn no_display_reason_now() -> Option<String> {
    no_display_reason(|k| std::env::var(k).ok(), std::env::consts::OS)
}

#[cfg(test)]
mod tests {
    use super::no_display_reason;
    use std::collections::HashMap;

    fn with(vars: &[(&str, &str)], os: &str) -> Option<String> {
        let m: HashMap<String, String> = vars
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        no_display_reason(|k| m.get(k).cloned(), os)
    }

    #[test]
    fn ssh_never_opens_a_browser_on_any_os() {
        for os in ["macos", "linux", "windows"] {
            let r = with(
                &[
                    ("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22"),
                    ("DISPLAY", ":0"),
                ],
                os,
            );
            assert!(r.unwrap().contains("SSH session"), "{os}");
            assert!(with(&[("SSH_TTY", "/dev/pts/1")], os).is_some(), "{os}");
        }
    }

    #[test]
    fn linux_needs_a_display_server() {
        assert!(with(&[], "linux").unwrap().contains("no display"));
        assert!(
            with(&[("DISPLAY", " ")], "linux").is_some(),
            "blank is unset"
        );
        assert_eq!(with(&[("DISPLAY", ":0")], "linux"), None);
        assert_eq!(with(&[("WAYLAND_DISPLAY", "wayland-0")], "linux"), None);
        assert!(with(&[], "freebsd").is_some());
    }

    #[test]
    fn a_local_mac_or_windows_session_opens() {
        assert_eq!(with(&[], "macos"), None);
        assert_eq!(with(&[], "windows"), None);
    }
}
