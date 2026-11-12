#!/usr/bin/env bash
# 由管理员安装的开机服务执行；只授权 Pin33 对应的 PWM 通道，不输出波形。
set -euo pipefail

USER_NAME="${1:?缺少用户名参数}"
id "$USER_NAME" >/dev/null
CHIP=""
for ((attempt=0; attempt<100; attempt++)); do
  for candidate in /sys/class/pwm/pwmchip*; do
    if [[ "$(readlink -f "$candidate/device")" == */2ade3000.pwm ]]; then
      CHIP="$candidate"
      break
    fi
  done
  [[ -n "$CHIP" ]] && break
  sleep 0.1
done
if [[ -z "$CHIP" ]]; then
  echo "未找到 Pin33 控制器 2ade3000.pwm；请检查内核和引脚复用。" >&2
  exit 1
fi

CHANNEL="$CHIP/pwm0"
if [[ ! -d "$CHANNEL" ]]; then
  echo 0 > "$CHIP/export"
fi
for ((attempt=0; attempt<100; attempt++)); do
  [[ -e "$CHANNEL/polarity" ]] && break
  sleep 0.01
done
for attribute in period duty_cycle polarity enable; do
  chown "$USER_NAME:$(id -gn "$USER_NAME")" "$CHANNEL/$attribute"
  chmod 0600 "$CHANNEL/$attribute"
done
echo "已授权 $USER_NAME 使用 $CHANNEL（物理 Pin33），未改变舵机位置。"
