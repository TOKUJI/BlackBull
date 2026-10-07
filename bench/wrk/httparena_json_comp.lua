-- HttpArena's json-comp mix: json-gzip-25.raw, -40.raw and -50.raw in turn.
local requests = {
  "GET /json/25?m=4 HTTP/1.1\r\nHost: localhost:8080\r\nAccept-Encoding: gzip, br\r\n\r\n",
  "GET /json/40?m=8 HTTP/1.1\r\nHost: localhost:8080\r\nAccept-Encoding: gzip, br\r\n\r\n",
  "GET /json/50?m=6 HTTP/1.1\r\nHost: localhost:8080\r\nAccept-Encoding: gzip, br\r\n\r\n",
}
local i = 0
request = function()
  i = i % #requests + 1
  return requests[i]
end
