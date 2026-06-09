-- wrk Lua rotation script for HttpArena-style static-profile reproduction.
--
-- Cycles through the 5 static files in `_local/data/static/` round-robin,
-- mirroring the documented HttpArena behaviour:
--   "wrk with a Lua rotation script that cycles through 20 static files
--    in round-robin fashion. Each request includes
--    Accept-Encoding: br;q=1, gzip;q=0.8."
--
-- We use 5 files because that's what BlackBull's local sandbox ships
-- with precompressed .br/.gz siblings.  Pattern (round-robin + the
-- Accept-Encoding header) is the same — only the dataset breadth
-- differs.  The degradation shape under burst keep-alive is the
-- signal we're after, not absolute r/s.

local paths = {
    "/static/app.js",
    "/static/components.css",
    "/static/helpers.js",
    "/static/layout.css",
    "/static/logo.svg",
}

local idx = 0

request = function()
    idx = idx + 1
    local path = paths[((idx - 1) % #paths) + 1]
    return wrk.format("GET", path, {
        ["Accept-Encoding"] = "br;q=1, gzip;q=0.8",
    })
end
