#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export LOCK_DRIVER=servo
export LOCK_PULSE_S="${LOCK_PULSE_S:-5}"
export LOCK_SERVO_LOCKED_ANGLE="${LOCK_SERVO_LOCKED_ANGLE:-0}"
export LOCK_SERVO_UNLOCKED_ANGLE="${LOCK_SERVO_UNLOCKED_ANGLE:-90}"
export LOCK_SERVO_MIN_US="${LOCK_SERVO_MIN_US:-500}"
export LOCK_SERVO_MAX_US="${LOCK_SERVO_MAX_US:-2500}"
export LOCK_SERVO_MOVE_S="${LOCK_SERVO_MOVE_S:-0.5}"
exec "$ROOT/.venv/bin/python" "$ROOT/main.py" "$@"
