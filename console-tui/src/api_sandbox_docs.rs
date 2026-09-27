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

    /// `POST /attachments/upload` (multipart: `session_id`, `file`,
    /// `filename`, optional `content_type`) — the web's `uploadSandboxFile`.
    /// Slow lane: the body is the whole file.
    pub fn upload_attachment(
        &self,
        session_id: &str,
        filename: &str,
        content_type: Option<&str>,
        bytes: &[u8],
    ) -> ApiResult<Value> {
        let (boundary, body) = multipart_body(session_id, filename, content_type, bytes);
        let req = self.with_auth(
            self.slow_agent
                .post(&self.url("/attachments/upload"))
                .set("Accept", "application/json")
                .set(
                    "Content-Type",
                    &format!("multipart/form-data; boundary={boundary}"),
                ),
        );
        let resp = req
            .send_bytes(&body)
            .map_err(|e| err_from_ureq("/attachments/upload", e))?;
        Self::read_json("/attachments/upload", resp)
    }

    /// `GET /discovery/models/capabilities?model_name&provider` — model
    /// metadata + execution-host capabilities (the MTP depth check).
    pub fn model_capabilities(&self, provider: &str, model: &str) -> ApiResult<Value> {
        self.get(
            &format!(
                "/discovery/models/capabilities?model_name={}&provider={}",
                urlencode(model),
                urlencode(provider)
            ),
            true,
        )
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

/// A multipart/form-data body (RFC 7578) for the attachment upload. The
/// boundary is checked against the payload so it can never collide.
pub fn multipart_body(
    session_id: &str,
    filename: &str,
    content_type: Option<&str>,
    bytes: &[u8],
) -> (String, Vec<u8>) {
    let mut n: u64 = 0x5a17_c0de;
    let boundary = loop {
        let b = format!("----abstractgateway-console-{n:x}");
        let needle = b.as_bytes();
        if !bytes.windows(needle.len()).any(|w| w == needle) {
            break b;
        }
        n = n.wrapping_mul(31).wrapping_add(7);
    };
    // Quoted header values: drop the two characters that would end them.
    let safe = |s: &str| s.replace(['"', '\r', '\n'], "_");
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len() + 512);
    let mut field = |name: &str, value: &str| {
        out.extend_from_slice(
            format!("--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
                .as_bytes(),
        );
    };
    field("session_id", session_id);
    field("filename", filename);
    if let Some(ct) = content_type {
        field("content_type", ct);
    }
    // Unknown type: no part Content-Type and no `content_type` field —
    // the server then records application/octet-stream, exactly what a
    // browser's upload of an unknown file type yields (live-verified).
    let part_type = content_type
        .map(|ct| format!("Content-Type: {ct}\r\n"))
        .unwrap_or_default();
    out.extend_from_slice(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{}\"\r\n{part_type}\r\n",
            safe(filename),
        )
        .as_bytes(),
    );
    out.extend_from_slice(bytes);
    out.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    (boundary, out)
}

#[cfg(test)]
mod tests {
    use super::multipart_body;

    #[test]
    fn multipart_carries_the_web_fields_and_the_bytes() {
        let (b, body) = multipart_body(
            "gateway_console_sandbox_default_admin",
            "a.png",
            Some("image/png"),
            b"\x89PNGDATA",
        );
        let text = String::from_utf8_lossy(&body);
        assert!(
            text.contains("name=\"session_id\"\r\n\r\ngateway_console_sandbox_default_admin\r\n")
        );
        assert!(text.contains("name=\"filename\"\r\n\r\na.png\r\n"));
        assert!(text.contains("name=\"content_type\"\r\n\r\nimage/png\r\n"));
        assert!(
            text.contains("name=\"file\"; filename=\"a.png\"\r\nContent-Type: image/png\r\n\r\n")
        );
        assert!(body.windows(8).any(|w| w == b"PNGDATA\r"));
        assert!(text.ends_with(&format!("\r\n--{b}--\r\n")));
        let (_, none) = multipart_body("s", "x.bin", None, b"z");
        assert!(
            !String::from_utf8_lossy(&none).contains("name=\"content_type\""),
            "unknown type: no field"
        );
    }
}
