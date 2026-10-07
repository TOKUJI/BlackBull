# Is BlackBull right for your project?

BlackBull serves HTTP/1.1, HTTP/2, WebSocket, gRPC and an MQTT 5 broker in one
asyncio runtime. See [Architecture](../about/architecture.md) for protocol
ownership and [Conformance](../about/conformance.md) for tested coverage.

Before choosing it, check these constraints:

- The API is Early Alpha and may change between MINOR releases.
- It requires asyncio; Trio is unsupported.
- Authentication, rate limiting and database integration are application or
  [extension](../guide/extensions.md) responsibilities.
- The MQTT broker has one worker owner and keeps state in memory; it is not
  a persistent or clustered message bus. See [MQTT limitations](../guide/mqtt.md#limitations).
- [OpenAPI](../guide/openapi.md) documents the supported schema surface; do
  not assume full body-model or security-scheme inference.
- [Fault injection](../guide/fault_injection.md) covers HTTP/1.1 and HTTP/2,
  with an HTTP/1.1 differential oracle; it is not a fault injector for every
  protocol.

For deployment examples, see [Edge inference serving](../guide/edge-inference.md)
and [Protocol translation](../guide/translation-hub.md). Check
[Known limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
before relying on a protocol feature.
