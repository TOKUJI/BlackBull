#!/usr/bin/env bash
# bench/conformance/autobahn_assert.sh — fail if any Autobahn case is non-OK.
#
# Shared by the CI Autobahn lanes (fast + heavy): one copy of the gate so the
# two lanes cannot drift.  Accepts an explicit index.json path as $1; without
# it, locates the freshest index.json under bench/conformance/results/autobahn_*/
# (the fast lane has exactly one run's results, so the default is unambiguous;
# the heavy retry wrapper passes its own attempt's path so a failed attempt
# can never be judged against a previous attempt's index).  Fails on any case
# whose behavior / behaviorClose is outside the accepted set
# (OK / NON-STRICT / INFORMATIONAL).

set -euo pipefail

INDEX="${1:-$(ls -1t bench/conformance/results/autobahn_*/index.json 2>/dev/null | head -1)}"
if [ -z "$INDEX" ]; then
    echo "ERROR: no Autobahn index.json produced" >&2
    exit 1
fi
echo "Parsing $INDEX"
# behavior + behaviorClose must both be OK or NON-STRICT; anything else
# (FAILED, UNCLEAN, INFORMATIONAL) is a regression.
BAD=$(jq -r '
    ."BlackBull" | to_entries
    | map(select(
          (.value.behavior != "OK" and .value.behavior != "NON-STRICT" and .value.behavior != "INFORMATIONAL")
          or
          (.value.behaviorClose != "OK" and .value.behaviorClose != "INFORMATIONAL")
      ))
    | map(.key)
    | .[]
' "$INDEX" | tr '\n' ' ')
if [ -n "$BAD" ]; then
    echo "FAIL: Autobahn cases with non-OK behavior: $BAD" >&2
    exit 1
fi
echo "All Autobahn cases pass with OK / NON-STRICT / INFORMATIONAL."
