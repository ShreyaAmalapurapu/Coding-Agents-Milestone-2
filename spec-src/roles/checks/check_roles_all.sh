#!/usr/bin/env bash
# Rules 1-70 of the roles check, one total. Rules 71-76 (the baseline, which kills a node) are in check_roles_4.sh.
exec bash "$(dirname "${BASH_SOURCE[0]}")/check_roles_core.sh"
