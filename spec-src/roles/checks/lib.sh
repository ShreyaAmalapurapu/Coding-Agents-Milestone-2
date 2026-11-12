#!/usr/bin/env bash
# Shared helpers for the roles checks. Sourced by check_roles_core.sh and check_roles_4.sh, not run directly.

# Testing mode: verbose prints a FAIL line with the reason for each failing rule, score_only prints only the RESULT line.
FEEDBACK=verbose   # verbose or score_only

# say <text...>  -> prints the text only in verbose mode (every PASS, FAIL and NOTE line goes through here)
say() { [ "$FEEDBACK" = verbose ] && echo "$*"; return 0; }
