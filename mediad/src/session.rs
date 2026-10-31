//! One peer's control channel, as a pipe to the services that own the answers.
//!
//! Transport-agnostic on purpose: this takes lines in and gives lines out, and knows nothing about
//! datachannels. That is what makes it testable without a WebRTC peer — the tests below drive it
//! over channels against fake daemons on real unix sockets — and it is also what would let a
//! WebSocket surface (`remote-webrtc.md` §11) reuse it unchanged.
//!
//! ## What it does not do
//!
//! **Ordinary routed replies are not parsed.** Requests are read far enough to route them;
//! everything a service emits is forwarded verbatim. Two things follow, and both matter:
//!
//! - A subscription works with no special case. It is a stream of notifications on an open
//!   connection, and every one has to reach the peer — which correlating replies to requests would
//!   break, keeping the first and dropping the rest.
//! - Adding a method to the API costs nothing here. `duck-ipc-proto` stays the only place a method
//!   is defined, and this file does not grow a case for it.
//!
//! The local `app.control` extension is a bounded request/reply bridge to the phone app's
//! arbiter. That service authenticates its token and returns one response with the same id.
//!
//! **Ordinary routes do not authenticate.** `remote-webrtc.md` §4: there is no gate on the robot. A LAN peer
//! may drive it, and a bridged peer authenticated to the rendezvous service — on both sides —
//! before arriving. `route::permits` refuses `system.authenticate` by name rather than answering
//! it, so a client that asks gets a clear no instead of a lie.

use duck_ipc_proto as proto;
use std::path::Path;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::mpsc;

use crate::route::{self, Route};
use crate::upstream::Pool;

/// Drive one peer's control channel until its inbound stream ends.
///
/// `inbound` carries one JSON-RPC object per item, as the peer sent it. `outbound` is where replies
/// and notifications go, merged from every service — the peer sorts them out by `id`, which is its
/// business rather than ours.
/// What the video is: the frame the encoder sends, and how far the camera is mounted from upright.
///
/// Held by the session because a peer has to be able to *ask*. Pushing it when the channel appears
/// races the browser: `mediad` writes the line the moment `webrtcsink` hands over the channel, and
/// if the peer's datachannel is not open yet the line is dropped — which is exactly what happened,
/// and the console showed a sideways picture with nothing in the log to say why. The push is kept
/// as a courtesy for a client that only listens; `media.video` as a *call* is what the console uses,
/// because a question it asks when it is ready cannot arrive too early.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Video {
    pub width: u32,
    pub height: u32,
    /// Degrees clockwise the camera is mounted from upright.
    pub rotate: u32,
}

/// The method a peer asks with. Answered here rather than routed: no service owns it.
const VIDEO_METHOD: &str = "media.video";
const APP_CONTROL_METHOD: &str = "app.control";
const APP_CONTROL_SOCKET: &str = "/run/duck-app/control.sock";
const MAX_LINE: usize = 64 * 1024;
const APP_TIMEOUT: Duration = Duration::from_secs(2);

/// What the video is, told to a peer once when its channel opens.
///
/// **The rotation is the whole point.** Nothing on the robot rotates pixels any more — a `videoflip`
/// in the pipeline cost the encoder its zero-copy path and the board 22 fps — so the stream a
/// browser receives is the picture the camera took, sideways on a robot whose camera is mounted a
/// quarter turn off. The page cannot work out by how much: a 180° mount is indistinguishable from an
/// upright one, and even a quarter turn is only a guess from the aspect ratio. So it is told.
///
/// A notification, with no id, because the page already treats an id-less line as something that
/// streams (`robot.state` is the other one) — no new mechanism at either end.
pub fn video_notification(video: Video) -> String {
    let Video {
        width,
        height,
        rotate,
    } = video;
    format!(
        r#"{{"jsonrpc":"2.0","method":"{VIDEO_METHOD}","params":{{"width":{width},"height":{height},"rotate":{rotate}}}}}"#
    )
}

pub async fn run(
    mut inbound: mpsc::Receiver<String>,
    outbound: mpsc::Sender<String>,
    mut pool: Pool,
    video: Video,
) {
    while let Some(line) = inbound.recv().await {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        if let Some(reply) = handle(&line, &mut pool, video).await {
            // A closed outbound means the peer is gone; there is nothing left to do for it.
            if outbound.send(reply).await.is_err() {
                break;
            }
        }
    }
    tracing::debug!("control channel ended");
}

/// Route one line, or return the reply for a session-local method or refusal.
async fn handle(line: &str, pool: &mut Pool, video: Video) -> Option<String> {
    handle_with_app_socket(line, pool, video, Path::new(APP_CONTROL_SOCKET)).await
}

async fn handle_with_app_socket(
    line: &str,
    pool: &mut Pool,
    video: Video,
    app_socket: &Path,
) -> Option<String> {
    if line.len() > MAX_LINE {
        return Some(error_line(
            None,
            proto::Error::new(proto::code::INVALID_REQUEST, "control frame exceeds 64 KiB"),
        ));
    }
    let request: proto::Request = match serde_json::from_str(line) {
        Ok(request) => request,
        Err(e) => {
            // Unparseable, so there is no `id` to answer against and no method to name. A
            // JSON-RPC parse error with a null id is the honest reply.
            tracing::debug!(error = %e, "unparseable frame on the control channel");
            return Some(error_line(
                None,
                proto::Error::new(proto::code::PARSE_ERROR, "not a JSON-RPC object"),
            ));
        }
    };

    // The id, kept before the request is consumed. A notification has none, and `Response`
    // serialises that as `null` — which is right: a refusal to a notification is still worth
    // sending, because silence would look like acceptance.
    let id = request.id.clone();

    if request.method == APP_CONTROL_METHOD {
        let params = request.params.as_ref();
        if request.jsonrpc != "2.0" || line.contains(['\n', '\r']) {
            return Some(error_line(
                id,
                proto::Error::new(
                    proto::code::INVALID_REQUEST,
                    "app.control requires one JSON-RPC 2.0 line",
                ),
            ));
        }
        if !params
            .and_then(|p| p.get("token"))
            .and_then(serde_json::Value::as_str)
            .is_some_and(|token| !token.is_empty() && token.len() <= 512)
            || !params
                .and_then(|p| p.get("command"))
                .is_some_and(serde_json::Value::is_object)
        {
            return Some(error_line(
                id,
                proto::Error::new(
                    proto::code::INVALID_PARAMS,
                    "app.control requires token and command",
                ),
            ));
        }
        let reply =
            tokio::time::timeout(APP_TIMEOUT, app_control_reply(app_socket, line, &id)).await;
        return Some(match reply {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => error_line(
                id,
                proto::Error::new(
                    proto::code::INTERNAL_ERROR,
                    "duck-app control service is unavailable or returned an invalid reply",
                ),
            ),
            Err(_) => error_line(
                id,
                proto::Error::new(
                    proto::code::INTERNAL_ERROR,
                    "duck-app control service timed out",
                ),
            ),
        });
    }

    // Answered here, before anything tries to make a `Call` of it: this is `mediad`'s own question
    // about `mediad`'s own pipeline, and there is no service to route it to.
    if request.method == VIDEO_METHOD {
        return Some(
            serde_json::to_string(&proto::Response::ok(
                id,
                &serde_json::json!({
                    "width": video.width,
                    "height": video.height,
                    "rotate": video.rotate,
                }),
            ))
            .expect("Response serialises"),
        );
    }

    let call = match request.as_call() {
        Ok(call) => call,
        Err(e) => {
            // An unknown method or a params shape this release does not know. `as_call` names
            // which, and that message is the whole value — a version skew fails on the one call
            // that cannot be served rather than on the handshake.
            return Some(error_line(id, e));
        }
    };

    match route::route_for(&call) {
        Route::Refused => {
            tracing::debug!(method = call.method(), "refused over WebRTC");
            Some(error_line(id, route::refusal(&call)))
        }
        Route::To(service, lane) => {
            match pool.send(service, lane, line).await {
                Ok(()) => None,
                Err(e) => {
                    // The service is not there, or stopped reading. Answering is important: a
                    // client that gets silence cannot tell "the robot is thinking" from "nothing
                    // will ever come back", and `robotd` is the service most likely to be missing
                    // because it is the one an update restarts.
                    tracing::debug!(method = call.method(), error = %e, "upstream unreachable");
                    Some(error_line(
                        id,
                        proto::Error::new(
                            proto::code::INTERNAL_ERROR,
                            format!("{:?} is not answering: {e}", service),
                        ),
                    ))
                }
            }
        }
    }
}

async fn app_control_reply(
    socket: &Path,
    line: &str,
    id: &Option<proto::Id>,
) -> std::io::Result<String> {
    let mut stream = UnixStream::connect(socket).await?;
    stream.write_all(line.as_bytes()).await?;
    stream.write_all(b"\n").await?;
    stream.flush().await?;
    // Bound the reader itself; checking length after an unlimited read would still allocate it.
    let mut reader = BufReader::new(stream.take((MAX_LINE + 2) as u64));
    let mut bytes = Vec::new();
    reader.read_until(b'\n', &mut bytes).await?;
    let invalid =
        || std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid app.control reply");
    if bytes.len() > MAX_LINE + 1 || bytes.last() != Some(&b'\n') {
        return Err(invalid());
    }
    let reply = String::from_utf8(bytes).map_err(|_| invalid())?;
    let value: serde_json::Value = serde_json::from_str(&reply).map_err(|_| invalid())?;
    let response: proto::Response = serde_json::from_value(value.clone()).map_err(|_| invalid())?;
    if response.jsonrpc != "2.0"
        || &response.id != id
        || value.get("result").is_some() == value.get("error").is_some()
    {
        return Err(invalid());
    }
    Ok(reply.trim_end().to_owned())
}

/// One refusal, as a line. Built through [`proto::Response`] rather than by hand so the envelope
/// has exactly one definition — the same reason `duck-ipc-proto` exists.
fn error_line(id: Option<proto::Id>, error: proto::Error) -> String {
    // A `Response` cannot fail to serialise: every field is a `String`, an `Id` or an `Error`.
    serde_json::to_string(&proto::Response::err(id, error)).expect("Response serialises")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::upstream::Sockets;
    use std::path::Path;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    /// A daemon that reads one line, replies with the next canned line, and remembers what it saw.
    fn fake_daemon(
        dir: &Path,
        name: &str,
        replies: Vec<String>,
    ) -> mpsc::UnboundedReceiver<String> {
        let path = dir.join(name);
        let listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let listener = tokio::net::UnixListener::from_std(listener).unwrap();
        let (seen_tx, seen_rx) = mpsc::unbounded_channel();

        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let seen = seen_tx.clone();
                let mut replies = replies.clone().into_iter();
                tokio::spawn(async move {
                    let (read, mut write) = stream.into_split();
                    let mut lines = BufReader::new(read).lines();
                    while let Ok(Some(line)) = lines.next_line().await {
                        let _ = seen.send(line);
                        if let Some(reply) = replies.next() {
                            let _ = write.write_all(format!("{reply}\n").as_bytes()).await;
                            let _ = write.flush().await;
                        }
                    }
                });
            }
        });
        seen_rx
    }

    struct Harness {
        to_peer: mpsc::Receiver<String>,
        from_peer: mpsc::Sender<String>,
        _dir: tempfile::TempDir,
    }

    fn harness(sockets: Sockets, dir: tempfile::TempDir) -> Harness {
        let (from_peer, inbound) = mpsc::channel(16);
        let (outbound, to_peer) = mpsc::channel(64);
        let (replies_tx, mut replies_rx) = mpsc::channel::<String>(64);
        let pool = Pool::new(sockets, replies_tx);

        // Everything a service emits goes to the peer, exactly as the datachannel writer will.
        let peer_writer = outbound.clone();
        tokio::spawn(async move {
            while let Some(line) = replies_rx.recv().await {
                if peer_writer.send(line).await.is_err() {
                    break;
                }
            }
        });
        tokio::spawn(run(
            inbound,
            outbound,
            pool,
            Video {
                width: 1280,
                height: 720,
                rotate: 90,
            },
        ));
        Harness {
            to_peer,
            from_peer,
            _dir: dir,
        }
    }

    fn sockets_in(dir: &Path) -> Sockets {
        Sockets {
            updater: dir.join("updaterd.sock"),
            robot: dir.join("robotd.sock"),
            config: dir.join("configd.sock"),
            pad: dir.join("pad.sock"),
            tof: dir.join("tof.sock"),
        }
    }

    async fn app_call(dir: &Path, line: &str) -> serde_json::Value {
        let (tx, _rx) = mpsc::channel(8);
        let mut pool = Pool::new(sockets_in(dir), tx);
        let reply = handle_with_app_socket(
            line,
            &mut pool,
            Video {
                width: 1280,
                height: 720,
                rotate: 90,
            },
            &dir.join("control.sock"),
        )
        .await
        .expect("session-local reply");
        serde_json::from_str(&reply).unwrap()
    }

    const APP_REQUEST: &str = r#"{"jsonrpc":"2.0","id":"phone-7","method":"app.control","params":{"token":"test-token","command":{"kind":"manual","vx":0.1,"vy":0.0,"vyaw":0.0,"seq":7}}}"#;

    #[tokio::test]
    async fn app_control_reaches_only_the_fixed_arbiter_and_preserves_its_reply() {
        let dir = tempfile::tempdir().unwrap();
        let result =
            r#"{"jsonrpc":"2.0","id":"phone-7","result":{"ok":true,"control":{"owner":"manual"}}}"#;
        let mut app_seen = fake_daemon(dir.path(), "control.sock", vec![result.into()]);
        let mut robot_seen = fake_daemon(dir.path(), "robotd.sock", vec![]);
        let reply = app_call(dir.path(), APP_REQUEST).await;
        assert_eq!(app_seen.recv().await.unwrap(), APP_REQUEST);
        assert_eq!(
            reply,
            serde_json::from_str::<serde_json::Value>(result).unwrap()
        );
        assert!(
            robot_seen.try_recv().is_err(),
            "arbiter requests must never bypass arbitration"
        );
    }

    #[tokio::test]
    async fn app_control_forwards_authentication_errors_with_the_request_id() {
        let dir = tempfile::tempdir().unwrap();
        let mut app_seen = fake_daemon(dir.path(), "control.sock", vec![
            r#"{"jsonrpc":"2.0","id":"phone-7","error":{"code":403,"message":"session expired"}}"#.into(),
        ]);
        let reply = app_call(dir.path(), APP_REQUEST).await;
        assert_eq!(reply["id"], "phone-7");
        assert_eq!(reply["error"]["code"], 403);
        assert_eq!(app_seen.recv().await.unwrap(), APP_REQUEST);
    }

    #[tokio::test]
    async fn app_extension_cannot_select_another_method_or_socket() {
        let dir = tempfile::tempdir().unwrap();
        let mut app_seen = fake_daemon(dir.path(), "control.sock", vec![]);
        let reply = app_call(
            dir.path(),
            &APP_REQUEST.replace("app.control", "app.execute"),
        )
        .await;
        assert_eq!(reply["error"]["code"], proto::code::METHOD_NOT_FOUND);
        let reply = app_call(dir.path(), &APP_REQUEST.replace("\"test-token\"", "null")).await;
        assert_eq!(reply["error"]["code"], proto::code::INVALID_PARAMS);
        assert!(app_seen.try_recv().is_err());
    }

    #[tokio::test]
    async fn app_control_rejects_oversized_and_multiline_requests_before_connecting() {
        let dir = tempfile::tempdir().unwrap();
        let mut seen = fake_daemon(dir.path(), "control.sock", vec![]);
        let large = APP_REQUEST.replace("test-token", &"x".repeat(MAX_LINE));
        let reply = app_call(dir.path(), &large).await;
        assert_eq!(reply["error"]["code"], proto::code::INVALID_REQUEST);
        let reply = app_call(
            dir.path(),
            &APP_REQUEST.replace(",\"params\"", ",\n\"params\""),
        )
        .await;
        assert_eq!(reply["error"]["code"], proto::code::INVALID_REQUEST);
        assert!(seen.try_recv().is_err());
    }

    #[tokio::test]
    async fn app_control_rejects_mismatched_ids_and_unbounded_replies() {
        for response in [
            r#"{"jsonrpc":"2.0","id":99,"result":{"ok":true}}"#.to_owned(),
            "x".repeat(MAX_LINE + 1),
            r#"{"jsonrpc":"2.0","id":"phone-7","result":{},"error":{"code":1,"message":"both"}}"#
                .to_owned(),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let _seen = fake_daemon(dir.path(), "control.sock", vec![response]);
            let reply = app_call(dir.path(), APP_REQUEST).await;
            assert_eq!(reply["id"], "phone-7");
            assert_eq!(reply["error"]["code"], proto::code::INTERNAL_ERROR);
        }
    }

    #[tokio::test]
    async fn app_control_unavailable_or_silent_service_returns_a_bounded_error() {
        let dir = tempfile::tempdir().unwrap();
        let missing = app_call(dir.path(), APP_REQUEST).await;
        assert_eq!(missing["id"], "phone-7");
        assert_eq!(missing["error"]["code"], proto::code::INTERNAL_ERROR);
        let _seen = fake_daemon(dir.path(), "control.sock", vec![]);
        let timeout = app_call(dir.path(), APP_REQUEST).await;
        assert_eq!(
            timeout["error"]["message"],
            "duck-app control service timed out"
        );
    }

    #[tokio::test]
    async fn video_only_requests_contact_neither_robot_nor_app_service() {
        let dir = tempfile::tempdir().unwrap();
        let mut robot_seen = fake_daemon(dir.path(), "robotd.sock", vec![]);
        let mut app_seen = fake_daemon(dir.path(), "control.sock", vec![]);
        let reply = app_call(
            dir.path(),
            r#"{"jsonrpc":"2.0","id":1,"method":"media.video"}"#,
        )
        .await;
        assert_eq!(reply["result"]["rotate"], 90);
        assert!(robot_seen.try_recv().is_err());
        assert!(app_seen.try_recv().is_err());
    }

    /// A permitted call reaches the service that owns it, and its reply comes back.
    #[tokio::test]
    async fn a_permitted_call_reaches_its_service() {
        let dir = tempfile::tempdir().unwrap();
        let mut robot_seen = fake_daemon(
            dir.path(),
            "robotd.sock",
            vec![r#"{"jsonrpc":"2.0","id":1,"result":{"healthy":true}}"#.into()],
        );
        let mut updater_seen = fake_daemon(dir.path(), "updaterd.sock", vec![]);
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer
            .send(r#"{"jsonrpc":"2.0","id":1,"method":"robot.health"}"#.into())
            .await
            .unwrap();

        let seen = robot_seen.recv().await.unwrap();
        assert!(seen.contains("robot.health"), "{seen}");
        let reply = h.to_peer.recv().await.unwrap();
        assert!(reply.contains(r#""healthy":true"#), "{reply}");
        // And it did not go to the wrong service.
        assert!(updater_seen.try_recv().is_err());
    }

    /// A refused call is answered by `mediad` and never reaches a socket.
    ///
    /// `net.connect` is the interesting one: it is *permitted over BLE* and refused here, because
    /// reconfiguring wifi would take this session with it.
    #[tokio::test]
    async fn a_refused_call_is_answered_here_and_forwarded_nowhere() {
        let dir = tempfile::tempdir().unwrap();
        let mut config_seen = fake_daemon(dir.path(), "configd.sock", vec![]);
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer
            .send(
                r#"{"jsonrpc":"2.0","id":7,"method":"net.connect","params":{"ssid":"x","psk":"y"}}"#
                    .into(),
            )
            .await
            .unwrap();

        let reply = h.to_peer.recv().await.unwrap();
        assert!(reply.contains("not available over WebRTC"), "{reply}");
        assert!(reply.contains(r#""id":7"#), "{reply}");
        assert!(
            config_seen.try_recv().is_err(),
            "a refused call reached configd"
        );
    }

    /// The pad tap reaches `padd`, which is a socket `btd` deliberately does not hold.
    ///
    /// The concrete difference between the two transports, so it is worth a test of its own rather
    /// than only a route-table assertion.
    #[tokio::test]
    async fn the_pad_tap_reaches_padd() {
        let dir = tempfile::tempdir().unwrap();
        let mut pad_seen = fake_daemon(dir.path(), "pad.sock", vec![]);
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer
            .send(r#"{"jsonrpc":"2.0","id":2,"method":"pad.input"}"#.into())
            .await
            .unwrap();

        let seen = pad_seen.recv().await.unwrap();
        assert!(seen.contains("pad.input"), "{seen}");
        // Nothing came back to the peer: `padd` answers with a stream, and this transport does not
        // invent a reply for it.
        assert!(h.to_peer.try_recv().is_err());
    }

    /// Every notification in a stream reaches the peer.
    ///
    /// This is the case that breaks if replies are ever correlated to requests, which is why it is
    /// pinned here as well as in `btd`.
    #[tokio::test]
    async fn every_notification_in_a_stream_reaches_the_peer() {
        let dir = tempfile::tempdir().unwrap();
        let progress: Vec<String> = (0..3)
            .map(|i| {
                format!(
                    r#"{{"jsonrpc":"2.0","method":"update.progress","params":{{"percent":{}}}}}"#,
                    i * 50
                )
            })
            .collect();
        let _updater_seen = fake_daemon(dir.path(), "updaterd.sock", progress);
        let mut h = harness(sockets_in(dir.path()), dir);

        // One request, then two more lines to make the fake emit its remaining replies.
        for _ in 0..3 {
            h.from_peer
                .send(r#"{"jsonrpc":"2.0","id":3,"method":"update.subscribe"}"#.into())
                .await
                .unwrap();
        }

        let mut seen = 0;
        for _ in 0..3 {
            let line = h.to_peer.recv().await.unwrap();
            assert!(line.contains("update.progress"), "{line}");
            seen += 1;
        }
        assert_eq!(seen, 3);
    }

    /// A missing service is reported, not met with silence.
    ///
    /// `robotd` is the one most likely to be absent, because it is the one an update restarts — and
    /// a peer that gets nothing back cannot tell "thinking" from "never coming".
    #[tokio::test]
    async fn an_absent_service_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        // No fake daemon at all: the socket does not exist.
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer
            .send(r#"{"jsonrpc":"2.0","id":9,"method":"robot.health"}"#.into())
            .await
            .unwrap();

        let reply = h.to_peer.recv().await.unwrap();
        assert!(reply.contains("is not answering"), "{reply}");
        assert!(reply.contains(r#""id":9"#), "{reply}");
    }

    /// An unknown method is refused by name rather than forwarded, which is what makes a version
    /// skew fail on the one call that cannot be served.
    #[tokio::test]
    async fn an_unknown_method_names_itself() {
        let dir = tempfile::tempdir().unwrap();
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer
            .send(r#"{"jsonrpc":"2.0","id":4,"method":"robot.teleport"}"#.into())
            .await
            .unwrap();

        let reply = h.to_peer.recv().await.unwrap();
        assert!(reply.contains("robot.teleport"), "{reply}");
    }

    /// The line that tells a page how the camera is mounted.
    ///
    /// Hand-built JSON, so this is the only thing between a console that rotates the picture and one
    /// that shows it sideways and says nothing.
    #[test]
    fn the_video_notification_carries_the_mount_rotation() {
        let line = video_notification(Video {
            width: 1280,
            height: 720,
            rotate: 90,
        });
        let parsed: serde_json::Value = serde_json::from_str(&line).expect("valid json");
        assert_eq!(parsed["method"], "media.video");
        assert!(parsed.get("id").is_none(), "a notification carries no id");
        assert_eq!(parsed["params"]["width"], 1280);
        assert_eq!(parsed["params"]["height"], 720);
        assert_eq!(parsed["params"]["rotate"], 90);
    }

    /// `media.video` is answered by the session, not routed to a service.
    ///
    /// This is the path the console uses, and it exists because pushing the same information when
    /// the channel appears races the browser's datachannel and loses — a sideways picture with
    /// nothing in the log. A question the page asks when it is ready cannot arrive too early.
    #[tokio::test]
    async fn the_page_can_ask_what_the_video_is() {
        let dir = tempfile::tempdir().unwrap();
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer
            .send(r#"{"jsonrpc":"2.0","id":7,"method":"media.video","params":{}}"#.into())
            .await
            .unwrap();

        let reply = h.to_peer.recv().await.unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&reply).expect("valid json");
        assert_eq!(parsed["id"], 7, "{reply}");
        assert_eq!(parsed["result"]["width"], 1280);
        assert_eq!(parsed["result"]["height"], 720);
        assert_eq!(parsed["result"]["rotate"], 90);
        // No service was involved: it is answered here, and nothing was forwarded.
        assert!(parsed.get("error").is_none(), "{reply}");
    }

    /// Garbage is answered rather than dropped, with a null id because there is none to echo.
    #[tokio::test]
    async fn garbage_gets_a_parse_error() {
        let dir = tempfile::tempdir().unwrap();
        let mut h = harness(sockets_in(dir.path()), dir);

        h.from_peer.send("not json at all".into()).await.unwrap();

        let reply = h.to_peer.recv().await.unwrap();
        assert!(reply.contains("not a JSON-RPC object"), "{reply}");
        assert!(reply.contains(r#""id":null"#), "{reply}");
    }
}
