//! Who made this console, and where to find it — the About modal's facts.
//!
//! The AbstractFramework root owns ONE canonical descriptor
//! (`identity/abstractframework.json`); every app vendors a BYTE-IDENTICAL
//! copy so an installed binary stays self-contained. This crate's copy is
//! `assets/abstractframework_identity.json`, compiled in with
//! `include_str!`; the root `scripts/check_identity_sync.py` fails when it
//! drifts. Nothing here is typed by hand: names, links, author, licence and
//! contact come from the descriptor, the version from Cargo.toml, and the
//! gateway's versions from its public `GET /api/gateway/about`.

use serde_json::Value;

/// The vendored descriptor, verbatim.
pub const IDENTITY_JSON: &str = include_str!("../assets/abstractframework_identity.json");

/// This console's key in the descriptor's `apps` map: it is AbstractGateway's
/// terminal console, so it shares the gateway's links.
pub const APP_ID: &str = "abstractgateway";

/// What the About modal calls this program (the descriptor names the
/// gateway; this binary is its console).
pub const CONSOLE_SUFFIX: &str = "console";

/// One app's identity, resolved against the framework's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppIdentity {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub framework_name: String,
    pub framework_website: String,
    pub author: String,
    pub years: String,
    pub license: String,
    pub copyright: String,
    pub contact_email: String,
    pub website: String,
    pub repo: String,
    pub docs: String,
    pub issues: String,
    pub feedback: String,
}

fn descriptor() -> Value {
    // A malformed vendored copy is a build-time defect, caught by the tests
    // below and by the root sync check — never a runtime condition.
    serde_json::from_str(IDENTITY_JSON).expect("vendored identity descriptor is valid JSON")
}

/// `app_id`'s identity at `version`. `None` when the descriptor does not
/// list the app (a descriptor/app mismatch — the tests pin ours).
pub fn app_identity(app_id: &str, version: &str) -> Option<AppIdentity> {
    let d = descriptor();
    let fw = d.get("framework")?;
    let app = d.get("apps")?.get(app_id)?;
    let s = |v: &Value, k: &str| {
        v.get(k)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string()
    };
    Some(AppIdentity {
        app_id: app_id.to_string(),
        name: s(app, "name"),
        version: version.to_string(),
        framework_name: s(fw, "name"),
        framework_website: s(fw, "website"),
        author: s(fw, "author"),
        years: s(fw, "years"),
        license: s(fw, "license"),
        copyright: s(fw, "copyright"),
        contact_email: s(fw, "contact_email"),
        website: s(app, "website"),
        repo: s(app, "repo"),
        docs: s(app, "docs"),
        issues: s(app, "issues"),
        feedback: s(app, "feedback"),
    })
}

/// This console's identity (this crate's version).
pub fn this_app() -> AppIdentity {
    app_identity(APP_ID, env!("CARGO_PKG_VERSION"))
        .expect("the vendored descriptor lists abstractgateway")
}

/// The gateway-version rows of `GET /api/gateway/about`
/// (`{abstractframework, abstractgateway, packages}`), the Rust twin of
/// ui-kit `gatewayVersionRows(payload, error?)` and AbstractCore
/// `gateway_version_rows(payload, error=None)` (contract A-9; the shared
/// fixture `tests/fixtures/gateway_version_rows.json` pins all three):
/// `Gateway: AbstractGateway <v>`, `Gateway framework: AbstractFramework
/// <v>` or `not installed on the gateway host`, then `Gateway package
/// <name>: <v>` sorted by code point. Only a non-empty string is a
/// version. An error (or a payload without a gateway version) is exactly
/// ONE row, `Gateway: unavailable (<reason>)`.
pub fn gateway_version_rows(payload: Option<&Value>, error: Option<&str>) -> Vec<(String, String)> {
    if let Some(e) = error {
        let e = e.trim();
        return vec![(
            "Gateway".into(),
            format!(
                "unavailable ({})",
                if e.is_empty() { "unknown error" } else { e }
            ),
        )];
    }
    let text = |v: Option<&Value>| {
        v.and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("")
            .to_string()
    };
    let obj = payload.and_then(Value::as_object);
    let gateway = text(obj.and_then(|o| o.get("abstractgateway")));
    if gateway.is_empty() {
        return vec![(
            "Gateway".into(),
            "unavailable (the gateway did not report its version)".into(),
        )];
    }
    let mut rows = vec![("Gateway".to_string(), format!("AbstractGateway {gateway}"))];
    let framework = text(obj.and_then(|o| o.get("abstractframework")));
    rows.push((
        "Gateway framework".into(),
        if framework.is_empty() {
            "not installed on the gateway host".into()
        } else {
            format!("AbstractFramework {framework}")
        },
    ));
    if let Some(pkgs) = obj
        .and_then(|o| o.get("packages"))
        .and_then(Value::as_object)
    {
        let mut names: Vec<&String> = pkgs.keys().collect();
        names.sort(); // byte order == code-point order for UTF-8
        for name in names {
            if name == "abstractgateway" || name == "abstractframework" {
                continue;
            }
            let v = text(pkgs.get(name));
            if !v.is_empty() {
                rows.push((format!("Gateway package {name}"), v));
            }
        }
    }
    rows
}

/// The About modal as `(label, value)` rows, in display order. An empty
/// label is a free-standing line; `gateway` rows (from
/// [`gateway_version_rows`]) follow the identity rows.
pub fn about_rows(id: &AppIdentity, gateway: &[(String, String)]) -> Vec<(String, String)> {
    let mut rows: Vec<(String, String)> = vec![
        (
            String::new(),
            format!("{} {} {}", id.name, CONSOLE_SUFFIX, id.version),
        ),
        (
            String::new(),
            format!("Part of {} — {}", id.framework_name, id.framework_website),
        ),
        (
            String::new(),
            format!("Author: {} ({})", id.author, id.years),
        ),
        (String::new(), id.copyright.clone()),
        ("Website".into(), id.website.clone()),
        ("Source".into(), id.repo.clone()),
        ("Documentation".into(), id.docs.clone()),
        ("Report an issue".into(), id.issues.clone()),
        ("Give feedback".into(), id.feedback.clone()),
        ("Contact".into(), id.contact_email.clone()),
    ];
    rows.extend(gateway.iter().cloned());
    rows
}

/// One printable line per row (`label: value`, or the bare value).
pub fn about_lines(rows: &[(String, String)]) -> Vec<String> {
    rows.iter()
        .map(|(k, v)| {
            if k.is_empty() {
                v.clone()
            } else {
                format!("{k}: {v}")
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_vendored_descriptor_names_the_gateway() {
        let id = this_app();
        assert_eq!(id.name, "AbstractGateway");
        assert_eq!(id.version, env!("CARGO_PKG_VERSION"));
        assert_eq!(id.framework_name, "AbstractFramework");
        assert_eq!(id.framework_website, "https://abstractframework.ai");
        assert_eq!(id.author, "Laurent-Philippe Albou, PhD");
        assert_eq!(id.years, "2023-2026");
        assert!(id.copyright.contains("MIT"));
        for link in [&id.website, &id.repo, &id.docs, &id.issues, &id.feedback] {
            assert!(link.starts_with("https://"), "{link}");
        }
        assert!(id.contact_email.contains('@'));
        assert!(app_identity("no-such-app", "0").is_none());
    }

    #[test]
    fn about_rows_carry_every_required_line() {
        let id = this_app();
        let gw = gateway_version_rows(Some(&serde_json::json!({"abstractgateway": "0.4.4"})), None);
        let all = about_lines(&about_rows(&id, &gw)).join("\n");
        for needle in [
            &format!("AbstractGateway console {}", env!("CARGO_PKG_VERSION")),
            "Part of AbstractFramework — https://abstractframework.ai",
            "Author: Laurent-Philippe Albou, PhD (2023-2026)",
            "© 2023-2026 Laurent-Philippe Albou, PhD. Released under the MIT License.",
            "Website: https://abstractframework.ai/gateway",
            "Source: https://github.com/lpalbou/AbstractGateway",
            "Documentation: https://www.lpalbou.info/AbstractGateway/",
            "Report an issue: https://github.com/lpalbou/AbstractGateway/issues",
            "Give feedback: https://github.com/lpalbou/AbstractGateway/issues/new?labels=feedback",
            "Contact: contact@abstractframework.ai",
            "Gateway: AbstractGateway 0.4.4",
            "Gateway framework: not installed on the gateway host",
        ] {
            assert!(all.contains(needle), "missing {needle:?} in\n{all}");
        }
    }

    /// The shared parity fixture (ui-kit `scripts/fixtures/
    /// gateway_version_rows.json`, rows produced by the Python twin): every
    /// case, exactly. A missing or shrunken fixture fails.
    #[test]
    fn gateway_version_rows_match_the_shared_fixture() {
        let raw = include_str!("../tests/fixtures/gateway_version_rows.json");
        let fixture: Value = serde_json::from_str(raw).expect("fixture JSON");
        let cases = fixture["cases"].as_array().expect("cases");
        assert_eq!(cases.len(), 13, "the shared fixture has 13 cases");
        for case in cases {
            let payload = case.get("payload").filter(|p| !p.is_null());
            let error = case.get("error").and_then(Value::as_str);
            let got = gateway_version_rows(payload, error);
            let want: Vec<(String, String)> = case["expected"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| {
                    (
                        r[0].as_str().unwrap().to_string(),
                        r[1].as_str().unwrap().to_string(),
                    )
                })
                .collect();
            assert_eq!(got, want, "case {}", case["name"]);
        }
    }

    /// Cargo.toml's homepage/documentation mirror the descriptor, and the
    /// asset ships in the published crate (include_str! needs it).
    #[test]
    fn manifest_links_match_the_descriptor() {
        let manifest = include_str!("../Cargo.toml");
        let id = this_app();
        assert!(
            manifest.contains(&format!("homepage = \"{}\"", id.website)),
            "homepage"
        );
        assert!(
            manifest.contains(&format!("documentation = \"{}\"", id.docs)),
            "documentation"
        );
        assert!(
            manifest.contains("\"assets/abstractframework_identity.json\""),
            "asset shipped"
        );
    }
}
