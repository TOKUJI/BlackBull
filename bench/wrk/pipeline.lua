-- bench/wrk/pipeline.lua — HTTP/1.1 pipelining script for wrk.
--
-- Usage:
--   wrk -t4 -c1024 -d30s -s bench/wrk/pipeline.lua -- 16  https://host/plaintext
--
-- The trailing "-- 16" is the pipeline depth (default 16, matches TechEmpower).
-- Each TCP write batches <depth> serialised GET requests, so per-write
-- syscall cost is amortised across <depth> responses. This is the regime
-- the TechEmpower plaintext board reports.

local depth = 16
local batch = nil

function init(args)
    if args[1] then
        depth = tonumber(args[1]) or 16
    end
    local r = {}
    for i = 1, depth do
        r[i] = wrk.format()
    end
    batch = table.concat(r)
end

function request()
    return batch
end
