#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ "$(uname -m)" == "aarch64" ]] || fail "需要 ARM64 Quectel Pi M2 系统。"
command -v apt-get >/dev/null || fail "需要使用 apt 的 Debian 系统。"
command -v sudo >/dev/null || fail "需要安装 sudo 并使用普通用户运行。"
[[ "$(id -u)" -ne 0 ]] || fail "请使用普通用户运行，不要使用 sudo 运行整个脚本。"

PERSON_MODEL="models/yolov5s-rk3576.rknn"
PARCEL_MODEL="models/parcel-yolov5n-rk3576-fp.rknn"
SYSTEM_MODEL="/usr/share/model/yolov5/RK3576/yolov5s-640-640.rknn"
if [[ ! -s "$PERSON_MODEL" && ! -s "$SYSTEM_MODEL" ]]; then
  fail "缺少人员模型。请将 RK3576 模型放到 $ROOT/$PERSON_MODEL 后重试。"
fi

echo "[1/4] 安装系统依赖..."
sudo apt-get update
sudo apt-get install -y curl ca-certificates python3 python3-dev build-essential libportaudio2 libgl1 \
  libopenblas-dev gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

if ! command -v uv >/dev/null 2>&1; then
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  curl -fsSL https://astral.sh/uv/install.sh -o "$installer"
  sh "$installer"
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || fail "找不到 uv，请检查安装输出及 PATH。"

echo "[2/4] 配置 Python 环境..."
if [[ ( -e .venv || -L .venv ) && ! -x .venv/bin/python ]]; then
  fail ".venv 已存在但不是可用的 Python 环境，请检查后再重试。"
fi
if [[ ! -e .venv ]]; then
  uv venv --python 3.11 .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 11), "后端环境需要 Python 3.11"'

if [[ ( -e .venv-ui || -L .venv-ui ) && ! -x .venv-ui/bin/python ]]; then
  fail ".venv-ui 已存在但不是可用的 Python 环境，请检查后再重试。"
fi
if [[ ! -e .venv-ui ]]; then
  /usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 13), "界面环境需要系统 Python 3.13"'
  uv venv --python /usr/bin/python3 .venv-ui
fi
.venv-ui/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 13), "界面环境需要 Python 3.13"'

echo "[3/4] 安装项目依赖和 RKNN Lite..."
uv pip install --python .venv/bin/python -e .
uv pip install --python .venv-ui/bin/python -r requirements.txt
uv pip install --python .venv/bin/python \
  https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknn-toolkit-lite2/packages/rknn_toolkit_lite2-2.3.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
.venv/bin/python -c 'import aiohttp, cv2, numpy, rknnlite.api, serial, sounddevice'
.venv-ui/bin/python -c 'from PySide6.QtWidgets import QApplication'

echo "[4/4] 检查模型..."
mkdir -p models
if [[ ! -s "$PERSON_MODEL" ]]; then
  cp "$SYSTEM_MODEL" "$PERSON_MODEL"
fi
[[ -s "$PERSON_MODEL" ]] || fail "人员模型准备失败：$PERSON_MODEL"

echo "Deployment complete."
if [[ -s "$PARCEL_MODEL" ]]; then
  echo "启动：LOCK_DRIVER=mock .venv/bin/python main.py"
else
  echo "注意：缺少 $PARCEL_MODEL，完整包裹检测不可用。"
  echo '单模型启动：VISION_PACKAGE_MODEL="" LOCK_DRIVER=mock .venv/bin/python main.py'
fi
