# HTTP/2 implementation policies

Use [RFC 9113](https://www.rfc-editor.org/rfc/rfc9113) for wire requirements.
This page records constraints that must survive implementation changes;
`tests/conformance/http2/` and the [conformance workflow](conformance.md)
provide executable checks.

## Connection setup

Bindings select ALPN or prior-knowledge h2c. Keep preface reads and initial
SETTINGS in the HTTP/2 binding/actor boundary; connection detection must not
consume bytes. Upgrade-based h2c and plain CONNECT tunnels are not supported.
RFC 8441 WebSocket requires explicit enablement.

## HPACK and field blocks

Use one `FrameFactory` per connection, including pushes and WebSocket
streams. A second encoder creates a divergent dynamic table.

Decode every received field block, including one whose stream will be
refused or whose promise will be discarded. Complete CONTINUATION handling
before a concurrency refusal; refusing early must not turn legal continuation
traffic into an unexpected-frame error.

Encoded field accumulation needs byte and time bounds before decoding.
A refused or corrupt block affects connection-wide decoder state; it cannot
be handled by resetting only its stream. Keep other frame types off the
connection while a field block is open.

Encode output headers only when their write will happen, after checking
stream ownership. Mutating the encoder for a block never sent makes later
sibling blocks undecodable.

Read `blackbull/protocol/frame.py`, `frame_types.py` and
`blackbull/server/http2_actor.py`.

## Flow control

Debit both stream and connection send credit before the first suspension.
Drain completion is not a credit grant; cancellation must not refund bytes
that may already have reached the peer. Remove buffered output before
awaiting its write to avoid duplicate automatic flushes.

Return receive credit on consumption, not enqueue. Connection credit must
survive cancellation of the stream consumer; stream credit must not be
emitted after retirement. Reset, refused DATA and late DATA must return
the connection credit they consumed, including padding, without resurrecting
stream ownership. Frame-count limits are still needed for empty DATA.

Use the existing server/client write-timeout owner for credit waits, not
another timer. Read `HTTP2Sender`, `ConnectionWindow` and `HTTP2Recipient`.

## Stream lifetime and error scope

Request END_STREAM ends input only. Application and response work stay live
until completion or reset. Retire all per-stream owners through one
idempotent transition; task-spawn failure must unwind prepared state.

Closed identifiers need bounded history and separate odd peer-stream and even
push-stream high-water marks. A priority-only node or reset of an unopened
future identifier must not imply that lower identifiers were opened.

Delayed WINDOW_UPDATE can cross terminal output. Preserve that legal timing
race; do not manufacture a failure for a completed response. Peer GOAWAY
stops new work while permitting accepted responses to drain. Connection errors
retire output owners and pending credit work before closing.

Preserve RFC connection-versus-stream error scope when validating fixed-size
control frames. Unknown frame types are ignored only outside an open field
block. Read `HTTP2Actor` and control responders in `blackbull/server/response.py`.

## Message validation

Use the same HTTP field grammar across transports. HTTP/2 additionally
refuses uppercase names and leading/trailing field whitespace. Validate
pseudo-header order and uniqueness, method tokens, URI schemes, target and
authority before dispatch. Normalize the scheme before host and ws/wss
decisions; `:authority` becomes the application's host header.

Malformed trailers are malformed field sections, not successful request
completion. Content-Length counts payload bytes, excluding padding.
Response heads and trailers leave the send boundary with lowercase names.

Read `blackbull/server/parser.py`, `blackbull/protocol/field_grammar.py`
and the HTTP/2 response-validation tests under `tests/unit/client/`.

## Priority and push

Validate and discard deprecated PRIORITY dependencies without creating
persistent nodes. RFC 9218 hints may precede requests but must remain bounded
by concurrent-stream capacity.

Push permission is connection state and must be checked again at send time.
Reserve ownership before the promise write can suspend, then recheck before
dispatch. A reset or cancellation during that write must retire the promised
stream without changing siblings. Push metadata describes a request and
bypasses response middleware.

## Resource limits

Byte, count and time limits answer different inputs. Meter empty/control
frames and both inbound and server-emitted resets before their work occurs.
Probe idle connections with PING rather than closing responsive idle peers.

Use [Environment variables](../reference/env-vars.md) for defaults and
[Security model](security-model.md) for scope. Passing h2spec does not
establish complete server or client conformance.
