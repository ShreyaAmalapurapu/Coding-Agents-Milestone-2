#!/usr/bin/env bash
# Shared settings for the features (component C) checks. Sourced by check_features_*.sh;
# check_features_core.py reads the FEEDBACK line below from this file. Not run directly.

# Testing mode: verbose prints a FAIL line with the reason for each failing rule, score_only prints only the RESULT line.
FEEDBACK=verbose   # verbose or score_only
