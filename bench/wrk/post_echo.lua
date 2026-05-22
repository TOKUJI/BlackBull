-- bench/wrk/post_echo.lua — POST with random body for /echo benchmarking.
--
-- Usage:
--   wrk -t4 -c100 -d30s -s bench/wrk/post_echo.lua -- 1024    https://host/echo
--   wrk -t4 -c50  -d30s -s bench/wrk/post_echo.lua -- 102400  https://host/echo
--
-- Trailing argument is body size in bytes (default 1024).
-- Body is generated once in init() so request() is allocation-free.

local body_size = 1024
local request_blob = nil

function init(args)
    if args[1] then
        body_size = tonumber(args[1]) or 1024
    end
    wrk.method = "POST"
    wrk.body = string.rep("x", body_size)
    wrk.headers["Content-Type"] = "application/octet-stream"
    request_blob = wrk.format()
end

function request()
    return request_blob
end
