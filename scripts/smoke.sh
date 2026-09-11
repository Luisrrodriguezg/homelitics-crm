#!/usr/bin/env bash
# End-to-end walk through the funnel against a running API.
#
#   export API=http://localhost:8000
#   export TOKEN=...          # a real Supabase access token
#   export CLIENT_ID=... LISTING_ID=...
#   ./scripts/smoke.sh
#
# Against the local compose stack (DEV_AUTH_BYPASS=true) set DEV_AGENT_ID to a
# core.agent UUID instead of TOKEN.
#
# Exits non-zero on the first unexpected status.
set -uo pipefail

API="${API:-http://localhost:8000}"
if [[ -n ${DEV_AGENT_ID:-} ]]; then
  AUTH=(-H "X-Dev-Agent-Id: ${DEV_AGENT_ID}" -H "Content-Type: application/json")
else
  : "${TOKEN:?set TOKEN to a Supabase access token (or DEV_AGENT_ID for the local stack)}"
  AUTH=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")
fi
pass=0; fail=0

req() {  # req METHOD PATH EXPECTED [BODY]
  local method=$1 path=$2 expected=$3 body=${4:-}
  local out code
  if [[ -n $body ]]; then
    out=$(curl -sS -w '\n%{http_code}' -X "$method" "${API}${path}" "${AUTH[@]}" -d "$body")
  else
    out=$(curl -sS -w '\n%{http_code}' -X "$method" "${API}${path}" "${AUTH[@]}")
  fi
  code=${out##*$'\n'}; BODY=${out%$'\n'*}
  if [[ $code == "$expected" ]]; then
    printf '  PASS  %-6s %-42s %s\n' "$method" "$path" "$code"; ((pass++))
  else
    printf '  FAIL  %-6s %-42s got %s want %s\n        %s\n' \
           "$method" "$path" "$code" "$expected" "${BODY:0:300}"; ((fail++))
  fi
}

expect() {  # expect LABEL ACTUAL WANTED
  if [[ $2 == "$3" ]]; then
    printf '  PASS  %-49s %s\n' "$1" "$2"; ((pass++))
  else
    printf '  FAIL  %s: got %s want %s\n' "$1" "$2" "$3"; ((fail++))
  fi
}

jqr() { printf '%s' "$BODY" | python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }
# 'Z', not '+00:00': these also go into query strings, where '+' reads as a space.
iso() { python3 -c "from datetime import*;print((datetime.now(timezone.utc)+timedelta($1)).replace(minute=0,second=0,microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }
plus() { python3 -c "from datetime import*;print((datetime.strptime('$1','%Y-%m-%dT%H:%M:%SZ')+timedelta(minutes=$2)).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }

echo "API ${API}"
echo
echo "identity"
req GET /health 200
req GET /me 200
AGENCY=$(jqr "['agency_id']"); ME=$(jqr "['id']")
echo "        agency ${AGENCY}"

: "${CLIENT_ID:?set CLIENT_ID}"; : "${LISTING_ID:?set LISTING_ID}"
LEAD_BODY=$(printf '{"client_id":"%s","listing_id":"%s","source_channel":"TELEGRAM","message":"smoke test"}' \
            "$CLIENT_ID" "$LISTING_ID")

echo
echo "dedup — same payload twice must give 201 then 200, same id"
req POST /leads 201 "$LEAD_BODY"; LEAD=$(jqr "['id']")
req POST /leads 200 "$LEAD_BODY"; LEAD2=$(jqr "['id']")
expect "dedup returned the same thread" "$LEAD2" "$LEAD"

echo
echo "timeline"
req POST "/leads/${LEAD}/interactions" 201 \
    '{"direction":"OUTBOUND","channel":"TELEGRAM","body":"Hi! When can you visit?"}'
req GET  "/leads/${LEAD}/interactions" 200

echo
echo "visits — a retry returns the same visit; another client's overlapping one 409s"
WHEN=$(iso "days=4"); LATER=$(plus "$WHEN" 30)      # starts inside the first visit
req POST "/leads/${LEAD}/appointments" 201 "{\"scheduled_at\":\"${WHEN}\",\"duration_min\":60}"
APPT=$(jqr "['id']"); VISIT_AGENT=$(jqr "['agent_id']")
req POST "/leads/${LEAD}/appointments" 200 "{\"scheduled_at\":\"${WHEN}\",\"duration_min\":60}"
expect "retry returned the same visit" "$(jqr "['id']")" "$APPT"
req POST /clients 201 '{"full_name":"smoke second client"}'; OTHER_CLIENT=$(jqr "['id']")
req POST /leads 201 "$(printf '{"client_id":"%s","listing_id":"%s","source_channel":"CALL"}' \
                      "$OTHER_CLIENT" "$LISTING_ID")"; OTHER_LEAD=$(jqr "['id']")
req POST "/leads/${OTHER_LEAD}/appointments" 409 "{\"scheduled_at\":\"${LATER}\",\"duration_min\":60}"

echo
echo "calendar views"
req GET "/agents/${VISIT_AGENT}/slots?from=$(iso "days=4")&to=$(iso "days=5")&duration_min=60" 200
req GET "/agents/${VISIT_AGENT}/calendar?from=$(iso "days=3")&to=$(iso "days=6")" 200
req GET "/appointments/${APPT}" 200
req GET "/appointments/${APPT}/invite.ics" 200
req GET /me/calendar-feed 200; FEED=$(jqr "['ics_url']")
code=$(curl -sS -o /dev/null -w '%{http_code}' "$FEED")        # no auth: what Google does
expect "feed fetched with no Authorization header" "$code" "200"

echo
echo "confirm -> complete -> feedback: the calendar moves the funnel"
req PATCH "/appointments/${APPT}" 200 '{"status":"CONFIRMED"}'
req GET "/leads/${LEAD}" 200
expect "confirming scheduled the lead" "$(jqr "['current_stage']")" "VISIT_SCHEDULED"
req PATCH "/appointments/${APPT}" 200 '{"status":"COMPLETED"}'
req GET "/leads/${LEAD}" 200
expect "completing it moved the lead" "$(jqr "['current_stage']")" "VISITED"
req POST  "/appointments/${APPT}/feedback" 201 \
    '{"submitted_by":"AGENT","interest_score":4,"close_probability":0.7}'

echo
echo "funnel — the rest of the legal walk, then an illegal skip"
for st in NEGOTIATING WON; do
  req POST "/leads/${LEAD}/transitions" 201 "{\"to_stage\":\"${st}\"}"
done
req POST "/leads/${LEAD}/transitions" 409 '{"to_stage":"VISITED"}'

echo
echo "analytics"
for ep in funnel-daily agent-response-time listing-performance north-star; do
  req GET "/analytics/${ep}" 200
done

echo
echo "-------------------------------------------"
echo "${pass} passed, ${fail} failed"
exit $(( fail > 0 ))
