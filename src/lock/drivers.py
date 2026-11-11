"""锁体驱动：只负责"通电/断电"，不做任何决策。

按现场硬件选：

* `MockDriver`   —— 没有锁体时用，只记日志，界面和策略逻辑可以完整跑通；
* `GpioRelayDriver` —— Pi 直驱继电器（方案 A）。用 libgpiod 的 `gpioset` 命令行，
  不引入额外的 Python 依赖；
* `McuDriver`    —— 接 ESP32-S3 执行单元（方案 B）：发 HMAC 签名帧，由 MCU 驱动电机。
* `PwmServoDriver` —— M2 物理 Pin33 的硬件 PWM，驱动 SG90 开锁并自动复位。

**GPIO 的失效方向问题**：继电器失电或线路被拔掉时，锁必须回到"上锁"。
所以驱动只把"通电"当成开锁动作，任何异常路径都会先断电再抛错；同时建议硬件上
串一个 555 单稳（最长通电时间 ≈ 开锁时长），这样即使 Pi 死机也不会一直抱着锁不放。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
import subprocess
import time
from typing import Protocol

from . import protocol

logger = logging.getLogger("lockd")

#: `gpioset` 在进程退出时会释放线路，之后电平由内核/外部电路决定。
#: 因此断电后要再显式拉一次"未通电"电平，把窗口缩到微秒级。
INACTIVE_HOLD_MS = 300


class LockDriverError(RuntimeError):
    pass


class LockDriver(Protocol):
    name: str

    def unlock(self, seconds: float) -> None:
        """开锁并在 `seconds` 秒后自动回锁；阻塞到回锁完成。"""

    def lock(self) -> None:
        """立刻回锁（不等待）。"""

    def close(self) -> None: ...

    def status(self) -> dict: ...


class MockDriver:
    """没有硬件时的驱动：记录每次动作，让策略与界面能完整联调。"""

    name = "mock"

    def __init__(self, pulse: float = 5.0) -> None:
        self.pulse = pulse
        self.history: list[dict] = []
        self.unlocked = False
        self.error: str | None = None

    def unlock(self, seconds: float) -> None:
        self.unlocked = True
        self.history.append({"at": time.time(), "seconds": seconds})
        logger.info("锁体（模拟）开锁 %.1f s", seconds)
        time.sleep(seconds)
        self.unlocked = False

    def lock(self) -> None:
        self.unlocked = False

    def close(self) -> None:
        self.unlocked = False

    def status(self) -> dict:
        return {"driver": self.name, "unlocked": self.unlocked, "pulses": len(self.history)}


class GpioRelayDriver:
    """用 libgpiod 的 `gpioset` 驱动一路继电器。

    为什么用命令行而不是 gpiod 的 Python 绑定：板子上只有 libgpiod v2 的 CLI，
    `pip install gpiod` 需要联网编译，而一开一合两三条命令足够可靠，也不用处理
    内核 ABI 差异。代价是每次开锁 fork 两个进程（约 5 ms），对门锁完全够用。
    """

    name = "gpio"

    def __init__(self, chip: str = "gpiochip0", line: int = 0, *,
                 active_low: bool = False, pulse: float = 5.0,
                 bias: str = "pull-down", binary: str = "gpioset") -> None:
        self.chip = chip
        self.line = int(line)
        self.active_low = active_low
        self.pulse = pulse
        self.bias = bias
        self.binary = binary
        self.unlocked = False

    # ---------------- 内部 ----------------
    def _argv(self, value: int, hold_ms: int) -> list[str]:
        return [
            self.binary, "-c", self.chip, "-C", "doorlock",
            "-p", f"{hold_ms}ms", f"--bias={self.bias}",
            f"{self.line}={value}",
        ]

    def _hold(self, value: int, hold_ms: int) -> None:
        """把线路驱动到 `value` 并保持 `hold_ms`，然后释放。

        `gpioset` 不带 `--toggle` 时可能一直不退出（这样能保证电平），
        所以统一用 Popen + 到点终止，两种实现都能工作。
        """
        process = subprocess.Popen(
            self._argv(value, hold_ms),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            out, err = process.communicate(timeout=max(0.2, hold_ms / 1000 + 0.5))
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                out, err = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate()
        if process.returncode not in (0, None, -15):
            raise LockDriverError((err or out or "gpioset 失败").strip())

    def _level(self, on: bool) -> int:
        return 0 if (on and self.active_low) else 1 if on else (1 if self.active_low else 0)

    # ---------------- 接口 ----------------
    def unlock(self, seconds: float) -> None:
        self.unlocked = True
        failure: Exception | None = None
        try:
            self._hold(self._level(True), int(max(0.1, seconds) * 1000))
        except (LockDriverError, OSError) as exc:
            failure = exc
        finally:
            # 无论上面是否抛错，都要把继电器放掉；放掉失败不能盖掉原始错误。
            try:
                self._hold(self._level(False), INACTIVE_HOLD_MS)
            except (LockDriverError, OSError) as exc:
                logger.error("释放继电器失败：%s", exc)
                failure = failure or exc
            finally:
                self.unlocked = False
        if failure is not None:
            raise LockDriverError(str(failure))

    def lock(self) -> None:
        self.unlocked = False
        try:
            self._hold(self._level(False), INACTIVE_HOLD_MS)
        except (LockDriverError, OSError) as exc:
            logger.error("回锁失败：%s", exc)

    def close(self) -> None:
        self.lock()

    def status(self) -> dict:
        return {
            "driver": self.name, "chip": self.chip, "line": self.line,
            "active_low": self.active_low, "unlocked": self.unlocked,
        }

    @classmethod
    def probe(cls, chip: str = "gpiochip0") -> str | None:
        """返回 None 表示可用，否则返回一句可操作的中文原因。"""
        from shutil import which

        if which("gpioset") is None:
            return "未安装 libgpiod 工具（gpioset）"
        try:
            done = subprocess.run(
                ["gpioinfo", "-c", chip], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"无法执行 gpioinfo：{exc}"
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()
            if "Permission denied" in detail:
                return (f"没有 {chip} 的访问权限；请执行 deploy/install_gpio_access.sh "
                        f"并把用户加入 gpio 组后重新登录")
            return f"无法打开 {chip}：{detail}"
        return None


class McuDriver:
    """ESP32-S3 执行单元（方案 B）。帧格式见 `lock.protocol`。

    写出去就算成功是**故意**的：MCU 校验失败会自己不开锁并通过 STATUS 上报，
    Pi 端不能因为"没收到 ACK"就重发 —— 重发会让一次按压变成两次开锁。
    """

    name = "mcu"

    def __init__(self, port: str, key: bytes, *, pulse: float = 5.0,
                 baudrate: int = 115200, serial_factory=None) -> None:
        if not key:
            raise LockDriverError("未配置 DOORLOCK_MCU_KEY，无法签名")
        if serial_factory is None:
            import serial

            serial_factory = serial.Serial
        self.port = port
        self.key = key
        self.pulse = pulse
        self._serial = serial_factory(port, baudrate=baudrate, timeout=1, write_timeout=1)
        self._seq = 0
        self.unlocked = False

    def _send(self, cmd: int, payload: bytes) -> None:
        self._seq = (self._seq + 1) % 256
        frame = protocol.build_frame(cmd, self._seq, payload, self.key)
        self._serial.write(frame)
        self._serial.flush()

    def unlock(self, seconds: float) -> None:
        self._send(protocol.CMD_UNLOCK, protocol.unlock_payload("", "mcu", nonce=str(self._seq)))
        self.unlocked = True
        time.sleep(seconds)
        self.unlocked = False

    def lock(self) -> None:
        self.unlocked = False
        self._send(protocol.CMD_LOCK, b"{}")

    def close(self) -> None:
        try:
            self.lock()
        finally:
            self._serial.close()

    def status(self) -> dict:
        return {"driver": self.name, "port": self.port, "unlocked": self.unlocked}


class PwmServoDriver:
    """Pin33 = PWM2_CH3，Linux 将该控制器暴露为 pwmchipN/pwm0。"""

    name = "servo"
    PERIOD_NS = 20_000_000
    CONTROLLER = "2ade3000.pwm"

    def __init__(self, *, sysfs: str | Path = "/sys/class/pwm",
                 locked_angle: float = 0, unlocked_angle: float = 90,
                 min_us: float = 500, max_us: float = 2500,
                 move_s: float = 0.5) -> None:
        values = (locked_angle, unlocked_angle, min_us, max_us, move_s)
        if not all(math.isfinite(value) for value in values):
            raise LockDriverError("舵机参数必须是有限数值")
        if not (0 <= locked_angle <= 180 and 0 <= unlocked_angle <= 180):
            raise LockDriverError("舵机角度必须在 0 到 180 度之间")
        if locked_angle == unlocked_angle:
            raise LockDriverError("开锁与回锁角度不能相同")
        if not 0 < min_us < max_us < self.PERIOD_NS / 1000 or move_s < 0.02:
            raise LockDriverError("舵机脉宽或运动等待时间无效")
        self.sysfs = Path(sysfs)
        self.locked_angle = locked_angle
        self.unlocked_angle = unlocked_angle
        self.min_us = min_us
        self.max_us = max_us
        self.move_s = move_s
        self.channel: Path | None = None
        self._ready = False
        self.unlocked = False
        self.error: str | None = None

    def _duty(self, angle: float) -> int:
        return round((self.min_us + (self.max_us - self.min_us) * angle / 180) * 1000)

    def _write(self, path: Path, value: int | str) -> None:
        try:
            path.write_text(f"{value}\n", encoding="ascii")
        except PermissionError as exc:
            self.error = (f"PWM 写入失败 {path}：{exc}；请执行 "
                          "sudo bash deploy/install_pwm_access.sh pi 后重启门锁程序")
            raise LockDriverError(self.error) from exc
        except OSError as exc:
            self.error = f"PWM 写入失败 {path}：{exc}"
            raise LockDriverError(self.error) from exc

    def _prepare(self) -> None:
        if self._ready:
            return
        chips = [
            chip for chip in self.sysfs.glob("pwmchip*")
            if (chip / "device").resolve().name == self.CONTROLLER
        ]
        if len(chips) != 1:
            raise LockDriverError("未找到 M2 Pin33 PWM 控制器 2ade3000.pwm；请检查内核和引脚复用")
        chip = chips[0]
        self.channel = channel = chip / "pwm0"
        if not channel.is_dir():
            self._write(chip / "export", 0)
            deadline = time.monotonic() + 1
            while not channel.is_dir():
                if time.monotonic() >= deadline:
                    raise LockDriverError(f"导出 PWM 超时：{channel}")
                time.sleep(0.01)
        period_path = channel / "period"
        try:
            current_period = int(period_path.read_text(encoding="ascii").strip())
            if current_period < 0:
                raise ValueError(f"周期不能为负数：{current_period}")
        except (OSError, ValueError) as exc:
            self.error = f"PWM 周期读取失败 {period_path}：{exc}"
            raise LockDriverError(self.error) from exc

        # 新导出的通道周期为 0，内核会拒绝写入 enable 和 duty_cycle。
        if current_period == 0:
            self._write(period_path, self.PERIOD_NS)
        self._write(channel / "enable", 0)
        self._write(channel / "duty_cycle", 0)
        self._write(channel / "polarity", "normal")
        if current_period != 0:
            self._write(period_path, self.PERIOD_NS)
        self._ready = True

    def _move(self, angle: float) -> None:
        self._prepare()
        assert self.channel is not None
        self._write(self.channel / "duty_cycle", self._duty(angle))
        self._write(self.channel / "enable", 1)
        time.sleep(self.move_s)

    def unlock(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            raise LockDriverError("开锁保持时间必须大于 0")
        failure: LockDriverError | None = None
        try:
            self._move(self.unlocked_angle)
            self.unlocked = True
            time.sleep(seconds)
        except LockDriverError as exc:
            failure = exc
        finally:
            try:
                self.lock()
            except LockDriverError as exc:
                logger.error("舵机自动回锁失败：%s", exc)
                failure = failure or exc
        if failure is not None:
            self.error = str(failure)
            raise failure

    def lock(self) -> None:
        self._move(self.locked_angle)
        self.unlocked = False
        self.error = None

    def close(self) -> None:
        try:
            self.lock()
        finally:
            if self.channel is not None and (self.channel / "enable").exists():
                self._write(self.channel / "enable", 0)
            self._ready = False

    def status(self) -> dict:
        return {
            "driver": self.name, "pin": 33, "controller": self.CONTROLLER,
            "channel": str(self.channel) if self.channel else None,
            "period_ns": self.PERIOD_NS, "locked_angle": self.locked_angle,
            "unlocked_angle": self.unlocked_angle,
            "unlocked": self.unlocked, "error": self.error,
        }


def build_driver(kind: str, *, pulse: float, chip: str, line: int,
                 active_low: bool, port: str = "", key: bytes = b"",
                 locked_angle: float = 0, unlocked_angle: float = 90,
                 min_us: float = 500, max_us: float = 2500,
                 move_s: float = 0.5) -> LockDriver:
    if kind == "gpio":
        return GpioRelayDriver(chip=chip, line=line, active_low=active_low, pulse=pulse)
    if kind == "mcu":
        return McuDriver(port, key, pulse=pulse)
    if kind == "servo":
        return PwmServoDriver(
            locked_angle=locked_angle, unlocked_angle=unlocked_angle,
            min_us=min_us, max_us=max_us, move_s=move_s,
        )
    if kind == "mock":
        return MockDriver(pulse=pulse)
    raise LockDriverError(f"未知锁体驱动：{kind}")
