//! The Runs tab is a technical listing: it shows EVERY session on the
//! gateway (operator ruling, 0.21.0). Since 0.21.0 a `root_only` listing
//! serves conversations only unless the request says `kind=all`, so the
//! console's runs read must ask for it, with root runs only on or off.
//! A local fake gateway (std TcpListener on a free loopback port), never a
//! real one.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::sync::mpsc;

use abstractgateway_console::api::GatewayClient;

/// Serve ONE request, answer an empty runs page, report its path.
fn one_request_path(call: impl FnOnce(&GatewayClient)) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a free port");
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let (stream, _) = listener.accept().expect("one request");
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        loop {
            let mut h = String::new();
            if reader.read_line(&mut h).is_err() || h == "\r\n" || h.is_empty() {
                break;
            }
        }
        let path = line.split_whitespace().nth(1).unwrap_or("").to_string();
        let body = r#"{"items":[],"count":0,"offset":0,"has_more":false}"#;
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let mut stream = stream;
        let _ = stream.write_all(resp.as_bytes());
        tx.send(path).unwrap();
    });
    let client = GatewayClient::new(&url, Some("t"));
    call(&client);
    rx.recv().expect("the fake gateway saw the request")
}

fn query_pairs(path: &str) -> Vec<(String, String)> {
    let query = path.split_once('?').map(|(_, q)| q).unwrap_or("");
    query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (k.to_string(), v.to_string())
        })
        .collect()
}

#[test]
fn the_runs_read_asks_for_every_session_kind() {
    for root_only in [true, false] {
        let path = one_request_path(|c| {
            c.runs(100, 0, "", "", root_only).expect("an empty page");
        });
        assert!(path.contains("/runs?"), "{path}");
        let pairs = query_pairs(&path);
        assert!(
            pairs.contains(&("kind".to_string(), "all".to_string())),
            "root_only={root_only}: the runs read must ask kind=all, sent {path}"
        );
        assert!(
            pairs.contains(&("root_only".to_string(), root_only.to_string())),
            "{path}"
        );
    }
}
