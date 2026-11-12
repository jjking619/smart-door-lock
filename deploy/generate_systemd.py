from __future__ import annotations

import os
from pathlib import Path
import pwd
import shlex

SERVICES = ("visiond", "audiood", "fingerprintd", "lockd")
TEMPLATE_ROOT = "@PROJECT_ROOT@"


def unit_quote(value: str, *, command: bool = False) -> str:
    if any(character in value for character in "\0\r\n"):
        raise ValueError("服务路径不能包含换行或空字符")
    value = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def unit_path(value: str) -> str:
    # WorkingDirectory/EnvironmentFile 不支持引号，路径原样写入，只转义 % 说明符。
    if any(character in value for character in "\0\r\n") or value[-1].isspace():
        raise ValueError("服务路径不能以空白字符结尾或包含换行")
    return value.replace("%", "%%")


def generate(root: Path, username: str) -> Path:
    root = root.resolve()
    deploy = root / "deploy" / "systemd"
    generated = deploy / "generated"
    generated.mkdir(parents=True, exist_ok=True)

    # 仅首次创建 600 权限的配置，已有配置（含密钥）保持不变。
    environment = deploy / "doorlock.env"
    try:
        descriptor = os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write((deploy / "doorlock.env.example").read_text(encoding="utf-8"))

    for service in SERVICES:
        lines = (deploy / f"{service}.service").read_text(encoding="utf-8").splitlines()
        output = []
        for line in lines:
            key, separator, value = line.partition("=")
            if separator and key == "User":
                line = f"User={username}"
            elif separator and key in ("WorkingDirectory", "EnvironmentFile"):
                line = f"{key}={unit_path(value.replace(TEMPLATE_ROOT, str(root)))}"
            elif separator and key == "ReadWritePaths":
                line = f"{key}={unit_quote(value.replace(TEMPLATE_ROOT, str(root)))}"
            elif separator and key == "ExecStart":
                arguments = shlex.split(value)
                line = "ExecStart=/usr/bin/env " + " ".join(
                    unit_quote(argument.replace(TEMPLATE_ROOT, str(root)), command=True)
                    for argument in arguments
                )
            output.append(line)
        (generated / f"{service}.service").write_text("\n".join(output) + "\n", encoding="utf-8")
    return generated


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[1]
    user = pwd.getpwuid(os.getuid()).pw_name
    print(generate(project, user))
