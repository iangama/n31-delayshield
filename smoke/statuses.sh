#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://localhost:8880}"

WP='{"waypoints":[{"lat":-19.9191,"lon":-43.9386},{"lat":-23.5505,"lon":-46.6333}]}'

mk_deadline() {
  local hours="$1"
  date -u -d "+${hours} hours" +"%Y-%m-%dT%H:%M:%SZ"
}

create_trip() {
  local hours="$1"
  local deadline
  deadline="$(mk_deadline "$hours")"

  local body
  body="$(python3 - <<PY
import json
wp=json.loads('''$WP''')["waypoints"]
print(json.dumps({"deadline_at":"$deadline","waypoints":wp}))
PY
)"

  local out
  out="$(curl -sS -X POST "$BASE/api/trips" -H "Content-Type: application/json" -d "$body")"

  local id
  id="$(python3 - <<PY
import json
j=json.loads('''$out''')
print(j["id"])
PY
)"
  echo "$id"
}

recalc() {
  local id="$1"
  curl -sS -X POST "$BASE/api/trips/$id/recalc" -H "Content-Type: application/json" -d '{}' >/dev/null
}

get_trip() {
  local id="$1"
  curl -sS "$BASE/api/trips/$id"
}

wait_done() {
  local id="$1"
  local timeout_s="${2:-35}"
  local start
  start="$(date +%s)"

  while true; do
    local now
    now="$(date +%s)"
    if (( now - start > timeout_s )); then
      echo "TIMEOUT waiting trip=$id" >&2
      echo "$(get_trip "$id")" >&2
      return 1
    fi

    local j
    j="$(get_trip "$id")"

    local ok
    ok="$(python3 - <<PY
import json, sys
j=json.loads('''$j''')
done = (j.get("status") in ["🟢","🟡","🔴"]) and (j.get("delay_risk_pct") is not None) and (j.get("eta_at") is not None)
print("1" if done else "0")
PY
)"
    if [[ "$ok" == "1" ]]; then
      echo "$j"
      return 0
    fi
    sleep 1.5
  done
}

assert_status() {
  local expected="$1"
  local j="$2"
  python3 - <<PY
import json, sys
j=json.loads('''$j''')
st=j.get("status")
rid=j.get("id")
buf=j.get("buffer_minutes")
risk=j.get("delay_risk_pct")
eta=j.get("eta_at")
dl=j.get("deadline_at")
print(f"id={rid}\\nstatus={st} risk={risk}% buffer_min={buf}\\neta={eta}\\ndeadline={dl}\\n")
if st != "$expected":
  print(f"ASSERT_FAIL expected=$expected got={st}", file=sys.stderr)
  sys.exit(1)
PY
}

echo "== smoke: 🟢 🟡 🔴 via deadlines (same route BH->SP) =="

# 1) GREEN: +12h
ID_G="$(create_trip 12)"
echo "created GREEN id=$ID_G"
recalc "$ID_G"
JG="$(wait_done "$ID_G" 45)"
assert_status "🟢" "$JG"

# 2) YELLOW: +8h
ID_Y="$(create_trip 8)"
echo "created YELLOW id=$ID_Y"
recalc "$ID_Y"
JY="$(wait_done "$ID_Y" 45)"
assert_status "🟡" "$JY"

# 3) RED: +6h
ID_R="$(create_trip 6)"
echo "created RED id=$ID_R"
recalc "$ID_R"
JR="$(wait_done "$ID_R" 45)"
assert_status "🔴" "$JR"

echo "SMOKE_OK ✅"
