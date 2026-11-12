#!/usr/bin/env bash
# Regression suite for Roles (A) and Channels (B): this one script holds every A and B rule.
# Prints only what is broken (REGRESSION FAIL <component> <rule> <why>), then REGRESSION: <n> broken.
# Usage: bash /spec/checks/regression.sh      Exit 0 only if nothing is broken.
#
# The rules are embedded below and unpacked into a temporary folder at run time. Roles 1-70,
# Channels 1-66 and baseline 71-74 run at the same time; then, if enough of the 30 second command
# limit is left, baseline 75-76 kill the HTTP node on 8001 (by the PID that owns the port) and wait
# for /app/start.sh to bring it back.
set -u

# The regression suite always runs in verbose mode: it prints which rule broke and why.
FEEDBACK=verbose   # verbose or score_only

T0=$SECONDS
PY=/opt/test-venv/bin/python; [ -x "$PY" ] || PY=python3
OUT="$(mktemp -d /tmp/regression.XXXXXX)"
trap 'rm -rf "$OUT"' EXIT
say() { [ "$FEEDBACK" = verbose ] && echo "$*"; return 0; }

# ---- the embedded rules (written out from Specs Roles and Channels when /spec was built) ----
printf '%s\n' "FEEDBACK=$FEEDBACK" 'say() { [ "$FEEDBACK" = verbose ] && echo "$*"; return 0; }' >"$OUT/lib.sh"
cat >"$OUT/roles.sh" <<'__REGRESSION_ROLES_RULES__'
#!/usr/bin/env bash
# Self-check for component A. Prints PASS/FAIL per rule in /spec/roles/roles-1..3-*.md.
# Usage: bash /spec/roles/checks/check_roles_all.sh      Exit 0 only if every rule passes.
set -u
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_roles_body.json
pass=0; fail=0
# AIDEV-NOTE: the agent's commands are cut off after 30s with the summary lost. After 22s every
# request fails at once (status 000) so the script still reaches its RESULT line in time. A signal
# trap does not work here: it fires inside a $(...) subshell and its output is swallowed.
T0=$SECONDS
LATE="/tmp/check_roles_late.$$"
trap 'rm -f "$LATE"' EXIT
PW="correct-horse-1"
RUN="$(printf '%x%x' "$(date +%s)" "$RANDOM")"

# AIDEV-NOTE: CHECK_RULES (for example "1-19,63") limits which rules are printed and counted. The
# staged layout uses it to run one stage; every other rule still runs silently as setup.
RULES="${CHECK_RULES:-all}"
want() {
  [ "$RULES" = all ] && return 0
  local t
  for t in ${RULES//,/ }; do
    case "$t" in
      *-*) [ "$1" -ge "${t%-*}" ] && [ "$1" -le "${t#*-}" ] && return 0 ;;
      *) [ "$1" = "$t" ] && return 0 ;;
    esac
  done
  return 1
}
# AIDEV-NOTE: ok and bad run in the main shell (not in a $(...) subshell), so this is the place to
# stop: once a request has hit the deadline, end with the normal RESULT line instead of running on.
late_exit() {
  say "NOTE: the 22 second time budget ran out; this rule and every rule after it were not checked (the app is too slow, or hung)"
  echo "RESULT: $pass passed, $fail failed"
  exit 1
}
ok()  { want "$1" || return 0; pass=$((pass+1)); [ -e "$LATE" ] && late_exit; return 0; }
bad() { [ "$1" = 0 ] || want "$1" || return 0; say "FAIL $1 $2"; fail=$((fail+1)); [ -e "$LATE" ] && late_exit; return 0; }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }
# expect2 <rule> <description> <expected-1> <actual-1> <expected-2> <actual-2>
expect2() {
  if [ "$4" = "$3" ] && [ "$6" = "$5" ]; then ok "$1" "$2"
  else bad "$1" "$2: expected $3 then $5, got $4 then $6"; fi
}

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local left=$((22 - (SECONDS - T0)))
  if [ "$left" -le 0 ]; then : > "$LATE"; echo 000; return; fi
  local mt=4; [ "$left" -lt 4 ] && mt=$left
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time "$mt" -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 5s
}
# field <dotted.path>  -> prints that field of the last response body, or nothing
field() {
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k]
    print(json.dumps(v) if isinstance(v,(dict,list,bool)) or v is None else v)
except Exception: pass' "$BODY" "$1"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}

# AIDEV-NOTE: total wait is capped at 20s so the whole script fits the agent's 30s command limit.
if ! healthy; then
  # AIDEV-NOTE: redirect the whole subshell, not just start.sh: otherwise the subshell keeps this
  # check's stdout open for as long as the cluster runs, and a caller reading that output hangs.
  (cd /app && setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &) </dev/null >/dev/null 2>&1
  for _ in $(seq 1 20); do healthy && break; sleep 1; done
fi
healthy || { say "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

# ---- helpers: users are referred to by a short label ----
tk()  { local v="T_$1"; printf '%s' "${!v}"; }     # token of user <label>
uid() { local v="I_$1"; printf '%s' "${!v}"; }     # id of user <label>
jid() { if [[ "$1" =~ ^[0-9]+$ ]]; then printf '%s' "$1"; else printf '"%s"' "$1"; fi; }
# reg <label>  -> registers user "<label><RUN>" on node 8000; sets REG_S REG_U T_<label> I_<label>
reg() {
  local l="$1" u="$1$RUN"
  REG_S=$(call POST $N0/api/auth/register "{\"username\":\"$u\",\"password\":\"$PW\"}")
  REG_U=$(field user.username)
  printf -v "T_$l" '%s' "$(field token)"; printf -v "I_$l" '%s' "$(field user.id)"
}
# mkws <slug> <label>  -> creates a workspace; sets WS_S (status) and GEN (id of #general)
mkws() { WS_S=$(call POST $N0/api/workspaces "{\"slug\":\"$1\",\"name\":\"Roles $1\"}" "$(tk "$2")"); GEN=$(field general_channel.id); }
# mkch <slug> <name> <private:true|false> <label>  -> status; read the new id afterwards with: field channel.id
mkch() { call POST $N0/api/workspaces/$1/channels "{\"name\":\"$2\",\"is_private\":$3}" "$(tk "$4")"; }
# jn <channel-id> <label> [node]  -> join status
jn() { call POST "${3:-$N0}"/api/channels/$1/join '{}' "$(tk "$2")"; }
# setrole <slug> <target-label> <role> <actor-label>  -> status
setrole() { call PATCH $N0/api/workspaces/$1/members/$(uid "$2") "{\"role\":\"$3\"}" "$(tk "$4")"; }
# role_of <slug> <label> <reader-label>  -> role of the user, read through node 8002
role_of() {
  call GET $N2/api/workspaces/$1/members "" "$(tk "$3")" >/dev/null
  python3 -c 'import json,sys
try:
    for m in json.load(open(sys.argv[1]))["members"]:
        if str(m.get("user_id"))==sys.argv[2]: print(m.get("role")); break
except Exception: pass' "$BODY" "$(uid "$2")"
}
# mkinv <slug> <label> <json-body>  -> status; read the code afterwards with: field invitation.code
mkinv() { call POST $N0/api/workspaces/$1/invitations "$3" "$(tk "$2")"; }
# accept <code> <label> [node]  -> status
accept() { call POST "${3:-$N0}"/api/invitations/$1/accept '{}' "$(tk "$2")"; }

# ---- setup: register every user ----
LABELS="o ad m g x m2 a3 m3 i1 i2 i3 i4 i5 i6 i7 i8 j1 j2 y z k1 k2 k3 k4"
for l in $LABELS; do
  reg $l
  if [ "$l" = o ]; then O_REG_S=$REG_S; O_REG_U=$REG_U; fi
done
[ -n "$(tk o)" ] || { bad 0 "setup: could not register users (rule 1 must work first)"; echo "RESULT: $pass passed, $fail failed"; exit 1; }

# ===== A. Foundations =====
if [ "$O_REG_S" = 201 ] && [ "$O_REG_U" = "o$RUN" ] && [ "${#T_o}" -ge 16 ]; then ok 1 "register returns 201 with user and token"
else bad 1 "register: expected 201, username o$RUN and a token of 16+ chars, got status $O_REG_S, username '$O_REG_U', token length ${#T_o}"; fi

s=$(call POST $N0/api/auth/register "{\"username\":\"o$RUN\",\"password\":\"$PW\"}"); expect 2 "duplicate username" 409 "$s"
s=$(call POST $N0/api/auth/register "{\"username\":\"bad name $RUN\",\"password\":\"$PW\"}"); expect 3 "username with a space" 400 "$s"
s=$(call POST $N0/api/auth/register "{\"username\":\"sp$RUN\",\"password\":\"short77\"}"); expect 4 "7 character password" 400 "$s"

s=$(call POST $N0/api/auth/login "{\"username\":\"o$RUN\",\"password\":\"$PW\"}"); lt=$(field token)
if [ "$s" = 200 ] && [ -n "$lt" ]; then ok 5 "login returns 200 with user and token"; else bad 5 "login: expected 200 with a token, got status $s"; fi
s=$(call POST $N0/api/auth/login "{\"username\":\"o$RUN\",\"password\":\"WRONG12345\"}"); expect 6 "login with wrong password" 401 "$s"

s=$(call GET $N0/api/auth/me "" "$(tk o)"); un=$(field user.username)
if [ "$s" = 200 ] && [ "$un" = "o$RUN" ]; then ok 7 "me returns 200 and the same username"; else bad 7 "me: expected 200 and o$RUN, got $s and '$un'"; fi
s1=$(call GET $N0/api/auth/me); s2=$(call GET $N0/api/auth/me "" "not-a-real-token-000000")
expect2 8 "me without a valid token" 401 "$s1" 401 "$s2"
a=$(call POST $N2/api/auth/login "{\"username\":\"o$RUN\",\"password\":\"$PW\"}"); b=$(call GET $N1/api/auth/me "" "$(tk o)")
expect2 9 "login on 8002 and me on 8001 after register on 8000" 200 "$a" 200 "$b"

S1="r1-$RUN"
mkws "$S1" o; GEN1=$GEN
W_ID=$(field workspace.id); W_SLUG=$(field workspace.slug); W_NAME=$(field workspace.name); W_JM=$(field workspace.join_mode); W_OWN=$(field workspace.owner_id); GN=$(field general_channel.name)
if [ "$WS_S" = 201 ] && [ -n "$W_ID" ] && [ "$W_SLUG" = "$S1" ] && [ -n "$W_NAME" ] && [ -n "$GEN1" ] && [ "$GN" = general ]; then ok 10 "create workspace returns 201 with workspace and #general"
else bad 10 "create workspace: expected 201, workspace{id,slug,name} and general_channel named general, got status $WS_S, slug '$W_SLUG', general '$GN'"; fi
if [ "$W_OWN" = "$(uid o)" ] && [ "$W_JM" = open ]; then ok 11 "owner_id is the creator and join_mode is open"
else bad 11 "owner_id/join_mode: expected $(uid o) and open, got '$W_OWN' and '$W_JM'"; fi
s=$(call POST $N0/api/workspaces "{\"slug\":\"$S1\",\"name\":\"Dup\"}" "$(tk m)"); expect 12 "duplicate slug" 409 "$s"
bads=""
for sl in Acme a "has space" "$(head -c 33 /dev/zero | tr '\0' a)"; do
  s=$(call POST $N0/api/workspaces "{\"slug\":\"$sl\",\"name\":\"Bad\"}" "$(tk m)"); [ "$s" = 400 ] || bads="$bads [${sl:0:8}->$s]"
done
if [ -z "$bads" ]; then ok 13 "invalid slugs return 400"; else bad 13 "invalid slugs: expected 400 for each, got$bads"; fi
s=$(call GET $N0/api/workspaces "" "$(tk o)"); has=$(grep -c "$S1" "$BODY")
s2=$(call GET $N0/api/workspaces "" "$(tk y)"); hasy=$(grep -c "$S1" "$BODY")
if [ "$s" = 200 ] && [ "$has" -ge 1 ] && [ "$s2" = 200 ] && [ "$hasy" = 0 ]; then ok 14 "list shows my workspace and hides others"
else bad 14 "list workspaces: expected 200, own slug present and absent for non-member, got $s/$has and $s2/$hasy"; fi
r=$(role_of "$S1" o o)
if [ "$r" = owner ]; then ok 15 "members lists the creator as owner"; else bad 15 "members: expected creator role owner, got '$r'"; fi
s1=$(call GET $N0/api/workspaces); s2=$(call GET $N0/api/workspaces/$S1/members)
expect2 16 "workspace routes without a token" 401 "$s1" 401 "$s2"
s=$(jn 99999999 o); expect 17 "join unknown channel" 404 "$s"
s=$(jn "$GEN1" x); r=$(role_of "$S1" x o)
if [ "$s" = 200 ] && [ "$r" = member ]; then ok 18 "open workspace: join returns 200 and adds a member"
else bad 18 "open join: expected 200 and role member, got $s and '$r'"; fi
s=$(mkch "$S1" secret true o); SEC=$(field channel.id); s=$(jn "$SEC" y); expect 19 "join a private channel without invite" 403 "$s"

# ===== B. Roles =====
for l in ad m g m2 a3 m3; do jn "$GEN1" $l >/dev/null; done
s=$(setrole "$S1" ad admin o); r=$(field member.role)
if [ "$s" = 200 ] && [ "$r" = admin ]; then ok 20 "owner promotes member to admin"; else bad 20 "owner promote: expected 200 and admin, got $s and '$r'"; fi
setrole "$S1" a3 admin o >/dev/null
s=$(setrole "$S1" a3 member o); r=$(field member.role); s2=$(setrole "$S1" m3 guest o); r2=$(field member.role)
if [ "$s" = 200 ] && [ "$r" = member ] && [ "$s2" = 200 ] && [ "$r2" = guest ]; then ok 21 "owner demotes admin to member and member to guest"
else bad 21 "owner demote: expected 200/member and 200/guest, got $s/'$r' and $s2/'$r2'"; fi
setrole "$S1" g guest o >/dev/null
ra=$(role_of "$S1" ad o); rb=$(role_of "$S1" a3 o); rc=$(role_of "$S1" m3 o); rd=$(role_of "$S1" g o)
if [ "$ra" = admin ] && [ "$rb" = member ] && [ "$rc" = guest ] && [ "$rd" = guest ]; then ok 22 "role changes persist and are visible on another node"
else bad 22 "persist: expected admin/member/guest/guest, got $ra/$rb/$rc/$rd"; fi
s=$(setrole "$S1" m2 admin ad); expect 23 "admin promotes a member to admin" 200 "$s"
s=$(setrole "$S1" m2 member ad); expect 24 "admin cannot modify another admin" 403 "$s"
s=$(setrole "$S1" o member ad); expect 25 "admin cannot modify the owner" 403 "$s"
s=$(setrole "$S1" o admin o); expect 26 "owner cannot change own role" 403 "$s"
s=$(setrole "$S1" m owner o); expect 27 "role owner via PATCH is rejected" 400 "$s"
s=$(setrole "$S1" g member m); expect 28 "member cannot change roles" 403 "$s"
s=$(setrole "$S1" m admin g); expect 29 "guest cannot change roles" 403 "$s"

S2="r2-$RUN"
mkws "$S2" o; GEN2=$GEN
for l in ad m; do jn "$GEN2" $l >/dev/null; done
setrole "$S2" ad admin o >/dev/null
tr_() { call POST $N0/api/workspaces/$1/transfer_ownership "{\"new_owner_id\":$(jid "$(uid "$2")")}" "$(tk "$3")"; }
s=$(tr_ "$S2" ad ad); expect 30 "admin cannot transfer ownership" 403 "$s"
s=$(tr_ "$S2" m o); expect 31 "transfer to a member is rejected" 400 "$s"
s=$(tr_ "$S2" ad o); expect 32 "owner transfers to an admin" 200 "$s"
r1=$(role_of "$S2" ad ad); r2=$(role_of "$S2" o ad); call GET $N1/api/workspaces/$S2 "" "$(tk ad)" >/dev/null; own=$(field workspace.owner_id)
if [ "$r1" = owner ] && [ "$r2" = admin ] && [ "$own" = "$(uid ad)" ]; then ok 33 "after transfer: new owner, old owner is admin, owner_id updated"
else bad 33 "transfer result: expected owner/admin and owner_id $(uid ad), got $r1/$r2 and '$own'"; fi
s=$(tr_ "$S2" ad o); expect 34 "previous owner cannot transfer again" 403 "$s"

s=$(mkch "$S1" lobby false o); LOBBY=$(field channel.id)
s=$(jn "$LOBBY" g); expect 35 "guest cannot join a public channel" 403 "$s"
s=$(mkch "$S1" guest-chan false g); expect 36 "guest cannot create a channel" 403 "$s"
s=$(mkch "$S1" m-pub false m); expect 37 "member creates a public channel" 201 "$s"
s=$(mkch "$S1" m-priv true m); expect 38 "member cannot create a private channel" 403 "$s"
s=$(mkch "$S1" ad-priv true ad); expect 39 "admin creates a private channel" 201 "$s"
s=$(mkch "$S1" o-priv true o); expect 40 "owner creates a private channel" 201 "$s"

# ===== C. Invitations =====
S3="r3-$RUN"
mkws "$S3" o; GEN3=$GEN
for l in ad m g; do jn "$GEN3" $l >/dev/null; done
setrole "$S3" ad admin o >/dev/null; setrole "$S3" g guest o >/dev/null
s=$(mkinv "$S3" o '{}'); C1=$(field invitation.code)
if [ "$s" = 201 ] && [ -n "$C1" ]; then ok 41 "owner creates an invitation with a code"; else bad 41 "owner invitation: expected 201 and a code, got $s and '$C1'"; fi
s=$(mkinv "$S3" ad '{}'); expect 42 "admin creates an invitation" 201 "$s"
s1=$(mkinv "$S3" m '{}'); s2=$(mkinv "$S3" g '{}'); expect2 43 "member and guest cannot create invitations" 403 "$s1" 403 "$s2"
sa=$(accept "$C1" i1); sb=$(accept "$C1" i2 $N1); expect2 44 "default max_uses is 1 (accept, then accept again)" 200 "$sa" 404 "$sb"
s=$(mkinv "$S3" o '{"max_uses":2}'); C2=$(field invitation.code)
sa=$(accept "$C2" i3); sb=$(accept "$C2" i4 $N2); sc=$(accept "$C2" i5)
if [ "$sa" = 200 ] && [ "$sb" = 200 ] && [ "$sc" = 404 ]; then ok 45 "max_uses 2 allows two accepts then 404"
else bad 45 "max_uses 2: expected 200, 200, 404, got $sa, $sb, $sc"; fi
r=$(role_of "$S3" i1 o)
if [ "$r" = member ]; then ok 46 "accepting an invitation adds a member"; else bad 46 "accept: expected i1 listed as member, got '$r'"; fi
s=$(accept "nosuchcode$RUN" i6); expect 47 "unknown code" 404 "$s"
s=$(mkinv "$S3" o '{"expires_in_seconds":1}'); CEXP=$(field invitation.code)
s=$(mkinv "$S3" o '{"max_uses":5}'); CREV=$(field invitation.code)
s=$(mkinv "$S3" o '{"max_uses":5}'); CA=$(field invitation.code)
sleep 2
s=$(accept "$CEXP" i6); expect 48 "expired invitation" 404 "$s"
s=$(call DELETE $N0/api/workspaces/$S3/invitations/$CREV "" "$(tk o)")
if [ "$s" = 200 ] && grep -Eq '"deleted": *true' "$BODY"; then dl=ok; else dl=no; fi
sb=$(accept "$CREV" i6)
if [ "$dl" = ok ] && [ "$sb" = 404 ]; then ok 49 "revoke returns {deleted:true} and the code then returns 404"
else bad 49 "revoke: expected 200 with deleted true then 404, got $s ($dl) then $sb"; fi
s=$(mkinv "$S3" o "{\"invited_user_id\":$(jid "$(uid i7)")}"); CT=$(field invitation.code)
sa=$(accept "$CT" i8); sb=$(accept "$CT" i7)
expect2 50 "targeted invitation: other user then target user" 403 "$sa" 200 "$sb"
sc=$(accept "$CT" i7); expect 51 "targeted invitation is single-use" 404 "$sc"
s=$(call GET $N0/api/workspaces/$S3/invitations "" "$(tk o)"); a1=$(grep -c "$CA" "$BODY"); a2=$(grep -c "$CREV" "$BODY")
s2=$(call GET $N1/api/workspaces/$S3/invitations "" "$(tk ad)")
if [ "$s" = 200 ] && [ "$s2" = 200 ] && [ "$a1" -ge 1 ] && [ "$a2" = 0 ]; then ok 52 "list shows active codes and hides revoked ones"
else bad 52 "list invitations: expected 200/200, active present, revoked absent, got $s/$s2, $a1, $a2"; fi
s=$(call GET $N0/api/workspaces/$S3/invitations "" "$(tk m)"); expect 53 "member cannot list invitations" 403 "$s"
s=$(call DELETE $N0/api/workspaces/$S3/invitations/$CA "" "$(tk m)"); expect 54 "member cannot revoke an invitation" 403 "$s"
s=$(call POST $N0/api/invitations/$CA/accept '{}'); expect 55 "accept without a token" 401 "$s"

# ===== D. Join mode =====
S4="r4-$RUN"
mkws "$S4" o; GEN4=$GEN
for l in ad m; do jn "$GEN4" $l >/dev/null; done
setrole "$S4" ad admin o >/dev/null
s=$(mkch "$S4" lobby false o); LB4=$(field channel.id); s=$(mkch "$S4" lobby2 false o); LB42=$(field channel.id)
s=$(call PATCH $N0/api/workspaces/$S4 '{"join_mode":"invite_only"}' "$(tk o)"); jm=$(field workspace.join_mode)
if [ "$s" = 200 ] && [ "$jm" = invite_only ]; then ok 56 "owner sets join_mode to invite_only"; else bad 56 "set join_mode: expected 200 and invite_only, got $s and '$jm'"; fi
s=$(call PATCH $N0/api/workspaces/$S4 '{"name":"Renamed"}' "$(tk ad)"); nm=$(field workspace.name)
if [ "$s" = 200 ] && [ "$nm" = Renamed ]; then ok 57 "admin renames the workspace"; else bad 57 "rename: expected 200 and Renamed, got $s and '$nm'"; fi
s=$(call PATCH $N0/api/workspaces/$S4 '{"name":"Nope"}' "$(tk m)"); expect 58 "member cannot PATCH the workspace" 403 "$s"
s=$(jn "$LB42" m); expect 59 "invite_only: existing member can still join a public channel" 200 "$s"
s=$(jn "$LB4" x); r=$(role_of "$S4" x o)
if [ "$s" = 403 ] && [ -z "$r" ]; then ok 60 "invite_only: non-member gets 403 and is not added"; else bad 60 "invite_only join: expected 403 and no membership, got $s and role '$r'"; fi
s=$(mkinv "$S4" o '{}'); CI=$(field invitation.code); sa=$(accept "$CI" x); sb=$(jn "$LB4" x)
expect2 61 "invite_only: redeem an invitation, then join" 200 "$sa" 200 "$sb"
call PATCH $N0/api/workspaces/$S4 '{"join_mode":"open"}' "$(tk o)" >/dev/null
s=$(jn "$LB4" y); r=$(role_of "$S4" y o)
if [ "$s" = 200 ] && [ "$r" = member ]; then ok 62 "back to open: non-member joins and becomes a member"; else bad 62 "open again: expected 200 and member, got $s and '$r'"; fi

# ===== E. Cluster baseline =====
ids=""
for p in 8000 8001 8002; do s=$(call GET http://127.0.0.1:$p/api/health); ids="$ids $s/$(field node_id)"; done
if [ "$ids" = " 200/0 200/1 200/2" ]; then ok 63 "every node reports its own node_id"; else bad 63 "health: expected 200/0 200/1 200/2, got$ids"; fi

# ===== F. Stricter edge cases =====
s=$(setrole "$S1" m superuser o); expect 64 "an unknown role value returns 400" 400 "$s"
s=$(setrole "$S1" k1 member o); expect 65 "role change for a user who is not in the workspace returns 404" 404 "$s"
s=$(mkinv "$S3" o "{\"invited_username\":\"k2$RUN\"}"); CU=$(field invitation.code)
sa=$(accept "$CU" k3); sb=$(accept "$CU" k2)
expect2 66 "invited_username invitation: other user then named user" 403 "$sa" 200 "$sb"
s=$(mkinv "$S3" o "{\"invited_user_id\":$(jid "$(uid k4)"),\"max_uses\":5}"); CM=$(field invitation.code)
sa=$(accept "$CM" k4); sb=$(accept "$CM" k4)
expect2 67 "targeted invitation is single-use even with max_uses 5" 200 "$sa" 404 "$sb"
s1=$(mkinv "$S3" o '{"max_uses":0}'); s2=$(mkinv "$S3" o '{"max_uses":"abc"}')
expect2 68 "max_uses of 0 and of a non-number return 400" 400 "$s1" 400 "$s2"
s=$(call PATCH $N0/api/workspaces/$S3 '{"join_mode":"closed"}' "$(tk o)"); expect 69 "an unknown join_mode returns 400" 400 "$s"
s1=$(call POST $N0/api/auth/register "{\"username\":\"np$RUN\"}"); s2=$(call POST $N0/api/auth/register '{}')
s3=$(call POST $N0/api/auth/login "{\"username\":\"nobody$RUN\",\"password\":\"$PW\"}")
if [ "$s1" = 400 ] && [ "$s2" = 400 ] && [ "$s3" = 401 ]; then ok 70 "register without a password or with {} returns 400, login for an unknown user 401"
else bad 70 "auth edge cases: expected 400, 400, 401, got $s1, $s2, $s3"; fi

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
__REGRESSION_ROLES_RULES__
cat >"$OUT/channels.py" <<'__REGRESSION_CHANNELS_RULES__'
#!/usr/bin/env python3
"""Checks for component B (channels and message history). Prints PASS/FAIL per rule."""
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websockets
except ImportError:  # the WebSocket rules then fail with a clear message
    websockets = None

N0, N1, N2 = "http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002"
PW = "correct-horse-1"
RUN = "%x%x" % (int(time.time()), random.randrange(4096))
T0 = time.time()


def _feedback():
    """The testing mode, read from the FEEDBACK= line of lib.sh next to this file."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib.sh")) as f:
            for line in f:
                m = re.match(r"\s*FEEDBACK=(\w+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "verbose"


FEEDBACK = _feedback()


def say(text):
    """Prints a PASS, FAIL or NOTE line, only in verbose mode."""
    if FEEDBACK == "verbose":
        print(text, flush=True)
# AIDEV-NOTE: the agent's commands are cut off after 30s; stop starting new WebSocket rules at 22s.
BUDGET = 18

passed = failed = 0
done = set()
# AIDEV-NOTE: CHECK_RULES (for example "1-14,66") limits which rules are printed and counted. The
# staged layout uses it to run one stage; every other rule still runs silently as setup.
RULES = os.environ.get("CHECK_RULES", "all")


def want(rule):
    if RULES == "all" or rule == 0:
        return True
    for tok in RULES.split(","):
        lo, _, hi = tok.partition("-")
        if int(lo) <= rule <= int(hi or lo):
            return True
    return False


# AIDEV-NOTE: the agent's commands are cut off after 30s with no output kept for the summary. This
# watchdog prints every rule that was not reached as FAIL, then RESULT, and exits at 22s.
TOTAL_RULES = 66


def _time_is_up():
    missing = [r for r in range(1, TOTAL_RULES + 1) if want(r) and r not in done]
    for r in missing:
        say("FAIL %s not reached: the check's 22 second time budget ran out (%s)" % (r, _slowest() or "earlier rules were slow"))
    print("RESULT: %d passed, %d failed" % (passed, failed + len(missing)), flush=True)
    os._exit(1)


def ok(rule, msg):
    global passed
    done.add(rule)
    if want(rule):
        passed += 1


def bad(rule, msg):
    global failed
    done.add(rule)
    if want(rule):
        failed += 1
        say("FAIL %s %s" % (rule, msg))


def check(rule, msg, cond, detail=""):
    if cond:
        ok(rule, msg)
    else:
        bad(rule, "%s: %s" % (msg, detail))


# AIDEV-NOTE: every request shares one deadline so a hung server cannot push the whole check past
# the agent's 30s command limit; after DEADLINE the remaining rules fail fast and RESULT still prints.
DEADLINE = 21


def call(method, url, body=None, token=None, timeout=5):
    """Returns (status, parsed-json-or-None). Status 0 means no answer."""
    left = DEADLINE - (time.time() - T0)
    if left <= 0:
        return 0, None
    timeout = min(timeout, max(left, 0.5))
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception:
        return 0, None
    try:
        return status, (json.loads(raw) if raw else None)
    except Exception:
        return status, None


def dig(d, path):
    for k in path.split("."):
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d


def healthy():
    for u in (N0, N1, N2):
        if call("GET", u + "/api/health", timeout=2)[0] != 200:
            return False
    return True


def boot():
    if not healthy():
        try:
            log = open("/tmp/start.log", "w")
            subprocess.Popen(["/app/start.sh"], cwd="/app", stdout=log, stderr=log, start_new_session=True)
        except OSError as e:
            say("FAIL 0 could not start /app/start.sh: %s" % e)
        for _ in range(20):
            if healthy():
                break
            time.sleep(1)
    if not healthy():
        say("FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)")
        print("RESULT: 0 passed, 1 failed")
        sys.exit(1)


# ---- helpers ----
def reg(label):
    name = "%s%s" % (label, RUN)
    st, d = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW})
    return {"name": name, "token": dig(d, "token"), "id": dig(d, "user.id")}


def mkws(slug, u):
    return call("POST", N0 + "/api/workspaces", {"slug": slug, "name": "Chan " + slug}, u["token"])


def mkch(slug, name, u, private=False, topic=None):
    body = {"name": name, "is_private": private}
    if topic is not None:
        body["topic"] = topic
    return call("POST", "%s/api/workspaces/%s/channels" % (N0, slug), body, u["token"])


def join(cid, u, node=N0):
    return call("POST", "%s/api/channels/%s/join" % (node, cid), {}, u["token"])[0]


def post(cid, u, body, parent=None, node=N0):
    payload = {"body": body}
    if parent is not None:
        payload["parent_id"] = parent
    return call("POST", "%s/api/channels/%s/messages" % (node, cid), payload, u["token"])


def messages(cid, u, qs="", node=N0):
    return call("GET", "%s/api/channels/%s/messages%s" % (node, cid, qs), None, u["token"])


def bodies(items):
    return [(m.get("body") if "body" in m else (m.get("message") or {}).get("body")) for m in (items or [])]


def q(cursor):
    return urllib.parse.quote(str(cursor), safe="")


def ids(items):
    return sorted(str(x) for x in items or [])


def newchan(slug, name, u, **kw):
    st, d = mkch(slug, name, u, **kw)
    return dig(d, "channel.id")


def read_state(cid, u, node=N0):
    return call("GET", "%s/api/channels/%s/read" % (node, cid), None, u["token"])


# ---- WebSocket helpers ----
async def ws_open(port, token):
    return await websockets.connect("ws://127.0.0.1:%d/api/ws?token=%s" % (port, token), open_timeout=3)


async def wait_for(ws, pred, timeout, seen=None):
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        try:
            f = json.loads(await asyncio.wait_for(ws.recv(), left))
        except Exception:
            return None
        if seen is not None:
            seen.append(f)
        if pred(f):
            return f


async def subscribe(ws, cid, timeout=3):
    await ws.send(json.dumps({"type": "subscribe", "channel_id": cid}))
    return await wait_for(ws, lambda f: f.get("type") == "subscribed" and str(f.get("channel_id")) == str(cid), timeout)


async def collect(ws, cid, want, timeout, kinds=None):
    """Collect durable frames of this channel until `want` are seen or the timeout ends."""
    got = []
    end = time.time() + timeout
    while len(got) < want and end > time.time():
        f = await wait_for(ws, lambda f: str(f.get("channel_id")) == str(cid) and "event_id" in f
                           and (kinds is None or f.get("type") in kinds), end - time.time())
        if f is None:
            break
        got.append(f)
    return got


def head_of(cid, token):
    async def go():
        ws = await ws_open(8000, token)
        try:
            f = await subscribe(ws, cid)
            return f.get("head_event_id") if f else None
        finally:
            await ws.close()
    try:
        return asyncio.run(asyncio.wait_for(go(), 6))
    except Exception:
        return None


SLOW = []  # (rules, seconds) of each WebSocket rule group that has run, to name what used the budget


def _slowest():
    top = sorted(SLOW, key=lambda x: -x[1])[:3]
    return ", ".join("rule%s %s took %.1f s" % ("s" if len(r) > 1 else "", "-".join(str(x) for x in (r[0], r[-1]) if len(r) > 1) or r[0], t)
                     for r, t in top if t >= 0.5)


async def guarded(rules, fn, limit=9):
    # AIDEV-NOTE: the WebSocket rules set up nothing that later rules use, so a staged slice that does
    # not include them skips them entirely instead of spending its time budget on them.
    if not any(want(r) for r in rules):
        return
    if websockets is None:
        for r in rules:
            bad(r, "the python `websockets` package is not available to the check")
        return
    if time.time() - T0 > BUDGET:
        for r in rules:
            bad(r, "skipped: the check's time budget is used up (%s; each should take about a second)" % (_slowest() or "earlier rules were slow"))
        return
    t0 = time.time()
    lim = max(1, min(limit, DEADLINE + 2 - (time.time() - T0)))
    try:
        await asyncio.wait_for(fn(), lim)
    except asyncio.TimeoutError:
        for r in rules:
            if r not in done:
                bad(r, "no result within %.1f s: an expected WebSocket frame never arrived, or the server answered too slowly" % lim)
    except Exception as e:
        for r in rules:
            if r not in done:
                bad(r, "error or timeout (%s %s)" % (type(e).__name__, e))
    finally:
        SLOW.append((list(rules), time.time() - t0))
    for r in rules:
        if r not in done:
            bad(r, "no result")


def main():
    wd = threading.Timer(22, _time_is_up)
    wd.daemon = True
    wd.start()
    boot()
    o, m, x, y = reg("o"), reg("m"), reg("x"), reg("y")
    if not o["token"]:
        say("FAIL 0 setup: could not register users (component A must work first)")
        print("RESULT: 0 passed, 1 failed")
        sys.exit(1)
    S = "c1-" + RUN
    st, d = mkws(S, o)
    WS1 = dig(d, "workspace.id")
    GEN = dig(d, "general_channel.id")
    for u in (m, x):
        join(GEN, u)  # open workspace: becomes a member

    # ===== A. Channel management =====
    st, d = mkch(S, "alpha", o)
    ch = dig(d, "channel") or {}
    keys = ["id", "workspace_id", "name", "is_private", "is_dm", "topic", "is_archived"]
    ALPHA = ch.get("id")
    check(1, "create returns 201 and a full channel with defaults",
          st == 201 and all(k in ch for k in keys) and ch.get("topic") == "" and ch.get("is_private") is False
          and ch.get("is_dm") is False and ch.get("is_archived") is False,
          "status %s, channel %s" % (st, json.dumps(ch)[:200]))
    s1, d1 = mkch(S, "topic250", o, topic="t" * 250)
    s2, _ = mkch(S, "topic251", o, topic="t" * 251)
    check(2, "topic of 250 chars is accepted, 251 is rejected", s1 == 201 and len(dig(d1, "channel.topic") or "") == 250 and s2 == 400,
          "250 -> %s, 251 -> %s" % (s1, s2))
    got = [mkch(S, n, o)[0] for n in ("Upper", "has space", "a" * 33)]
    check(3, "invalid channel names return 400", got == [400, 400, 400], "got %s" % got)
    st, d = mkws("c1b-" + RUN, o)
    s1 = mkch(S, "alpha", o)[0]
    s2 = mkch("c1b-" + RUN, "alpha", o)[0]
    check(4, "duplicate name 409, same name in another workspace 201", s1 == 409 and s2 == 201, "got %s and %s" % (s1, s2))
    st, d = call("GET", "%s/api/workspaces/%s" % (N0, S), None, o["token"])
    names = [c.get("name") for c in (d or {}).get("channels", [])]
    check(5, "workspace detail has workspace, channels, read_state and general",
          st == 200 and d and "workspace" in d and "read_state" in d and "general" in names, "status %s, channels %s" % (st, names))
    SEC = newchan(S, "secret", o, private=True)
    names_x = [c.get("name") for c in (call("GET", "%s/api/workspaces/%s" % (N0, S), None, x["token"])[1] or {}).get("channels", [])]
    names_o = [c.get("name") for c in (call("GET", "%s/api/workspaces/%s" % (N0, S), None, o["token"])[1] or {}).get("channels", [])]
    check(6, "private channel is hidden from non-members and shown to members", "secret" not in names_x and "secret" in names_o,
          "x sees %s, o sees %s" % (names_x, names_o))
    OLD = newchan(S, "old", o)
    call("POST", "%s/api/channels/%s/archive" % (N0, OLD), {}, o["token"])
    def listed(suffix):
        return "old" in [c.get("name") for c in (call("GET", "%s/api/workspaces/%s%s" % (N0, S, suffix), None, o["token"])[1] or {}).get("channels", [])]
    a, b, c = listed(""), listed("?include_archived=1"), listed("?include_archived=true")
    check(7, "archived channels are hidden by default and shown with include_archived", (a, b, c) == (False, True, True),
          "default %s, =1 %s, =true %s" % (a, b, c))
    join(ALPHA, m)
    MCH = newchan(S, "mchan", m)
    s1, d1 = call("PATCH", "%s/api/channels/%s" % (N0, ALPHA), {"topic": "from member"}, m["token"])
    s2, d2 = call("PATCH", "%s/api/channels/%s" % (N0, MCH), {"topic": "from owner"}, o["token"])
    s3, _ = call("PATCH", "%s/api/channels/%s" % (N0, ALPHA), {"topic": "from x"}, x["token"])
    s4, _ = call("PATCH", "%s/api/channels/%s" % (N0, ALPHA), {"topic": "z" * 251}, o["token"])
    check(8, "topic update: member 200, owner 200, other member 403, too long 400",
          (s1, dig(d1, "channel.topic"), s2, dig(d2, "channel.topic"), s3, s4) == (200, "from member", 200, "from owner", 403, 400),
          "got %s/%s/%s/%s/%s/%s" % (s1, dig(d1, "channel.topic"), s2, dig(d2, "channel.topic"), s3, s4))
    s1, d1 = call("POST", "%s/api/channels/%s/leave" % (N0, ALPHA), {}, m["token"])
    s2, _ = call("POST", "%s/api/channels/%s/leave" % (N1, ALPHA), {}, m["token"])
    check(9, "leave returns 200 with the channel, twice", s1 == 200 and "channel" in (d1 or {}) and s2 == 200, "got %s and %s" % (s1, s2))
    st, d = call("GET", "%s/api/channels/%s/members" % (N2, ALPHA), None, o["token"])
    mem = (d or {}).get("members", [])
    check(10, "a user who left is no longer listed", st == 200 and m["name"] not in [u.get("username") for u in mem],
          "status %s, members %s" % (st, [u.get("username") for u in mem]))
    ufields = ["id", "username", "display_name", "timezone", "avatar_url", "status_text", "status_emoji"]
    check(11, "members returns full user objects", st == 200 and len(mem) >= 1 and all(all(k in u for k in ufields) for u in mem),
          "status %s, first member %s" % (st, json.dumps(mem[:1])[:200]))
    ARCH = newchan(S, "arch", o)
    a1 = call("POST", "%s/api/channels/%s/archive" % (N0, ARCH), {}, x["token"])[0]
    s, d = call("POST", "%s/api/channels/%s/archive" % (N0, ARCH), {}, o["token"])
    a3 = call("POST", "%s/api/channels/%s/unarchive" % (N0, ARCH), {}, x["token"])[0]
    check(12, "archive/unarchive are owner-only", a1 == 403 and s == 200 and dig(d, "channel.is_archived") is True and a3 == 403,
          "x archive %s, owner archive %s/%s, x unarchive %s" % (a1, s, dig(d, "channel.is_archived"), a3))
    p1 = post(ARCH, o, "while archived")[0]
    u1 = call("POST", "%s/api/channels/%s/unarchive" % (N0, ARCH), {}, o["token"])[0]
    p2 = post(ARCH, o, "after unarchive")[0]
    check(13, "archived channel rejects posts with 423, unarchive restores", (p1, u1, p2) == (423, 200, 201), "got %s/%s/%s" % (p1, u1, p2))
    BAD = 99999999
    r = [call("GET", "%s/api/channels/%s/members" % (N0, BAD), None, o["token"])[0],
         call("POST", "%s/api/channels/%s/archive" % (N0, BAD), {}, o["token"])[0],
         call("PATCH", "%s/api/channels/%s" % (N0, BAD), {"topic": "t"}, o["token"])[0],
         call("GET", "%s/api/channels/%s/members" % (N0, ALPHA))[0]]
    check(14, "unknown channel 404 and no token 401", r == [404, 404, 404, 401], "got %s" % r)

    # ===== B. Messages and paging =====
    st, d = post(ALPHA, o, "hello")
    msg = dig(d, "message") or {}
    mk = ["id", "channel_id", "author_id", "author", "body", "parent_id", "reply_count", "created_at", "edited_at", "files", "reactions", "mentions"]
    ts = re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$", str(msg.get("created_at")))
    check(15, "post returns 201 with a full message", st == 201 and all(k in msg for k in mk) and msg.get("body") == "hello"
          and msg.get("parent_id") is None and msg.get("reply_count") == 0 and msg.get("edited_at") is None
          and msg.get("files") == [] and msg.get("reactions") == [] and msg.get("mentions") == []
          and str(msg.get("author_id")) == str(o["id"]) and ts,
          "status %s, message %s" % (st, json.dumps(msg)[:250]))
    check(16, "empty body returns 400", post(ALPHA, o, "")[0] == 400, "")
    THR = newchan(S, "thr", o)
    st, d = post(THR, o, "parent")
    PARENT = dig(d, "message.id")
    s1, _ = post(THR, o, "r1", parent=PARENT)
    lst = messages(THR, o)[1] or {}
    rc = [mm.get("reply_count") for mm in lst.get("messages", []) if mm.get("id") == PARENT]
    check(17, "reply returns 201 and the parent's reply_count is 1", s1 == 201 and rc == [1], "reply status %s, reply_count %s" % (s1, rc))
    PG = newchan(S, "pg", o)
    for i in range(1, 6):
        post(PG, o, "m%d" % i)
    st, d = messages(PG, o)
    check(18, "list is newest first", st == 200 and bodies((d or {}).get("messages")) == ["m5", "m4", "m3", "m2", "m1"],
          "status %s, bodies %s" % (st, bodies((d or {}).get("messages"))))
    st1, p1 = messages(PG, o, "?limit=2")
    c1 = (p1 or {}).get("next_cursor")
    p2 = messages(PG, o, "?limit=2&cursor=" + q(c1))[1] if c1 else {}
    p3 = messages(PG, o, "?limit=2&cursor=" + q((p2 or {}).get("next_cursor")))[1] if (p2 or {}).get("next_cursor") else {}
    seq = [bodies((p or {}).get("messages")) for p in (p1, p2, p3)]
    check(19, "limit=2 pages through 5 messages and ends with a null cursor",
          seq == [["m5", "m4"], ["m3", "m2"], ["m1"]] and isinstance(c1, str) and c1 and (p3 or {}).get("next_cursor") is None,
          "pages %s, first cursor %r, last cursor %r" % (seq, c1, (p3 or {}).get("next_cursor")))
    st, d = messages(PG, o, "?limit=2", node=N0)
    cur = (d or {}).get("next_cursor")
    st, d2 = messages(PG, o, "?limit=2&cursor=" + q(cur), node=N2) if cur else (0, None)
    check(20, "a cursor from port 8000 works on port 8002", st == 200 and bodies((d2 or {}).get("messages")) == ["m3", "m2"],
          "status %s, bodies %s" % (st, bodies((d2 or {}).get("messages"))))
    st, d = messages(PG, o, "?limit=2", node=N0)
    cur = (d or {}).get("next_cursor")
    post(PG, o, "m6")
    st, d2 = messages(PG, o, "?limit=2&cursor=" + q(cur), node=N1) if cur else (0, None)
    check(21, "a message posted between pages does not shift page 2", st == 200 and bodies((d2 or {}).get("messages")) == ["m3", "m2"],
          "status %s, bodies %s" % (st, bodies((d2 or {}).get("messages"))))
    check(22, "malformed cursor returns 400", messages(PG, o, "?cursor=not-a-real-cursor")[0] == 400, "")
    got = [messages(PG, o, "?limit=%s" % v)[0] for v in ("0", "201", "-1", "abc", "200")]
    check(23, "limit out of range returns 400, limit=200 is accepted", got == [400, 400, 400, 400, 200], "got %s" % got)
    if want(24):  # 55 posts: only worth the time when this rule is being checked
        BIG = newchan(S, "big", o)
        for i in range(55):
            post(BIG, o, "b%d" % i)
        st, d = messages(BIG, o)
        check(24, "default limit is 50", st == 200 and len((d or {}).get("messages", [])) == 50 and (d or {}).get("next_cursor"),
              "status %s, count %s, cursor %r" % (st, len((d or {}).get("messages", [])), (d or {}).get("next_cursor")))
    post(THR, o, "r2", parent=PARENT)
    post(THR, o, "r3", parent=PARENT)
    base = "%s/api/messages/%s/replies" % (N0, PARENT)
    st, d = call("GET", base, None, o["token"])
    b_all = bodies((d or {}).get("replies"))
    st1, d1 = call("GET", base + "?limit=2", None, o["token"])
    cr = (d1 or {}).get("next_cursor")
    d2 = call("GET", base + "?limit=2&cursor=" + q(cr), None, o["token"])[1] if cr else {}
    check(25, "replies are oldest first and page correctly", st == 200 and b_all == ["r1", "r2", "r3"]
          and bodies((d1 or {}).get("replies")) == ["r1", "r2"] and bodies((d2 or {}).get("replies")) == ["r3"] and (d2 or {}).get("next_cursor") is None,
          "all %s, page1 %s, page2 %s" % (b_all, bodies((d1 or {}).get("replies")), bodies((d2 or {}).get("replies"))))
    TOK = "zq" + RUN
    post(ALPHA, o, "first " + TOK)
    time.sleep(0.05)
    post(ALPHA, o, "second " + TOK)
    post(SEC, o, "hidden " + TOK)
    so, do_ = call("GET", "%s/api/search?q=%s" % (N0, TOK), None, o["token"])
    sx, dx = call("GET", "%s/api/search?q=%s" % (N0, TOK), None, x["token"])
    bo, bx = bodies((do_ or {}).get("results")), bodies((dx or {}).get("results"))
    check(26, "search finds matches newest first and hides private channels", so == 200 and sx == 200
          and bo[:3] == ["hidden " + TOK, "second " + TOK, "first " + TOK] and bx == ["second " + TOK, "first " + TOK],
          "owner got %s, other member got %s" % (bo, bx))
    S2 = "c1c-" + RUN
    st, d = mkws(S2, o)
    WS2, GEN2 = dig(d, "workspace.id"), dig(d, "general_channel.id")
    post(GEN2, o, "ws2 " + TOK)
    b1 = bodies((call("GET", "%s/api/search?q=%s&workspace_id=%s" % (N0, TOK, WS1), None, o["token"])[1] or {}).get("results"))
    b2 = bodies((call("GET", "%s/api/search?q=%s&workspace_id=%s" % (N0, TOK, WS2), None, o["token"])[1] or {}).get("results"))
    check(27, "workspace_id limits search to one workspace", ("ws2 " + TOK) not in b1 and b1 and b2 == ["ws2 " + TOK],
          "workspace 1 results %s, workspace 2 results %s" % (b1, b2))
    PIN = newchan(S, "pins", o)
    pm = {}
    for n in ("p1", "p2", "p3"):
        pm[n] = dig(post(PIN, o, n)[1], "message.id")
    s1, d1 = call("POST", "%s/api/messages/%s/pin" % (N0, pm["p1"]), {}, o["token"])
    s2, _ = call("POST", "%s/api/messages/%s/pin" % (N1, pm["p1"]), {}, o["token"])
    lst = (call("GET", "%s/api/channels/%s/pins" % (N0, PIN), None, o["token"])[1] or {}).get("pins", [])
    pin = dig(d1, "pin") or {}
    check(28, "pin returns 200 with a pin object and is idempotent", s1 == 200 and s2 == 200 and all(k in pin for k in ("message_id", "pinned_by", "pinned_at"))
          and len(lst) == 1, "status %s/%s, pin %s, list length %s" % (s1, s2, json.dumps(pin)[:150], len(lst)))
    for n in ("p2", "p3"):
        time.sleep(0.05)
        call("POST", "%s/api/messages/%s/pin" % (N0, pm[n]), {}, o["token"])
    base = "%s/api/channels/%s/pins" % (N0, PIN)
    st, d = call("GET", base, None, o["token"])
    names = [(p.get("message") or {}).get("body") for p in (d or {}).get("pins", [])]
    st1, d1 = call("GET", base + "?limit=2", None, o["token"])
    cp = (d1 or {}).get("next_cursor")
    d2 = call("GET", base + "?limit=2&cursor=" + q(cp), None, o["token"])[1] if cp else {}
    n1 = [(p.get("message") or {}).get("body") for p in (d1 or {}).get("pins", [])]
    n2 = [(p.get("message") or {}).get("body") for p in (d2 or {}).get("pins", [])]
    pfields = all(all(k in p for k in ("message", "pinned_by", "pinned_at")) for p in (d or {}).get("pins", []))
    check(29, "pin list is newest-pinned first, has the right fields and pages", st == 200 and names == ["p3", "p2", "p1"] and pfields
          and n1 == ["p3", "p2"] and n2 == ["p1"] and (d2 or {}).get("next_cursor") is None,
          "list %s, page1 %s, page2 %s" % (names, n1, n2))
    s, d = call("DELETE", "%s/api/messages/%s/pin" % (N0, pm["p1"]), None, o["token"])
    after = [(p.get("message") or {}).get("body") for p in (call("GET", base, None, o["token"])[1] or {}).get("pins", [])]
    check(30, "unpin returns {unpinned: true} and removes the pin", s == 200 and d == {"unpinned": True} and after == ["p3", "p2"],
          "status %s, body %s, list %s" % (s, d, after))
    post(SEC, o, "pinme")
    sx = call("GET", "%s/api/channels/%s/pins" % (N0, SEC), None, x["token"])[0]
    so = call("GET", "%s/api/channels/%s/pins" % (N0, SEC), None, o["token"])[0]
    check(31, "private channel pin list: outsider 403, member 200", (sx, so) == (403, 200), "got %s and %s" % (sx, so))

    # ===== C. WebSocket events =====
    EVT = newchan(S, "evt", o)
    join(EVT, m)

    async def r32():
        try:
            ws = await ws_open(8000, "bogus-token-" + RUN)
        except Exception:
            ok(32, "a bad token is rejected at the handshake")
            return
        try:
            await asyncio.wait_for(ws.recv(), 2)
            bad(32, "a bad token was accepted and a frame was delivered")
        except websockets.exceptions.ConnectionClosed:
            ok(32, "a bad token closes the connection")
        except asyncio.TimeoutError:
            bad(32, "a bad token connection stayed open")

    async def r33_35():
        ws = await ws_open(8000, o["token"])
        try:
            sub = await subscribe(ws, EVT)
            head = (sub or {}).get("head_event_id")
            check(33, "subscribe is answered with subscribed and head_event_id", isinstance(head, int), "got %s" % sub)
            if not isinstance(head, int):
                bad(34, "no head_event_id to compare with")
                bad(35, "no head_event_id to compare with")
                return
            st, d = post(EVT, o, "ws one")
            f = await wait_for(ws, lambda f: f.get("type") == "message.created" and str(f.get("channel_id")) == str(EVT), 3)
            check(34, "message.created with event_id head+1", f is not None and f.get("event_id") == head + 1,
                  "head %s, frame %s" % (head, json.dumps(f)[:200]))
            post(EVT, o, "ws reply", parent=dig(d, "message.id"))
            f2 = await wait_for(ws, lambda f: f.get("type") == "message.reply" and str(f.get("channel_id")) == str(EVT), 3)
            check(35, "message.reply with the next event_id", f2 is not None and f is not None and f2.get("event_id") == f.get("event_id") + 1,
                  "frame %s" % json.dumps(f2)[:200])
        finally:
            await ws.close()

    async def r36():
        ws = await ws_open(8001, o["token"])
        try:
            sub = await subscribe(ws, EVT)
            head = (sub or {}).get("head_event_id")
            if not isinstance(head, int):
                bad(36, "subscribe on 8001 gave no head_event_id")
                return
            for i in range(12):
                post(EVT, o, "dense %d" % i, node=(N0, N1, N2)[i % 3])
            got = await collect(ws, EVT, 12, 4, kinds=("message.created",))
            evs = [f["event_id"] for f in got]
            check(36, "12 posts across 3 nodes give 12 consecutive event_ids", evs == list(range(head + 1, head + 13)),
                  "head %s, event_ids seen %s" % (head, evs))
        finally:
            await ws.close()

    async def r37():
        ws = await ws_open(8002, o["token"])
        try:
            if not await subscribe(ws, EVT):
                bad(37, "subscribe on 8002 got no subscribed frame")
                return
            t = time.time()
            post(EVT, o, "fanout", node=N0)
            f = await wait_for(ws, lambda f: f.get("type") == "message.created", 5)
            check(37, "an event posted via 8000 reaches a subscriber on 8002 within 5s", f is not None, "no frame within 5s")
        finally:
            await ws.close()

    async def r38():
        a = await ws_open(8000, o["token"])
        sub = await subscribe(a, EVT)
        await a.close()
        head = (sub or {}).get("head_event_id")
        if not isinstance(head, int):
            bad(38, "subscribe gave no head_event_id")
            return
        for i in range(3):
            post(EVT, o, "missed %d" % i, node=N1)
        b = await ws_open(8002, o["token"])
        try:
            await b.send(json.dumps({"type": "resume", "channel_id": EVT, "since_event_id": head}))
            seen = []
            end = await wait_for(b, lambda f: f.get("type") == "resumed", 4, seen)
            evs = [f.get("event_id") for f in seen if "event_id" in f and f.get("type") != "resumed"]
            check(38, "resume on another node replays the missed events then resumed",
                  end is not None and evs == [head + 1, head + 2, head + 3] and end.get("head_event_id") == head + 3,
                  "head %s, replayed %s, resumed %s" % (head, evs, json.dumps(end)[:150]))
        finally:
            await b.close()

    async def r39():
        KC = newchan(S, "kinds", o)
        ws = await ws_open(8000, o["token"])
        try:
            sub = await subscribe(ws, KC)
            head = (sub or {}).get("head_event_id")
            if not isinstance(head, int):
                bad(39, "subscribe gave no head_event_id")
                return
            join(KC, m)
            call("PATCH", "%s/api/channels/%s" % (N0, KC), {"topic": "kinds"}, o["token"])
            call("POST", "%s/api/channels/%s/leave" % (N0, KC), {}, m["token"])
            call("POST", "%s/api/channels/%s/archive" % (N0, KC), {}, o["token"])
            call("POST", "%s/api/channels/%s/unarchive" % (N0, KC), {}, o["token"])
            got = await collect(ws, KC, 5, 4)
            kinds = [f.get("type") for f in got]
            evs = [f.get("event_id") for f in got]
            want = ["member.joined", "channel.updated", "member.left", "channel.archived", "channel.unarchived"]
            check(39, "join, topic, leave, archive, unarchive give the five durable kinds with consecutive ids",
                  kinds == want and evs == list(range(head + 1, head + 6)), "head %s, kinds %s, ids %s" % (head, kinds, evs))
        finally:
            await ws.close()

    async def r61():
        LV = newchan(S, "leave2", o)
        join(LV, m)
        ws = await ws_open(8000, o["token"])
        try:
            sub = await subscribe(ws, LV)
            head = (sub or {}).get("head_event_id")
            if not isinstance(head, int):
                bad(61, "subscribe gave no head_event_id")
                return
            call("POST", "%s/api/channels/%s/leave" % (N0, LV), {}, m["token"])
            call("POST", "%s/api/channels/%s/leave" % (N1, LV), {}, m["token"])
            got = await collect(ws, LV, 3, 1.5)
            kinds, evs = [f.get("type") for f in got], [f.get("event_id") for f in got]
            check(61, "leaving twice emits exactly one member.left", kinds == ["member.left"] and evs == [head + 1],
                  "head %s, kinds %s, ids %s" % (head, kinds, evs))
        finally:
            await ws.close()

    async def r62():
        CC = newchan(S, "conc", o)
        ws = await ws_open(8000, o["token"])
        try:
            sub = await subscribe(ws, CC)
            head = (sub or {}).get("head_event_id")
            if not isinstance(head, int):
                bad(62, "subscribe gave no head_event_id")
                return
            nodes = (N0, N1, N2)

            def burst():
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(10) as ex:
                    return list(ex.map(lambda i: post(CC, o, "c%d" % i, node=nodes[i % 3])[0], range(30)))

            statuses = await asyncio.get_running_loop().run_in_executor(None, burst)
            got = await collect(ws, CC, 30, 4, kinds=("message.created",))
            evs = sorted(f["event_id"] for f in got)
            check(62, "30 concurrent posts across 3 nodes all succeed with 30 consecutive event_ids",
                  statuses.count(201) == 30 and evs == list(range(head + 1, head + 31)),
                  "statuses %s, head %s, ids %s" % (sorted(set(statuses)), head, evs[:6]))
        finally:
            await ws.close()

    async def ws_rules():
        await guarded([32], r32)
        await guarded([33, 34, 35], r33_35)
        await guarded([36], r36)
        await guarded([37], r37)
        await guarded([38], r38)
        await guarded([39], r39)
        await guarded([61], r61)
        await guarded([62], r62, limit=12)

    asyncio.run(ws_rules())

    # ===== D. Mentions and read state =====
    MEN = newchan(S, "ment", o)
    join(MEN, m)
    join(MEN, x)
    def mentions(text, who=o):
        st, d = post(MEN, who, text)
        return st, [str(i) for i in (dig(d, "message.mentions") or [])]
    st, got = mentions("hi @%s" % m["name"])
    check(40, "@username fills mentions with the user's id", st == 201 and got == [str(m["id"])], "status %s, mentions %s" % (st, got))
    (s1, g1), (s2, g2) = mentions("hello@%s there" % m["name"]), mentions("write to bob@%s.com" % m["name"])
    check(41, "in-word and email-shaped mentions do not match", (s1, s2) == (201, 201) and g1 == [] and g2 == [],
          "statuses %s and %s, mentions %s and %s" % (s1, s2, g1, g2))
    st, got = mentions("note to self @%s" % o["name"])
    check(42, "a self-mention is dropped", st == 201 and got == [], "status %s, mentions %s" % (st, got))
    st, got = mentions("@%s @%s and @%s" % ((m["name"],) * 3))
    check(43, "mentions are deduplicated", st == 201 and got == [str(m["id"])], "status %s, mentions %s" % (st, got))
    st, got = mentions("hey @%s" % y["name"])
    check(44, "a user outside the workspace is excluded", st == 201 and got == [], "status %s, mentions %s" % (st, got))
    st, got = mentions("@channel heads up")
    expected = sorted([str(m["id"]), str(x["id"])])
    check(45, "@channel mentions every channel member except the author", st == 201 and sorted(got) == expected,
          "status %s, mentions %s, expected %s" % (st, sorted(got), expected))

    RS = newchan(S, "rs", o)
    join(RS, m)
    st, d = read_state(RS, m)
    rs = dig(d, "read_state") or {}
    check(46, "read state has channel_id, last_read_event_id, unread_count, mention_count",
          st == 200 and all(k in rs for k in ("channel_id", "last_read_event_id", "unread_count", "mention_count")) and str(rs.get("channel_id")) == str(RS),
          "status %s, read_state %s" % (st, json.dumps(rs)[:200]))
    u0 = rs.get("unread_count") if isinstance(rs.get("unread_count"), int) else 0
    for i in range(3):
        post(RS, o, "unread %d" % i)
    u1 = (dig(read_state(RS, m, node=N2)[1], "read_state") or {}).get("unread_count")
    check(47, "unread_count rises by 3 after 3 posts", u1 == u0 + 3, "before %s, after %s" % (u0, u1))
    m0 = (dig(read_state(RS, m)[1], "read_state") or {}).get("mention_count")
    o0 = (dig(read_state(RS, o)[1], "read_state") or {}).get("mention_count")
    post(RS, o, "@%s ping" % m["name"])
    post(RS, o, "@%s me" % o["name"])
    m1 = (dig(read_state(RS, m)[1], "read_state") or {}).get("mention_count")
    o1 = (dig(read_state(RS, o)[1], "read_state") or {}).get("mention_count")
    check(48, "mention_count rises for the mentioned user and not for a self-mention",
          isinstance(m0, int) and m1 == m0 + 1 and o1 == o0, "mentioned user %s -> %s, self-mentioner %s -> %s" % (m0, m1, o0, o1))
    head = head_of(RS, m["token"])
    if isinstance(head, int) and head >= 3:
        st, d = call("POST", "%s/api/channels/%s/read" % (N0, RS), {"last_read_event_id": head}, m["token"])
        now = (dig(read_state(RS, m, node=N1)[1], "read_state") or {}).get("last_read_event_id")
        check(49, "marking read returns the updated read_state and persists", st == 200 and dig(d, "read_state.last_read_event_id") == head and now == head,
              "status %s, posted %s, returned %s, later %s" % (st, head, dig(d, "read_state.last_read_event_id"), now))
        st, d = call("POST", "%s/api/channels/%s/read" % (N0, RS), {"last_read_event_id": head - 2}, m["token"])
        check(50, "the read cursor never moves backwards", st == 200 and dig(d, "read_state.last_read_event_id") == head,
              "status %s, posted %s then %s, got %s" % (st, head, head - 2, dig(d, "read_state.last_read_event_id")))
    else:
        bad(49, "setup: could not read head_event_id over WebSocket (rules 33 to 35 first)")
        bad(50, "setup: could not read head_event_id over WebSocket (rules 33 to 35 first)")
    st, d = call("GET", "%s/api/workspaces/%s" % (N0, S), None, m["token"])
    emb = json.dumps((d or {}).get("read_state"))
    check(51, "workspace detail embeds read_state with the same fields", st == 200 and all(k in emb for k in ("last_read_event_id", "unread_count", "mention_count")),
          "status %s, read_state %s" % (st, emb[:200]))

    # ===== E. Stricter edge cases =====
    base_r = "%s/api/messages/%s/replies" % (N0, PARENT)
    base_p = "%s/api/channels/%s/pins" % (N0, PIN)
    base_s = "%s/api/search?q=%s" % (N0, TOK)
    bads = []
    for name, url in (("replies", base_r + "?"), ("pins", base_p + "?"), ("search", base_s + "&")):
        for v in ("0", "201"):
            s = call("GET", url + "limit=" + v, None, o["token"])[0]
            if s != 400:
                bads.append("%s limit=%s->%s" % (name, v, s))
    check(52, "limit 0 and 201 return 400 on replies, pins and search", not bads, "; ".join(bads))
    bads = []
    for name, url in (("replies", base_r + "?"), ("pins", base_p + "?"), ("search", base_s + "&")):
        s = call("GET", url + "cursor=%21%21%21", None, o["token"])[0]
        if s != 400:
            bads.append("%s->%s" % (name, s))
    check(53, "a malformed cursor returns 400 on replies, pins and search", not bads, "; ".join(bads))
    SP = newchan(S, "sp", o)
    TOK2 = "sp" + RUN
    for i in range(1, 6):
        post(SP, o, "s%d %s" % (i, TOK2))
    url = "%s/api/search?q=%s&limit=2" % (N0, TOK2)
    s1, d1 = call("GET", url, None, o["token"])
    c1 = (d1 or {}).get("next_cursor")
    d2 = call("GET", url + "&cursor=" + q(c1), None, o["token"])[1] if c1 else {}
    c2 = (d2 or {}).get("next_cursor")
    d3 = call("GET", url + "&cursor=" + q(c2), None, o["token"])[1] if c2 else {}
    seq = [[b.split(" ")[0] for b in bodies((d or {}).get("results"))] for d in (d1, d2, d3)]
    check(54, "search pages with limit and cursor and ends with a null cursor",
          seq == [["s5", "s4"], ["s3", "s2"], ["s1"]] and (d3 or {}).get("next_cursor") is None, "pages %s" % seq)
    sx = call("GET", base_p, None, x["token"])
    check(55, "a workspace member who has not joined a public channel can read its pin list",
          sx[0] == 200 and isinstance((sx[1] or {}).get("pins"), list), "status %s" % sx[0])
    M3 = newchan(S, "men3", o)
    st, d = post(M3, o, "psst @%s" % x["name"])
    got = [str(i) for i in (dig(d, "message.mentions") or [])]
    check(56, "a workspace member who is not in the channel is still mentioned", st == 201 and got == [str(x["id"])],
          "status %s, mentions %s" % (st, got))
    M2 = newchan(S, "men2", o)
    join(M2, m)
    st, d = post(M2, o, "@channel notice")
    mid = dig(d, "message.id")
    join(M2, x)
    lst = (messages(M2, o)[1] or {}).get("messages", [])
    later = [[str(i) for i in mm.get("mentions", [])] for mm in lst if mm.get("id") == mid]
    check(57, "mentions are fixed when the message is posted", st == 201 and later == [[str(m["id"])]],
          "status %s, mentions when listed later %s" % (st, later))
    ARCH2 = newchan(S, "arch2", o)
    st, d = post(ARCH2, o, "parent")
    pid = dig(d, "message.id")
    call("POST", "%s/api/channels/%s/archive" % (N0, ARCH2), {}, o["token"])
    r1 = post(ARCH2, o, "reply", parent=pid)[0]
    r2 = call("PATCH", "%s/api/channels/%s" % (N0, ARCH2), {"topic": "new"}, o["token"])[0]
    r3 = call("POST", "%s/api/messages/%s/pin" % (N0, pid), {}, o["token"])[0]
    check(63, "reply, topic change and pin in an archived channel return 423", [r1, r2, r3] == [423, 423, 423],
          "reply %s, topic %s, pin %s" % (r1, r2, r3))
    w1 = post(PG, o, "   ")[0]
    w2 = call("POST", "%s/api/channels/%s/messages" % (N0, PG), {}, o["token"])[0]
    check(64, "a whitespace-only body and a missing body return 400", (w1, w2) == (400, 400), "got %s and %s" % (w1, w2))
    p1 = post(PG, o, "orphan", parent=99999999)[0]
    p2 = post(PG, o, "cross", parent=PARENT)[0]
    check(65, "a reply to an unknown parent or to a parent in another channel is rejected (400 or 404)",
          p1 in (400, 404) and p2 in (400, 404), "unknown parent %s, parent in other channel %s" % (p1, p2))
    st, d = call("POST", "%s/api/workspaces/%s/channels" % (N0, S), {"name": "desc-chan", "description": "hello"}, o["token"])
    check(66, "a description in the create body does not fill topic", st == 201 and dig(d, "channel.topic") == "",
          "status %s, topic %r" % (st, dig(d, "channel.topic")))
    RS2 = newchan(S, "rs2", o)
    join(RS2, m)
    own0 = (dig(read_state(RS2, o)[1], "read_state") or {}).get("unread_count")
    post(RS2, o, "my own message")
    own1 = (dig(read_state(RS2, o)[1], "read_state") or {}).get("unread_count")
    check(58, "the author's own message does not raise their unread_count", isinstance(own0, int) and own0 == own1,
          "before %s, after %s" % (own0, own1))
    h2 = head_of(RS2, m["token"])
    if isinstance(h2, int):
        call("POST", "%s/api/channels/%s/read" % (N0, RS2), {"last_read_event_id": h2}, m["token"])
        z0 = (dig(read_state(RS2, m)[1], "read_state") or {}).get("unread_count")
        post(RS2, o, "after read")
        z1 = (dig(read_state(RS2, m)[1], "read_state") or {}).get("unread_count")
        check(59, "marking read to the head makes unread_count 0, and one new message makes it 1", (z0, z1) == (0, 1),
              "after marking read %s, after one new message %s" % (z0, z1))
    else:
        bad(59, "setup: could not read head_event_id over WebSocket")
    emb = json.dumps((call("GET", "%s/api/workspaces/%s" % (N0, S), None, m["token"])[1] or {}).get("read_state"))
    check(60, "workspace detail read_state carries real counts, not zeros", re.search(r'"unread_count": *[1-9]', emb) is not None,
          "read_state %s" % emb[:200])

    print("RESULT: %d passed, %d failed" % (passed, failed))
    sys.exit(0 if failed == 0 else 1)


main()
__REGRESSION_CHANNELS_RULES__
cat >"$OUT/baseline.sh" <<'__REGRESSION_BASELINE_RULES__'
#!/usr/bin/env bash
# Runs rules 71-76 of the roles check (see roles-4-baseline.md): the four cluster baseline conditions.
# It kills the HTTP node on port 8001 (by the PID that owns the port) and waits for it to come back.
set -u
HERE="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "$HERE/lib.sh"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_roles_4_body.json
PW="correct-horse-1"
RUN="$(printf '%x%x' "$(date +%s)" "$RANDOM")"
PY=/opt/test-venv/bin/python; [ -x "$PY" ] || PY=python3
T0=$SECONDS
pass=0; fail=0
ok()  { pass=$((pass+1)); return 0; }
bad() { say "FAIL $1 $2"; fail=$((fail+1)); return 0; }
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; exit $?; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 3 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 3s
}
# health <url>  -> prints "<status>/<status-field>/<node_id>" ("000//" when there is no answer)
health() {
  local s; : > "$BODY"; s=$(call GET "$1/api/health")
  printf '%s/%s' "$s" "$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); n=d.get("node_id")
    print("%s/%s" % (d.get("status"), n if isinstance(n,int) and not isinstance(n,bool) else repr(n)))
except Exception: print("/")' "$BODY")"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}

# AIDEV-NOTE: boot wait is capped at 12s so boot + the 15s respawn wait still fit the 30s command limit.
if ! healthy; then
  # AIDEV-NOTE: redirect the whole subshell, not just start.sh: otherwise the subshell keeps this
  # check's stdout open for as long as the cluster runs, and a caller reading that output hangs.
  (cd /app && setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &) </dev/null >/dev/null 2>&1
  for _ in $(seq 1 12); do healthy && break; sleep 1; done
fi
healthy || { say "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

# AIDEV-NOTE: BASELINE_KILL_ONLY=1 (used by the regression suite) runs only rules 75-76;
# BASELINE_NO_KILL=1 runs only rules 71-74.
if [ "${BASELINE_KILL_ONLY:-}" != 1 ]; then
# ===== 71-72: health and node ids =====
h0=$(health $N0)
if [ "$h0" = "200/ok/0" ]; then ok 71 "health on 8000 returns 200 with status ok and node_id 0"
else bad 71 "health on 8000: expected 200/ok/0 (status/status field/node_id), got $h0"; fi
got=""; for u in $N0 $N1 $N2; do got="$got $(health "$u")"; done
if [ "$got" = " 200/ok/0 200/ok/1 200/ok/2" ]; then ok 72 "ports 8000, 8001, 8002 report node_id 0, 1, 2"
else bad 72 "node ids: expected 200/ok/0 200/ok/1 200/ok/2, got$got"; fi

# ===== 73-74: WebSocket token check =====
call POST $N0/api/auth/register "{\"username\":\"bl$RUN\",\"password\":\"$PW\"}" >/dev/null
TOKEN=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("token") or "")
except Exception: pass' "$BODY")
ws=$(timeout 8 "$PY" - "$TOKEN" <<'PY' 2>/dev/null
import asyncio, sys
try:
    import websockets
except ImportError:
    print("nows\nnows"); sys.exit()
async def probe(token):
    # every stage has its own time limit, so a server that never answers is named, not just "error"
    try:
        ws = await asyncio.wait_for(websockets.connect("ws://127.0.0.1:8000/api/ws?token=" + token,
                                                       open_timeout=3, close_timeout=1), 3.5)
    except asyncio.TimeoutError:
        return "handshake-timeout"
    except Exception:
        return "refused"
    try:
        await asyncio.wait_for(ws.recv(), 1.5)
        r = "frame"          # something was sent: the connection is live
    except asyncio.TimeoutError:
        r = "open"           # handshake accepted and still open
    except Exception:
        r = "closed"
    try:
        await asyncio.wait_for(ws.close(), 1.2)
    except Exception:
        r += "+close-hung"
    return r
async def main():
    print(await probe("bogus-token-not-real-0000"), flush=True)
    print(await probe(sys.argv[1]) if sys.argv[1] else "notoken", flush=True)
asyncio.run(main())
PY
)
{ read -r wbad; read -r wgood; } <<<"$ws"
HUNG="the server did not answer the client's WebSocket close frame (opcode 8): reply with a close frame and end the connection"
case "${wbad:-}" in
  refused|closed|closed+close-hung) ok 73 "a WebSocket with a bad token is rejected" ;;
  nows) bad 73 "the python websockets package is not available to the check" ;;
  handshake-timeout) bad 73 "a WebSocket with a bad token: expected it refused (401) or closed, got no handshake answer within 3.5 s" ;;
  open*|frame*) bad 73 "a WebSocket with a bad token: expected it refused (401) or closed, got it accepted and open" ;;
  *) bad 73 "a WebSocket with a bad token: the probe did not finish within 8 s (the server held the connection open without answering)" ;;
esac
case "${wgood:-}" in
  open|frame) ok 74 "a WebSocket with a valid token opens and stays open" ;;
  open+close-hung|frame+close-hung) ok 74 "a WebSocket with a valid token opens and stays open"
    say "NOTE 74 passed, but $HUNG" ;;
  notoken) bad 74 "setup: could not register a user to get a token (Roles.1 rule 1 first)" ;;
  nows) bad 74 "the python websockets package is not available to the check" ;;
  handshake-timeout) bad 74 "a WebSocket with a valid token (?token=): expected it open, got no handshake answer within 3.5 s" ;;
  refused) bad 74 "a WebSocket with a valid token (?token=): expected it open, got the handshake refused (is the token read from the ?token= query?)" ;;
  closed*) bad 74 "a WebSocket with a valid token: expected it to stay open, but the server closed it within 1.5 s" ;;
  *) bad 74 "a WebSocket with a valid token: the probe did not finish within 8 s (${wbad:+bad-token probe: $wbad; }the server held a connection without answering)" ;;
esac

fi
[ "${BASELINE_NO_KILL:-}" = 1 ] && finish

# ===== 75-76: kill node 1 by the PID that owns port 8001 =====
# AIDEV-NOTE: never kill by name; only PIDs that own the listening socket on :8001, and never
# this script, its parent, or PID 1.
pids=$(ss -ltnpH 'sport = :8001' 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
safe=""
for p in $pids; do
  [ "$p" = $$ ] || [ "$p" = "$PPID" ] || [ "$p" = 1 ] || safe="$safe $p"
done
if [ -z "$safe" ]; then
  bad 75 "could not find the PID that owns port 8001 with ss -ltnp"
  bad 76 "not checked: node 1 was not killed"
  finish
fi
kill -9 $safe 2>/dev/null
sleep 1
ok0=no; ok2=no; a=""; c=""
for _ in 1 2 3; do
  a=$(health $N0); c=$(health $N2)
  [ "${a%/*}" = "200/ok" ] && ok0=yes; [ "${c%/*}" = "200/ok" ] && ok2=yes
  [ $ok0 = yes ] && [ $ok2 = yes ] && break
  sleep 0.5
done
if [ $ok0 = yes ] && [ $ok2 = yes ]; then ok 75 "after killing the node on 8001, 8000 and 8002 still answer health with status ok"
else bad 75 "after SIGKILL of PID(s)$safe on 8001, expected 8000 and 8002 to answer 200/ok, got 8000 $a and 8002 $c (each node must be its own process)"; fi

limit=${BASELINE_RESPAWN_LIMIT:-$((T0 + 27 - SECONDS))}; [ "$limit" -gt 15 ] && limit=15
back=""; t=0
while [ "$t" -lt $((limit * 2)) ]; do
  b=$(health $N1)
  if [ "$b" = "200/ok/1" ]; then back=$((t / 2)); break; fi
  sleep 0.5; t=$((t + 1))
done
if [ -n "$back" ]; then ok 76 "node 1 came back by itself within ${back}s"
else
  bad 76 "node 1 did not come back on 8001 within ${limit}s (last health: $b); /app/start.sh must restart a killed node"
  say "NOTE: to restore the cluster, stop the PIDs on 8000 and 8002 (ss -ltnp) and start /app/start.sh again"
fi
finish
__REGRESSION_BASELINE_RULES__

healthy() {
  for p in 8000 8001 8002; do curl -fs --max-time 2 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done
}
# AIDEV-NOTE: start the cluster once here, before the parts run in parallel; otherwise each part would
# find it down and start its own /app/start.sh. The whole subshell is redirected so a caller reading
# this script's output is not held open by the cluster.
if ! healthy; then
  (cd /app && setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &) </dev/null >/dev/null 2>&1
  for _ in 1 2 3 4 5 6; do healthy && break; sleep 1; done
fi
if ! healthy; then
  say "REGRESSION FAIL cluster 0 not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"
  echo "REGRESSION: 1 broken"
  exit 1
fi

bash "$OUT/roles.sh" >"$OUT/roles.out" 2>&1 &
"$PY" "$OUT/channels.py" >"$OUT/channels.out" 2>&1 &
BASELINE_NO_KILL=1 bash "$OUT/baseline.sh" >"$OUT/baseline.out" 2>&1 &
wait

# AIDEV-NOTE: rules 75-76 kill node 1 and wait for it; they run only after everything else, and only
# when at least 8 seconds of the 30 second limit remain (they need about 2-5 s on a healthy cluster).
left=$((27 - (SECONDS - T0)))
if [ "$left" -ge 8 ]; then
  BASELINE_KILL_ONLY=1 BASELINE_RESPAWN_LIMIT=$((left - 3)) bash "$OUT/baseline.sh" >"$OUT/kill.out" 2>&1
else
  echo "NOTE: rules 75-76 (a killed node) were not run this time: the other rules used most of the time limit; run the suite again" >"$OUT/kill.out"
  echo "RESULT: 0 passed, 0 failed" >>"$OUT/kill.out"
fi

broken=0
# report <component> <file>: turns FAIL lines into REGRESSION FAIL lines; a part with no RESULT line
# (it crashed or was cut off) counts as broken too.
report() {
  local comp="$1" f="$2" line
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) broken=$((broken + 1)); say "REGRESSION FAIL $comp ${line#FAIL }" ;;
      NOTE:*) say "REGRESSION NOTE $comp ${line#NOTE: }" ;;
    esac
  done <"$f"
  if ! grep -q '^RESULT: ' "$f"; then
    broken=$((broken + 1))
    say "REGRESSION FAIL $comp 0 its part of the suite did not finish (last line: $(tail -1 "$f" | cut -c1-150))"
  fi
}
report roles "$OUT/roles.out"
report channels "$OUT/channels.out"
report roles "$OUT/baseline.out"
report roles "$OUT/kill.out"

echo "REGRESSION: $broken broken"
[ "$broken" -eq 0 ]
