-- bench/wrk/no_keepalive.lua — short-lived connections, Sprint 24 Lane E.
--
-- wrk holds connections open across requests by default; with this
-- script every request adds `Connection: close` so the server is
-- forced through accept → TLS handshake → request → close on each
-- iteration.  Exposes the connection-setup cost (accept loop + TLS
-- handshake + first-byte work) that the keep-alive-dominated Lane B
-- hides.
--
-- Usage:
--   wrk -t4 -c256 -d60s -s bench/wrk/no_keepalive.lua https://host/plaintext
--
-- No CLI args.  Request blob is built once in init() so request() is
-- allocation-free.

local request_blob = nil

function init(args)
    wrk.headers["Connection"] = "close"
    request_blob = wrk.format()
end

function request()
    return request_blob
end
