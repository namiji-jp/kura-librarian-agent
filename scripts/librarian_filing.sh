#!/usr/bin/env bash
# Local scaffold: the live scheduler and decision provider are not bundled or enabled.
exec python3 "$(dirname "$0")/kura_scan.py" loose 2
