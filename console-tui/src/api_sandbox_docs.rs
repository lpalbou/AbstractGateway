//! Client methods for the Review screen's multimodal sandbox and the
//! docs assistant (web-console parity, 2026-09-27).
//!
//! Declared as a CHILD module of `api` (see the `#[path]` line there) so
//! these methods reach the private request helpers (`send`, `get`,
//! `with_auth`) instead of duplicating the transport.
//!
//! Every route here is the one the web console calls (console.py
//! `runSandbox`, `assistantEnsureCorpus`, `assistantAsk`):
//! - text: `POST /sandbox/generate`
//! - image: `POST /runs/{run}/images/generate`
//! - voice: `POST /runs/{run}/voice/tts` (the existing `run_voice_tts`)
//! - music + sound effects: `POST /runs/{run}/music/generate`
//! - video: `POST /runs/{run}/videos/generate`
//! - docs corpus: `GET /docs/corpus`; docs answer: `POST /runs/start`
//!   (the docs-qa bundle) then `GET /runs/{run}` (the existing
//!   `run_status`).

use serde_json::Value;

use super::{err_from_ureq, urlencode, ApiError, ApiErrorKind, ApiResult, GatewayClient};

impl GatewayClient {
    /// Text sandbox turn — the body is built by the caller
    /// (`ui::sandbox::text_body`) so the exact web payload is unit-tested
    /// without a socket. Slow lane: a real model call.
    pub fn sandbox_text(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/sandbox/generate", body, true)
    }

    /// Direct media generation on the sandbox session run. `leaf` is the
    /// route tail the web picks per mode (`images/generate`,
    /// `music/generate`, `videos/generate`, `voice/tts`). Slow lane:
    /// local diffusion runs for minutes (the web marks these `slow`).
    pub fn sandbox_media(&self, run_id: &str, leaf: &str, body: &Value) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/runs/{}/{}", urlencode(run_id), leaf),
            body,
            true,
        )
    }

    /// Stream one artifact's FULL bytes into `dest` (created/truncated).
    /// No cap: the web downloads the whole blob for its media players,
    /// and a saved file that was silently cut would be a corrupt lie.
    /// Returns the byte count written.
    pub fn save_artifact(
        &self,
        run_id: &str,
        artifact_id: &str,
        dest: &std::path::Path,
    ) -> ApiResult<u64> {
        let url = format!(
            "{}/api/gateway/runs/{}/artifacts/{}/content?access=download",
            self.base_url,
            urlencode(run_id),
            urlencode(artifact_id)
        );
        let req = self.with_auth(self.slow_agent.get(&url));
        let resp = req
            .call()
            .map_err(|e| err_from_ureq("artifact content", e))?;
        let io_err = |what: &str, e: std::io::Error| ApiError {
            kind: ApiErrorKind::Protocol,
            message: format!("{what} {}: {e}", dest.display()),
            body: None,
            timed_out: false,
        };
        if let Some(parent) = dest.parent() {
            std::fs::create_dir_all(parent).map_err(|e| io_err("cannot create folder for", e))?;
        }
        let mut file = std::fs::File::create(dest).map_err(|e| io_err("cannot create", e))?;
        std::io::copy(&mut resp.into_reader(), &mut file)
            .map_err(|e| io_err("download interrupted while writing", e))
    }

    /// The gateway's own documentation corpus (`{app, source, chars,
    /// text}`); 404 with a detail when the gateway runs without one.
    pub fn docs_corpus(&self) -> ApiResult<Value> {
        self.get("/docs/corpus", false)
    }

    /// Start one workflow run (`POST /runs/start`) — the docs assistant
    /// starts the docs-qa catalog bundle with it.
    pub fn start_run(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/runs/start", body, false)
    }
}
