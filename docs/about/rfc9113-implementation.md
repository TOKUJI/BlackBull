# HTTP/2 implementation policies

Use [RFC 9113](https://www.rfc-editor.org/rfc/rfc9113) for wire requirements.
This page records constraints that must survive implementation changes;
`tests/conformance/http2/` and the [conformance workflow](conformance.md)
provide executable checks.

## One core, two roles

`HTTP2Connection` (`blackbull/server/http2_connection.py`) owns frame
admission, stream state, field-block assembly, the control frames, flow
windows and error scope for the server and the client alike. A role decides
only what an opened stream means and whether the peer may push.
`tests/conformance/http2/test_rules_both_roles.py` runs each shared rule on
both roles.

## Connection setup

Bindings select ALPN or prior-knowledge h2c. Keep preface reads and initial
SETTINGS in the HTTP/2 binding/actor boundary; connection detection must not
consume bytes. Upgrade-based h2c and plain CONNECT tunnels are not supported.
RFC 8441 WebSocket requires explicit enablement.

## HPACK and field blocks

Use one `FrameFactory` per connection, including pushes and WebSocket
streams. A second encoder creates a divergent dynamic table.

Peer `SETTINGS_HEADER_TABLE_SIZE` limits the encoder, not the decoder. Apply
entries in order before ACK, capped at the local encoder capacity. Emit the
smallest pending size and final size at the start of the next field block,
before any static-indexed prefix.

Decode every received field block, including one whose stream will be
refused or whose promise will be discarded. Complete CONTINUATION handling
before a stream refusal (including concurrency, priority self-dependency and
input already ended); refusing early must not lose HPACK updates or the
CONTINUATION requirement.

Encoded field accumulation needs byte and time bounds before decoding.
An undecoded or corrupt block affects connection-wide decoder state; it cannot
be handled by resetting only its stream. Keep other frame types off the
connection while a field block is open.

Accumulated CONTINUATION bytes are bounded by `BB_HEADER_MAX_TOTAL`
(client: `BB_CLIENT_HEAD_MAX_TOTAL`) and an unfinished block by
`BB_HEADER_TIMEOUT` (client: `BB_CLIENT_HEAD_TIMEOUT`); both send GOAWAY
`ENHANCE_YOUR_CALM`. Only one block can be open per connection. The shared
decoder separately bounds decompressed fields: server excess logs
`header_max_total` and sends `ENHANCE_YOUR_CALM`; client excess retains
`COMPRESSION_ERROR`. Invalid HPACK also sends `COMPRESSION_ERROR`. Otherwise valid
client pushes are connection errors (`PROTOCOL_ERROR`), never stream-only
refusals.

Encode output headers only when their write will happen, after checking
stream ownership. Mutating the encoder for a block never sent makes later
sibling blocks undecodable.

Read `blackbull/protocol/frame.py`, `frame_types.py` and
`blackbull/server/http2_connection.py`.

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
WebSocket streams, in both roles, read through `HTTP2StreamReader`: octets a
pending read has committed to count as consumed, or a WebSocket frame longer
than the window never completes.

A WINDOW_UPDATE that would raise a window above 2^31-1 is `FLOW_CONTROL_ERROR`
(§6.9.1): GOAWAY for the connection window, RST_STREAM for a stream's. A
SETTINGS_INITIAL_WINDOW_SIZE change that would lift any stream's window past
it is a connection `FLOW_CONTROL_ERROR` (§6.9.2).

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
control frames. SETTINGS, PING and GOAWAY on a non-zero stream, and a
stream-only frame on stream 0, are connection `PROTOCOL_ERROR`s (§6.1–§6.10). A frame whose length or padding breaks its type's layout
raises `FrameFormatError` with the RFC's code, and the frame loop answers it
with GOAWAY: every such case, HEADERS padding included, is a connection error.
Unknown frame types are ignored only outside an open field block.

A frame on a stream this endpoint reset is ignored (§5.1): DATA still
returns its connection credit and a field block is still decoded. On a
stream the peer reset or that ended, HEADERS is a connection
`STREAM_CLOSED` error and other frames are answered with RST_STREAM
`STREAM_CLOSED`, except WINDOW_UPDATE after an ordinary end.

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
