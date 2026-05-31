-- bench/soak/mixed.lua — mixed-traffic wrk script for the soak harness.
--
-- Rotates GET requests across three endpoints on bench/app.py to keep
-- the workload from being a single hot path:
--   /plaintext  (13-byte plaintext)
--   /json       (~26-byte JSON)
--   /1kb        (1 KiB binary)
--
-- Requests are pre-formatted in init() so per-request hot path is just
-- a table index — no allocation cost on the load generator side.
--
-- Usage:
--   wrk -t4 -c256 -d3600s -s bench/soak/mixed.lua http://host:8000

local requests
local n = 0
local total = 3

function init(args)
    requests = {
        wrk.format(nil, "/plaintext"),
        wrk.format(nil, "/json"),
        wrk.format(nil, "/1kb"),
    }
end

function request()
    n = n + 1
    return requests[(n % total) + 1]
end
