#!/usr/bin/env bash
# Shared settings for the irc-ui (component E) checks. Sourced by check_irc_ui_*.sh;
# check_irc_ui_core.py reads the FEEDBACK line below from this file. Not run directly.

# Testing mode: verbose prints a FAIL line with the reason for each failing rule, score_only prints only the RESULT line.
FEEDBACK=score_only   # verbose or score_only
