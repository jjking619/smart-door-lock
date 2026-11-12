#!/usr/bin/env bash
# 让普通用户在不加 sudo 的情况下访问 GPIO（lockd 的 gpio 驱动需要）。
#
# 为什么需要：/dev/gpiochip* 默认属 root:root 0600，普通用户直接 gpioset 会
# Permission denied。指纹串口走的是 dialout 组，GPIO 没有对应的组，所以要自己加。
#
# 用法：sudo bash deploy/install_gpio_access.sh [用户名]
set -euo pipefail

USER_NAME="${1:-${SUDO_USER:?请指定用户名：sudo bash deploy/install_gpio_access.sh <用户名>}}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行：sudo bash deploy/install_gpio_access.sh ${USER_NAME}" >&2
  exit 1
fi

getent group gpio >/dev/null || groupadd --system gpio
usermod -aG gpio "${USER_NAME}"

cat >/etc/udev/rules.d/60-gpio-doorlock.rules <<'RULE'
# 门锁锁体继电器：允许 gpio 组访问 GPIO 字符设备
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
RULE

udevadm control --reload-rules
udevadm trigger --subsystem-match=gpio

echo "已授权 ${USER_NAME} 访问 GPIO。请重新登录（或重启）后生效。"
echo "验证：gpioinfo -c gpiochip0 | head -3"
