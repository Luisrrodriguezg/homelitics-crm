#!/usr/bin/env bash
# End-to-end walk through the funnel against a running API.
#
#   export API=http://localhost:8000
#   export TOKEN=...          # a real Supabase access token
#   export CLIENT_ID=... LISTING_ID=...
#   ./scripts/smoke.sh
#
# Exits non-zero on the first unexpected status.
set -uo pipefail

API="${API:-http://localhost:8000}"
: "${TOKEN:?set TOKEN to a Supabase access token}"

AUTH=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")
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

jqr() { printf '%s' "$BODY" | python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }

echo "API ${API}"
echo
echo "identity"
req GET /health 200
req GET /me 200
AGENCY=$(jqr "['agency_id']")
echo "        agency ${AGENCY}"

: "${CLIENT_ID:?set CLIENT_ID}"; : "${LISTING_ID:?set LISTING_ID}"
LEAD_BODY=$(printf '{"client_id":"%s","listing_id":"%s","source_channel":"WHATSAPP","message":"smoke test"}' \
            "$CLIENT_ID" "$LISTING_ID")

echo
echo "dedup — same payload twice must give 201 then 200, same id"
req POST /leads 201 "$LEAD_BODY"; LEAD=$(jqr "['id']")
req POST /leads 200 "$LEAD_BODY"; LEAD2=$(jqr "['id']")
if [[ $LEAD == "$LEAD2" ]]; then
  printf '  PASS  %-49s same id\n' "dedup returned the same thread"; ((pass++))
else
  printf '  FAIL  dedup created a second lead (%s vs %s)\n' "$LEAD" "$LEAD2"; ((fail++))
fi

echo
echo "timeline"
req POST "/leads/${LEAD}/interactions" 201 \
    '{"direction":"OUTBOUND","channel":"WHATSAPP","body":"Hi! When can you visit?"}'
req GET  "/leads/${LEAD}/interactions" 200

echo
echo "visits — the second must 409 on overlap"
WHEN=$(python3 -c "from datetime import*;print((datetime.now(timezone.utc)+timedelta(days=4)).replace(microsecond=0).isoformat())")
LATER=$(python3 -c "from datetime import*;print((datetime.now(timezone.utc)+timedelta(days=4,minutes=30)).replace(microsecond=0).isoformat())")
req POST "/leads/${LEAD}/appointments" 201 "{\"scheduled_at\":\"${WHEN}\",\"duration_min\":60}"
APPT=$(jqr "['id']")
req POST "/leads/${LEAD}/appointments" 409 "{\"scheduled_at\":\"${LATER}\",\"duration_min\":60}"

echo
echo "confirm -> complete -> feedback"
req PATCH "/appointments/${APPT}" 200 '{"status":"CONFIRMED"}'
req PATCH "/appointments/${APPT}" 200 '{"status":"COMPLETED"}'
req POST  "/appointments/${APPT}/feedback" 201 \
    '{"submitted_by":"AGENT","interest_score":4,"close_probability":0.7}'

echo
echo "funnel — legal walk, then an illegal skip"
for st in VISIT_SCHEDULED VISITED NEGOTIATING WON; do
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
