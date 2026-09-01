#!/usr/bin/env bash
set -euo pipefail

: "${YOUTRACK_URL:?YOUTRACK_URL is not set}"
: "${YOUTRACK_TOKEN:?YOUTRACK_TOKEN is not set}"

base="${YOUTRACK_URL%/}"
api() {
  curl --fail-with-body -sS \
    -H "Authorization: Bearer ${YOUTRACK_TOKEN}" \
    -H 'Accept: application/json' \
    -H 'Content-Type: application/json' "$@"
}

case "${1:-}" in
  search)
    shift
    query="${*:-project: BLA #Unresolved}"
    api --get "${base}/api/issues" \
      --data-urlencode "query=${query}" \
      --data-urlencode 'fields=idReadable,summary,priority(name),state(name),assignee(login),links(direction,linkType(name),issues(idReadable))' \
      --data-urlencode '$top=100' | jq .
    ;;
  show)
    [[ $# -eq 2 ]] || { echo 'usage: just yt-show ISSUE' >&2; exit 2; }
    api "${base}/api/issues/$2" \
      --get --data-urlencode 'fields=idReadable,summary,description,priority(name),state(name),assignee(login),comments(text,author(login),created),links(direction,linkType(name),issues(idReadable))' | jq .
    ;;
  create)
    [[ $# -ge 3 ]] || { echo 'usage: just yt-create SUMMARY DESCRIPTION' >&2; exit 2; }
    jq -n --arg summary "$2" --arg description "$3" \
      '{project:{shortName:"BLA"},summary:$summary,description:$description}' |
      api -X POST "${base}/api/issues?fields=idReadable,summary" --data-binary @- | jq .
    ;;
  comment)
    [[ $# -ge 3 ]] || { echo 'usage: just yt-comment ISSUE TEXT' >&2; exit 2; }
    jq -n --arg text "$3" '{text:$text}' |
      api -X POST "${base}/api/issues/$2/comments" --data-binary @- | jq .
    ;;
  command)
    [[ $# -ge 3 ]] || { echo 'usage: just yt-command ISSUE COMMAND...' >&2; exit 2; }
    shift
    issue="$1"; shift
    jq -n --arg query "$*" --arg issue "$issue" \
      '{query:$query,issues:[{idReadable:$issue}]}' |
      api -X POST "${base}/api/commands" --data-binary @- | jq .
    ;;
  close)
    [[ $# -eq 2 ]] || { echo 'usage: just yt-close ISSUE' >&2; exit 2; }
    exec "$0" command "$2" 'State Fixed'
    ;;
  *)
    echo 'usage: just yt-search [QUERY] | yt-show ISSUE | yt-create SUMMARY DESCRIPTION | yt-comment ISSUE TEXT | yt-command ISSUE COMMAND... | yt-close ISSUE' >&2
    exit 2
    ;;
esac
