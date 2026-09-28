//! The local gateway pointer (`~/.abstractframework/gateway.json`, root
//! backlog 0943) and the address precedence. The shared case table
//! `tests/fixtures/gateway_pointer/cases.json` is byte-identical to
//! AbstractUIC's `ui-kit/scripts/fixtures/gateway_pointer/` (every reader
//! checks the same cases); the rest pins what the table cannot express.

use std::path::{Path, PathBuf};

use abstractgateway_console::pointer::{
    current_uid, follow_pointer, loopback_origin, read_pointer, resolve, PointerRead, UrlSource,
    BUILTIN_GATEWAY_URL,
};
use serde_json::Value;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/gateway_pointer")
}

/// A scratch HOME inside the crate's target dir (tests write nowhere else).
fn home(name: &str) -> PathBuf {
    let h = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("target/test-scratch")
        .join(format!("pointer-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&h);
    std::fs::create_dir_all(h.join(".abstractframework")).unwrap();
    h
}

fn put(home: &Path, text: &str) -> PathBuf {
    let p = home.join(".abstractframework/gateway.json");
    std::fs::write(&p, text).unwrap();
    p
}

#[test]
fn the_shared_case_table() {
    let cases: Value =
        serde_json::from_str(&std::fs::read_to_string(fixtures().join("cases.json")).unwrap())
            .unwrap();
    let cases = cases["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 5);
    for c in cases {
        let name = c["name"].as_str().unwrap();
        let h = home(name);
        if let Some(file) = c["file"].as_str() {
            let text = std::fs::read_to_string(fixtures().join(file)).unwrap();
            put(&h, &text);
        }
        let r = resolve("", None, Some(&h), current_uid());
        assert_eq!(r.url, c["expect"].as_str().unwrap(), "{name}");
        assert_eq!(
            r.warning.is_some(),
            c["warn"].as_bool().unwrap(),
            "{name}: {:?}",
            r.warning
        );
        let from_pointer = name == "valid";
        assert_eq!(r.source == UrlSource::Pointer, from_pointer, "{name}");
    }
}

#[test]
fn flag_then_env_then_pointer_then_default() {
    let h = home("order");
    put(
        &h,
        &std::fs::read_to_string(fixtures().join("valid.json")).unwrap(),
    );
    let uid = current_uid();
    let r = resolve(
        "http://10.0.0.5:9000/",
        Some("http://127.0.0.1:7000"),
        Some(&h),
        uid,
    );
    assert_eq!(
        (r.url.as_str(), r.source),
        ("http://10.0.0.5:9000", UrlSource::Flag)
    );
    let r = resolve("", Some("http://127.0.0.1:7000"), Some(&h), uid);
    assert_eq!(
        (r.url.as_str(), r.source),
        ("http://127.0.0.1:7000", UrlSource::Env)
    );
    let r = resolve("", Some("  "), Some(&h), uid);
    assert_eq!(
        (r.url.as_str(), r.source),
        ("http://127.0.0.1:8081", UrlSource::Pointer)
    );
    let r = resolve("", None, None, uid);
    assert_eq!(
        (r.url.as_str(), r.source),
        (BUILTIN_GATEWAY_URL, UrlSource::Default)
    );
    assert!(r.warning.is_none(), "no home, no pointer: silent");
}

#[test]
fn url_rules() {
    assert_eq!(
        loopback_origin("http://127.0.0.1:8081").unwrap(),
        "http://127.0.0.1:8081"
    );
    assert_eq!(
        loopback_origin("http://127.0.0.1:8081/").unwrap(),
        "http://127.0.0.1:8081"
    );
    assert_eq!(
        loopback_origin("https://LocalHost:8443").unwrap(),
        "https://localhost:8443"
    );
    assert_eq!(
        loopback_origin("http://[::1]:18893").unwrap(),
        "http://[::1]:18893"
    );
    assert_eq!(
        loopback_origin("http://localhost:80").unwrap(),
        "http://localhost"
    );
    for bad in [
        "ftp://127.0.0.1:21",
        "http://192.168.1.20:8081",
        "http://example.com",
        "http://user:pw@127.0.0.1:8081",
        "http://127.0.0.1:8081/console",
        "http://127.0.0.1:8081?x=1",
        "http://127.0.0.1:8081#a",
        "http://127.0.0.1:99999",
        "127.0.0.1:8081",
        "",
    ] {
        assert!(loopback_origin(bad).is_err(), "{bad}");
    }
}

#[test]
fn only_a_regular_file_of_this_user() {
    let valid = std::fs::read_to_string(fixtures().join("valid.json")).unwrap();
    let h = home("owner");
    let p = put(&h, &valid);
    assert_eq!(
        read_pointer(&p, current_uid()),
        PointerRead::Ok {
            url: "http://127.0.0.1:8081".into()
        }
    );
    #[cfg(unix)]
    {
        let other = current_uid().unwrap().wrapping_add(1);
        match read_pointer(&p, Some(other)) {
            PointerRead::Refused { warning } => {
                assert!(warning.contains("belongs to another user"), "{warning}")
            }
            r => panic!("{r:?}"),
        }
        let h = home("symlink");
        let target = h.join("real.json");
        std::fs::write(&target, &valid).unwrap();
        let link = h.join(".abstractframework/gateway.json");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        match read_pointer(&link, current_uid()) {
            PointerRead::Refused { warning } => {
                assert!(warning.contains("not a regular file"), "{warning}")
            }
            r => panic!("a symlink is refused: {r:?}"),
        }
    }
    let h = home("dir");
    std::fs::create_dir_all(h.join(".abstractframework/gateway.json")).unwrap();
    assert!(matches!(
        read_pointer(&h.join(".abstractframework/gateway.json"), current_uid()),
        PointerRead::Refused { .. }
    ));
    assert_eq!(
        read_pointer(
            &home("none").join(".abstractframework/gateway.json"),
            current_uid()
        ),
        PointerRead::Missing
    );
}

#[test]
fn a_gateway_restarted_on_a_new_port_is_followed() {
    let h = home("follow");
    let uid = current_uid();
    put(&h, r#"{"schema": 1, "url": "http://127.0.0.1:18893"}"#);
    assert_eq!(
        follow_pointer("http://127.0.0.1:18893", Some(&h), uid),
        None,
        "unchanged: nothing to do"
    );
    put(&h, r#"{"schema": 1, "url": "http://127.0.0.1:18894"}"#);
    let next = follow_pointer("http://127.0.0.1:18893", Some(&h), uid).expect("moved");
    assert_eq!(
        (next.url.as_str(), next.source),
        ("http://127.0.0.1:18894", UrlSource::Pointer)
    );
}
