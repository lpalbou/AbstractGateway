//! The ONE table of command-line audio players this console drives (the
//! sandbox's Play / Speak and the entity voice audition), in preference
//! order: macOS `afplay`, PulseAudio `paplay`, ALSA `aplay`, then
//! `ffplay` (headless flags). Nothing is installed or guessed: a machine
//! without one says so and the saved file is the affordance.

use std::path::PathBuf;

/// (binary, args placed before the file path).
pub const AUDIO_PLAYERS: [(&str, &[&str]); 4] = [
    ("afplay", &[]),
    ("paplay", &[]),
    ("aplay", &["-q"]),
    ("ffplay", &["-nodisp", "-autoexit", "-loglevel", "quiet"]),
];

/// The first known player on `path_var` (a PATH-style list): its full
/// path and its arguments.
pub fn find_player_on(path_var: &std::ffi::OsStr) -> Option<(PathBuf, &'static [&'static str])> {
    for (bin, args) in AUDIO_PLAYERS {
        for dir in std::env::split_paths(path_var) {
            let candidate = dir.join(bin);
            if candidate.is_file() {
                return Some((candidate, args));
            }
        }
    }
    None
}

/// The first known player on the process PATH.
pub fn find_player() -> Option<(PathBuf, &'static [&'static str])> {
    find_player_on(&std::env::var_os("PATH")?)
}

/// The first known player's NAME on `path_var`.
pub fn find_player_in(path_var: &str) -> Option<String> {
    find_player_on(std::ffi::OsStr::new(path_var))
        .and_then(|(p, _)| p.file_name().and_then(|n| n.to_str()).map(str::to_string))
}

/// Play `file` with `player` (a name from [`AUDIO_PLAYERS`]) in the
/// background (the console never waits on playback).
pub fn spawn_player(player: &str, file: &str) -> std::io::Result<()> {
    let args: &[&str] = AUDIO_PLAYERS
        .iter()
        .find(|(b, _)| *b == player)
        .map(|(_, a)| *a)
        .unwrap_or(&[]);
    std::process::Command::new(player)
        .args(args)
        .arg(file)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn player_lookup_scans_the_given_path_only_in_table_order() {
        let dir = std::env::temp_dir().join(format!("agc-audio-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.to_str().unwrap();
        assert_eq!(find_player_in(p), None);
        std::fs::write(dir.join("ffplay"), b"").unwrap();
        assert_eq!(find_player_in(p).as_deref(), Some("ffplay"));
        std::fs::write(dir.join("aplay"), b"").unwrap();
        let (path, args) = find_player_on(std::ffi::OsStr::new(p)).unwrap();
        assert!(path.ends_with("aplay"), "table order wins: {path:?}");
        assert_eq!(args, &["-q"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
