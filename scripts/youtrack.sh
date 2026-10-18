#!/usr/bin/env bash
set -euo pipefail

: "${YOUTRACK_URL:?YOUTRACK_URL is not set}"
: "${YOUTRACK_TOKEN:?YOUTRACK_TOKEN is not set}"

base="${YOUTRACK_URL%/}"
api() {
  if [[ ${1:-} == --multipart ]]; then
    shift
  else
    set -- -H 'Content-Type: application/json' "$@"
  fi
  curl --fail-with-body -sS \
    -H "Authorization: Bearer ${YOUTRACK_TOKEN}" \
    -H 'Accept: application/json' \
    "$@"
}

case "${1:-}" in
  version|version-release)
    if [[ $1 == version-release ]]; then
      [[ $# -eq 3 ]] || { echo 'usage: just yt-version-release NAME YYYY-MM-DD' >&2; exit 2; }
      release_date=$(jq -en --arg date "$3" '
        if ($date | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$")) then
          ($date + "T00:00:00Z" | fromdateiso8601) as $seconds |
          if ($seconds | strftime("%Y-%m-%d")) == $date then $seconds * 1000
          else error("Invalid release date") end
        else error("Expected YYYY-MM-DD") end')
    else
      [[ $# -eq 2 ]] || { echo 'usage: just yt-version NAME' >&2; exit 2; }
    fi
    project=$(api --get "${base}/api/admin/projects" \
      --data-urlencode 'query=BLA' --data-urlencode 'fields=id,shortName' \
      --data-urlencode '$top=100' |
      jq -er '[.[] | select(.shortName == "BLA")] | if length == 1 then .[0].id else error("BLA project is not unique") end')
    bundle=$(api --get "${base}/api/admin/projects/${project}/customFields" \
      --data-urlencode 'fields=field(name),bundle(id)' --data-urlencode '$top=100' |
      jq -er '[.[] | select(.field.name == "Fix versions")] | if length == 1 then .[0].bundle.id else error("Fix versions field is not unique") end')
    values=$(api --get "${base}/api/admin/customFieldSettings/bundles/version/${bundle}/values" \
      --data-urlencode 'fields=id,name,released,archived,releaseDate' --data-urlencode '$top=-1')
    existing=$(jq -c --arg name "$2" '[.[] | select(.name == $name)]' <<< "$values")
    if [[ $1 == version-release ]]; then
      element=$(jq -er 'if length == 1 then .[0].id else error("Version must already exist and be unique") end' <<< "$existing")
      jq -n --argjson date "$release_date" '{released:true,releaseDate:$date}' |
        api -X POST "${base}/api/admin/customFieldSettings/bundles/version/${bundle}/values/${element}?fields=id,name,released,archived,releaseDate" \
          --data-binary @- | jq .
    elif [[ $(jq 'length' <<< "$existing") -gt 0 ]]; then
      jq '.[0]' <<< "$existing"
    else
      jq -n --arg name "$2" '{name:$name,"$type":"VersionBundleElement",released:false}' |
        api -X POST "${base}/api/admin/customFieldSettings/bundles/version/${bundle}/values?fields=id,name,released,archived,releaseDate" \
          --data-binary @- | jq .
    fi
    ;;
  search)
    shift
    query="${*:-project: BLA #Unresolved}"
    api --get "${base}/api/issues" \
      --data-urlencode "query=${query}" \
      --data-urlencode 'fields=idReadable,summary,customFields(name,value(name)),links(direction,linkType(name),issues(idReadable))' \
      --data-urlencode '$top=100' | jq .
    ;;
  show)
    [[ $# -eq 2 ]] || { echo 'usage: just yt-show ISSUE' >&2; exit 2; }
    api "${base}/api/issues/$2" \
      --get --data-urlencode 'fields=idReadable,summary,description,customFields(name,value(name)),comments(text,author(login),created),attachments(id,name,size),links(direction,linkType(name),issues(idReadable))' | jq .
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
  update)
    [[ $# -eq 3 ]] || { echo 'usage: just yt-update ISSUE DESCRIPTION_FILE' >&2; exit 2; }
    [[ -f "$3" ]] || { echo "no such file: $3" >&2; exit 2; }
    jq -n --rawfile description "$3" '{description:$description}' |
      api -X POST "${base}/api/issues/$2?fields=idReadable,summary" --data-binary @- | jq .
    ;;
  attach)
    [[ $# -eq 3 ]] || { echo 'usage: just yt-attach ISSUE FILE' >&2; exit 2; }
    [[ -f "$3" ]] || { echo "no such file: $3" >&2; exit 2; }
    upload_path=${3//\\/\\\\}
    upload_path=${upload_path//\"/\\\"}
    api --multipart -X POST "${base}/api/issues/$2/attachments?fields=id,name,size" \
      -F "upload=@\"$upload_path\"" | jq .
    ;;
  article)
    # The Knowledge Base is a second namespace, not a second project: articles
    # are BLA-A-<n> and live under /api/articles, so an article id handed to
    # the issue endpoint 404s and reads as a typo.  Without an id, list them —
    # BLA-A-10 says nothing about what it holds, and the tree cites articles by
    # id alone because this repository is public and the tracker is not.
    if [[ $# -eq 1 ]]; then
      api --get "${base}/api/articles" \
        --data-urlencode 'fields=idReadable,summary' \
        --data-urlencode '$top=200' | jq .
    else
      [[ $# -eq 2 ]] || { echo 'usage: just yt-article [ARTICLE]' >&2; exit 2; }
      api --get "${base}/api/articles/$2" \
        --data-urlencode 'fields=idReadable,summary,content,updated,parentArticle(idReadable)' | jq .
    fi
    ;;
  article-update)
    # Replace an article's body.  Deliberately a separate verb from the read,
    # and deliberately file-only: an article is reference material that is
    # supposed to sit still, and the ones here are the source the public
    # security page is projected from.  **Ask the user before every run** —
    # see AGENTS.md; this script cannot ask, so the rule lives where sessions
    # read it.
    [[ $# -eq 3 ]] || { echo 'usage: just yt-article-update ARTICLE CONTENT_FILE' >&2; exit 2; }
    [[ -f "$3" ]] || { echo "no such file: $3" >&2; exit 2; }
    jq -n --rawfile content "$3" '{content:$content}' |
      api -X POST "${base}/api/articles/$2?fields=idReadable,summary" --data-binary @- | jq .
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
    echo 'usage: just yt-version NAME | yt-version-release NAME YYYY-MM-DD | yt-search [QUERY] | yt-show ISSUE | yt-create SUMMARY DESCRIPTION | yt-comment ISSUE TEXT | yt-attach ISSUE FILE | yt-update ISSUE DESCRIPTION_FILE | yt-article [ARTICLE] | yt-article-update ARTICLE CONTENT_FILE | yt-command ISSUE COMMAND... | yt-close ISSUE' >&2
    exit 2
    ;;
esac
