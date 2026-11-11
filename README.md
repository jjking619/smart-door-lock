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
人员模型会从系统镜像复制，不会覆盖已有模型。

**包裹模型未包含在仓库中，也没有自动下载地址。** 完整双模型模式需自行准备已转换的
`models/parcel-yolov5n-rk3576-fp.rknn`；未准备时可按下方单模型方式运行。
人员模型若未包含在系统镜像中，也需自行放到 `models/yolov5s-rk3576.rknn` 后重新安装。

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
前，请参阅[详细使用说明](docs/manual.md)；SG90 需要单独配置 PWM 权限，语音需要 Coze 配置。

## 注意事项

- 摄像头关闭或界面离开时，默认会暂停检测；持续安防需配置 `VISION_PAUSE_WITHOUT_UI=0`。
- SG90 无位置反馈，断电或进程崩溃时软件无法保证回锁；须保留机械应急开门方式。
- 指纹管理无需 PIN；录像和事件保存在 `data/`，请注意访问权限和备份。

更多内容：[视觉服务](src/vision/README.md) · [语音服务](src/audio/README.md) ·
[界面运行环境](docs/ui-runtime.md) · [设计方案](docs/design.md)
