#!/usr/bin/env bash
set -euo pipefail

USER_NAME="${1:-${SUDO_USER:-pi}}"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "需要管理员权限安装 Pin33 PWM 开机授权服务。" >&2
  exit 1
fi
if [[ ! "$USER_NAME" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "用户名无效：$USER_NAME" >&2
  exit 1
fi
id "$USER_NAME" >/dev/null
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$ROOT/prepare_servo_pwm.sh" /usr/local/libexec/doorlock-pwm-access
cat > /etc/systemd/system/doorlock-pwm-access.service <<EOF
[Unit]
Description=Prepare Quectel M2 Pin33 PWM access for doorlock
After=systemd-udev-trigger.service
Before=lockd.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/doorlock-pwm-access $USER_NAME
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now doorlock-pwm-access.service
