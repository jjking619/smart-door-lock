"""doorui —— 门锁桌面操作界面（PySide6）。

设计说明见 docs/ui-design.md。UI 只是瘦客户端：不碰相机、不碰 UART、不碰锁体，
只消费 visiond 的 HTTP 接口，开锁请求仍由 lockd/MCU 决策。
"""
