//! The header's memory/compute widget (round 14, R10.3 parity): the web
//! console's top-bar line — memory used/total (%) · GPU busy % · models
//! loaded — replacing the address in the title bar once signed in (the
//! address stays on Connection, Network and About, as on the web). It
//! reads the RESOURCES snapshot (`GET /host/state`, `store.host_state` —
//! the same one the Resources page renders; no second store), refreshed
//! every few seconds while signed in (`Cmd::RefreshHostWidget` off the
//! Resources page, whose own chain refreshes it there). Every figure comes
//! from the snapshot: an unknown one is "—", never an invented zero. The
//! web tooltip's real values (one per line) are [`WidgetView::tip`]; a
//! terminal has no hover, so `H` (Resources) shows them in full.

use crate::store::{human_bytes, HostStateData};

/// How often the widget re-reads `GET /host/state` (the web's 5 s).
pub const POLL: std::time::Duration = std::time::Duration::from_secs(5);

/// What the widget shows (the web's `topbarResourcesView`).
#[derive(Clone, Debug, PartialEq)]
pub struct WidgetView {
    /// "18.2 GiB / 64.0 GiB (28%)" | "28%" | "—".
    pub mem: String,
    /// "28%" | "—" (narrow terminals).
    pub mem_short: String,
    /// "3%" | "—".
    pub gpu: String,
    /// "1 model" | "2 models" | "— models".
    pub models: String,
    /// The tooltip's real values, one per line.
    pub tip: Vec<String>,
    /// No snapshot yet, or the last refresh failed.
    pub stale: bool,
}

/// The web's `_fmtPct`.
fn pct(n: Option<f64>) -> String {
    match n {
        Some(v) if v.is_finite() => format!("{}%", v.round() as i64),
        _ => "unknown".into(),
    }
}

/// The widget for a snapshot (None = not read yet) and the last error.
pub fn view(data: Option<&HostStateData>, error: Option<&str>) -> WidgetView {
    let Some(d) = data else {
        let why = match error {
            Some(e) => format!("Host resources unavailable: {e}"),
            None => "Reading host resources…".into(),
        };
        return WidgetView {
            mem: "—".into(),
            mem_short: "—".into(),
            gpu: "—".into(),
            models: "—".into(),
            tip: vec![why],
            stale: true,
        };
    };
    let ram = d.ram.as_ref();
    let used = ram.and_then(|r| r.used_bytes);
    let total = ram.and_then(|r| r.total_bytes);
    let percent = ram.and_then(|r| r.percent).or(match (used, total) {
        (Some(u), Some(t)) if t > 0 => Some(u as f64 / t as f64 * 100.0),
        _ => None,
    });
    let gpu_pct = if d.gpu_supported {
        d.gpu_util_pct.filter(|v| v.is_finite() && *v >= 0.0)
    } else {
        None
    };
    let mem = match (used, total) {
        (Some(u), Some(t)) => format!("{} / {} ({})", human_bytes(u), human_bytes(t), pct(percent)),
        _ if percent.is_some() => pct(percent),
        _ => "—".into(),
    };
    let models = match d.models_resident {
        None => "— models".to_string(),
        Some(1) => "1 model".to_string(),
        Some(n) => format!("{n} models"),
    };
    let mut tip = vec![format!(
        "RAM: {}",
        match (used, total) {
            (Some(u), Some(t)) => format!(
                "{} of {} ({})",
                human_bytes(u),
                human_bytes(t),
                pct(percent)
            ),
            _ if percent.is_some() => pct(percent),
            _ => "unknown".into(),
        }
    )];
    tip.push(format!(
        "Model weights: {} · {}",
        d.model_bytes
            .map(human_bytes)
            .unwrap_or_else(|| "unknown".into()),
        match d.models_resident {
            None => "models loaded unknown".to_string(),
            Some(1) => "1 model loaded".to_string(),
            Some(n) => format!("{n} models loaded"),
        }
    ));
    tip.push(format!(
        "GPU load: {}",
        match gpu_pct {
            None if d.gpu_supported => "unknown".to_string(),
            None => "not measured on this host".to_string(),
            Some(p) => match &d.gpu_source {
                Some(src) => format!("{} (via {src})", pct(Some(p))),
                None => pct(Some(p)),
            },
        }
    ));
    if let Some(e) = error {
        tip.push(format!("Last refresh failed: {e}"));
    }
    WidgetView {
        mem,
        mem_short: if percent.is_some() {
            pct(percent)
        } else {
            "—".into()
        },
        gpu: gpu_pct.map(|p| pct(Some(p))).unwrap_or_else(|| "—".into()),
        models,
        tip,
        stale: error.is_some(),
    }
}

impl WidgetView {
    /// The header text: "Mem 18.2 GiB / 64.0 GiB (28%) · GPU 3% · 1 model"
    /// (`short`: "Mem 28% · GPU 3% · 1 model", the web's narrow form).
    pub fn line(&self, short: bool) -> String {
        format!(
            "Mem {} · GPU {} · {}",
            if short { &self.mem_short } else { &self.mem },
            self.gpu,
            self.models
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::host_state_from_payload;
    use serde_json::json;

    #[test]
    fn the_widget_reads_the_snapshot_never_invents() {
        let d = host_state_from_payload(&json!({
            "memory": {"ram": {"total_bytes": 68719476736u64, "used_bytes": 19541180416u64, "percent": 28.4}},
            "gpu": {"supported": true, "utilization_gpu_pct": 3.2, "source": "ioreg"},
            "totals": {"models_resident": 1, "model_bytes": 4294967296u64}
        }));
        let v = view(Some(&d), None);
        assert_eq!(
            v.line(false),
            "Mem 18.2 GiB / 64.0 GiB (28%) · GPU 3% · 1 model"
        );
        assert_eq!(v.line(true), "Mem 28% · GPU 3% · 1 model");
        assert_eq!(v.tip[0], "RAM: 18.2 GiB of 64.0 GiB (28%)");
        assert_eq!(v.tip[1], "Model weights: 4.0 GiB · 1 model loaded");
        assert_eq!(v.tip[2], "GPU load: 3% (via ioreg)");
        assert!(!v.stale);
        // Unknown figures are dashes, never zeros.
        let d = host_state_from_payload(&json!({"memory": {}, "gpu": {"supported": false}}));
        let v = view(Some(&d), None);
        assert_eq!(v.line(false), "Mem — · GPU — · — models");
        assert_eq!(v.tip[2], "GPU load: not measured on this host");
        // Two models; a failed refresh keeps the snapshot, marked stale.
        let d = host_state_from_payload(&json!({"totals": {"models_resident": 2}}));
        let v = view(Some(&d), Some("network failure: refused"));
        assert_eq!(v.models, "2 models");
        assert!(v.stale);
        assert_eq!(
            v.tip.last().unwrap(),
            "Last refresh failed: network failure: refused"
        );
        let v = view(None, None);
        assert_eq!(v.line(true), "Mem — · GPU — · —");
        assert!(v.stale);
    }
}
