//! The plain JSON lane: `GET` / `POST|PUT|DELETE` any `/api/gateway/*`
//! route the web console calls, body in, body out (round 7, R7.2).
//!
//! The parity pages (OpenAI API, Models, Providers' local engines, the
//! Network advanced block, About…) read the SAME routes as the web
//! console with the SAME payloads, so they need no typed client of their
//! own: the page folds the JSON it shows (pure functions, unit-tested
//! beside the page) and this lane stays one transport. Declared as a child
//! of `api` (`#[path]`) so it reuses the retry law and the error taxonomy
//! (a refusal keeps its JSON body on `ApiError::body`).

use serde_json::Value;

use super::{ApiResult, GatewayClient};

impl GatewayClient {
    /// `GET /api/gateway{path}` (idempotent: the socket-death retry applies).
    pub fn json_get(&self, path: &str, slow: bool) -> ApiResult<Value> {
        self.get(path, slow)
    }

    /// `method /api/gateway{path}` with a JSON body (`DELETE` sends none).
    /// Never retried: the gateway's writes are not uniformly idempotent.
    pub fn json_send(
        &self,
        method: &str,
        path: &str,
        body: &Value,
        slow: bool,
    ) -> ApiResult<Value> {
        if method.eq_ignore_ascii_case("DELETE") {
            self.delete(path)
        } else {
            self.send(method, path, body, slow)
        }
    }
}
