# 智能门锁

基于 Quectel Pi M2智能主控板的智能门锁示例，支持摄像头预览与录像、
人员和包裹检测、指纹开锁及 Coze 语音应答。

## 硬件与环境

- Quectel Pi M2智能主控板、Debian 13 桌面系统（系统 Python 3.13）、网络连接、USB 摄像头和显示设备。
- 指纹、舵机和语音功能分别需要 AS608、SG90、麦克风和扬声器。
- 安装需 sudo 权限和足够磁盘空间；脚本不会修改系统默认 Python。

## 一键部署

在终端执行：

```bash
git clone https://github.com/jjking619/smart-door-lock.git
cd smart-door-lock
bash install.sh
```

出现 `Deployment complete` 表示环境安装完成；如有报错，请按提示处理后重新运行。
脚本创建 `.venv` 和 `.venv-ui`，安装系统依赖、Python 依赖及 RKNN Lite。
同时安装/加载 CH340/CH341 串口驱动、加入 `dialout` 串口组，并安装 Pin33 PWM
开机授权服务（不输出波形、不启动门锁）。完成后注销并重新登录，让组权限生效。
使用 GPIO 继电器时运行 `bash install.sh --gpio`，额外配置 GPIO 权限；
默认不会开放 GPIO。驱动缺失且内核头文件不匹配时会报错停止，请安装匹配的头文件
或设置 `KERNEL_HEADERS` 后重试；内核升级后可单独重跑驱动脚本。
仓库已包含 RK3576 人员模型 `models/yolov5s-rk3576.rknn` 和包裹模型
`models/parcel-yolov5n-rk3576-fp.rknn`。人员模型缺失时脚本会从系统镜像复制，
不会覆盖已有模型；包裹模型缺失时可按下方单模型方式运行。

## 运行

在板子的桌面终端中：

```bash
cd smart-door-lock
LOCK_DRIVER=mock .venv/bin/python main.py
```

上面使用模拟锁体，不会驱动实际门锁。没有包裹模型时，改用：

```bash
VISION_PACKAGE_MODEL="" LOCK_DRIVER=mock .venv/bin/python main.py
```

单模型模式不使用专用纸箱模型，包裹识别能力不同。使用实际舵机、指纹模块或语音
前，请参阅[详细使用说明](docs/manual.md)；语音需要 Coze 配置。

## 注意事项

- 安装时根据当前用户和项目路径生成 systemd 服务文件；仅首次从
  [`doorlock.env.example`](deploy/systemd/doorlock.env.example) 创建配置，已有密钥不会覆盖。
  服务不会自动安装或启动，详见[部署说明](docs/architecture.md#7-部署)。
- 摄像头关闭或界面离开时，默认会暂停检测；持续安防需配置 `VISION_PAUSE_WITHOUT_UI=0`。
- SG90 无位置反馈，断电或进程崩溃时软件无法保证回锁；须保留机械应急开门方式。
- 指纹管理无需 PIN；录像和事件保存在 `data/`，请注意访问权限和备份。

更多内容：[视觉服务](src/vision/README.md) · [语音服务](src/audio/README.md) ·
[界面运行环境](docs/ui-runtime.md) · [设计方案](docs/design.md)
