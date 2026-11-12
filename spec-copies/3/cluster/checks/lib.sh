#!/usr/bin/env bash
# Shared settings for the cluster (component D) checks. Sourced by check_cluster_*.sh;
# check_cluster_core.py reads the FEEDBACK line below from this file. Not run directly.

# Testing mode: verbose prints a FAIL line with the reason for each failing rule, score_only prints only the RESULT line.
FEEDBACK=verbose   # verbose or score_only
