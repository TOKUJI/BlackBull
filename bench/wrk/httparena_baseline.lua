-- HttpArena's baseline mix: get.raw, post_cl.raw and post_chunked.raw in turn.
local requests = {
  "GET /baseline11?a=13&b=42 HTTP/1.1\r\nHost: localhost:8080\r\n\r\n",
  "POST /baseline11?a=13&b=42 HTTP/1.1\r\nHost: localhost:8080\r\nContent-Length: 2\r\n\r\n20",
  "POST /baseline11?a=13&b=42 HTTP/1.1\r\nHost: localhost:8080\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n20\r\n0\r\n\r\n",
}
local i = 0
request = function()
  i = i % #requests + 1
  return requests[i]
end
