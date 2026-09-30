#!/usr/bin/env bash
# End-to-end smoke test for the Golden Wok voice-ordering backend.
# Exercises the full conversational flow via curl only (no Dialogflow needed).
#
# Usage: ./scripts/e2e.sh [base-url]      (default: http://localhost:8000)
set -u

BASE="${1:-${BASE_URL:-http://localhost:8000}}"
SID="e2e-$(date +%s)"
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1 -- $2"; }

# check <label> <python-expr> ; reads JSON from stdin, evaluates expr with `d`
check() {
  local label="$1" expr="$2" body
  body="$(cat)"
  if python3 -c "import json,sys; d=json.load(sys.stdin); assert ($expr), 'assertion failed'" <<<"$body" 2>/dev/null; then
    ok "$label"
  else
    fail "$label" "$(echo "$body" | head -c 300)"
  fi
}

echo "== e2e against $BASE (session $SID) =="

check "health ok" "d['status']=='ok' and d['restaurant']=='Golden Wok'" \
  <<<"$(curl -s "$BASE/health")"

check "menu has 19 items" "d['item_count']==19 and len(d['items'])==19" \
  <<<"$(curl -s "$BASE/tools/menu")"

check "search 'chicken' finds items" "d['match_count']>=4" \
  <<<"$(curl -s "$BASE/tools/menu/search?q=chicken")"

check "search 'cheeseburger' finds nothing" "d['match_count']==0" \
  <<<"$(curl -s "$BASE/tools/menu/search?q=cheeseburger")"

# "I'd like two egg rolls and a chicken lo mein."
check "add 2 egg rolls -> subtotal 3.50" "d['subtotal']==3.5" \
  <<<"$(curl -s -X POST "$BASE/tools/order/update" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"action\":\"add\",\"item_id\":\"egg-rolls\",\"quantity\":2}")"

# "I want fried rice." without protein -> must ask, not guess
check "fried rice w/o protein -> 422 missing_required_modifiers" \
  "d['error']['code']=='missing_required_modifiers'" \
  <<<"$(curl -s -X POST "$BASE/tools/order/update" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"action\":\"add\",\"item_id\":\"fried-rice\"}")"

check "add chicken lo mein -> subtotal 15.99" "d['subtotal']==15.99" \
  <<<"$(curl -s -X POST "$BASE/tools/order/update" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"action\":\"add\",\"item_id\":\"lo-mein\",\"modifiers\":{\"protein\":\"chicken\"}}")"

# "Actually make that one egg roll."
check "update egg rolls qty 1 -> subtotal 14.24" "d['subtotal']==14.24" \
  <<<"$(curl -s -X POST "$BASE/tools/order/update" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"action\":\"update\",\"item_id\":\"egg-rolls\",\"quantity\":1}")"

check "get_order reflects 2 lines" "d['item_count']==2 and d['state']=='draft'" \
  <<<"$(curl -s "$BASE/tools/order/$SID")"

# "Yes, place the order."
SUBMIT="$(curl -s -X POST "$BASE/tools/order/submit" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$SID\"}")"
check "submit -> GW-#### confirmation" \
  "d['state']=='submitted' and d['confirmation_number'].startswith('GW-')" <<<"$SUBMIT"
echo "    confirmation: $(python3 -c "import json,sys; print(json.load(sys.stdin)['confirmation_number'])" <<<"$SUBMIT")"

check "submit twice is idempotent" "d.get('already_submitted')==True" \
  <<<"$(curl -s -X POST "$BASE/tools/order/submit" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\"}")"

check "update after submit -> 409 order_locked" "d['error']['code']=='order_locked'" \
  <<<"$(curl -s -X POST "$BASE/tools/order/update" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"action\":\"add\",\"item_id\":\"coke\"}")"

check "submit empty order fails" "d['error']['code']=='empty_order'" \
  <<<"$(curl -s -X POST "$BASE/tools/order/submit" -H 'Content-Type: application/json' \
    -d '{"session_id":"e2e-empty-'$SID'"}')"

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
