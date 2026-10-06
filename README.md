# HIKSEMI R1 · PVE 前屏与硬件实验

本私有仓库保存海康 R1 在 PVE 上的前屏、独立软件温控、RGB 灯与 HTTPS Web 管理。`main` 保存最新现场验收版；`stable-2026-10-05-fan-rgb` 保留旧版作为回退点，`develop` 保留为后续开发。旧三状态屏幕仍可从 `stable-2026-10-03-swipe-cache` 恢复。

## 当前功能与证据（2026-10-06）

| 功能 | 已验证 | 尚未验证 |
| --- | --- | --- |
| 前屏总览 | 376×960 DSI、Cairo/framebuffer；左右滑切四状态、上下滑调亮度，黑屏唤醒无控制台闪屏；前台采集已移到后台；tty2 光标隐藏后固定小黑块消失 | 整机重启后的自动恢复、实体控制台顶部亮带原因 |
| 风扇页与温控 | IT8613E `fan2/fan3` RPM/PWM；两路共用保守温度曲线；`FULL SPEED` 60 秒后自动回曲线；实体与 Web 按钮已验收 | 两路风扇的物理身份、长时间热循环及内核升级兼容性 |
| RGB | 前屏与 Web 开关、常亮、呼吸、彩虹及颜色现场验收；按已验证 PCI 控制器定位会变号的 I²C 总线，只访问 `0x26` | 长期运行观察 |
| HTTPS Web | 登录后镜像实体屏四状态，可切页、调亮度、点实体屏；温度-PWM 坐标图拖动并经校验保存；控制台只读 | PVE 主机防火墙尚未启用 |

PVE 已启用 `r1-panel.service`、`r1-fan-curve.service`、`r1-web.service`，三者均 active/enabled，离线测试 45 项通过；用户已现场确认 Web 与实体 RGB 修复后正常。未为了验证而重启 PVE。此仓库不包含驱动二进制、fnOS 安装包、私钥、密码、硬盘序列号或用户专用配置。仓库版面板用本机未追踪的 `drives.json` 读取磁盘标识。

## 文件

- `r1_panel_next.py` / `r1_panel_repository.py` / `r1_panel_runtime.py`：当前前屏。由本机未跟踪的 `drives.json` 加载硬盘标识；缺失时仍可运行，只是不显示逐盘温度。`r1_panel_buttons.py` 是 10 月 5 日旧稳定版。
- `r1_fan_curve.py` / `r1_fan_control_ipc.py`：保守软件温控与屏幕模式请求；风扇停转或传感器缺失时请求全速。
- `r1-panel.service` / `r1-fan-curve.service` / `r1-web.service`：当前 PVE 服务定义。启用前必须核实主机型号、驱动、目录及安全回退。
- `r1_hardware.py` / `r1_hardware_probe.py`：早期只读硬件探测与离线计算，不属于常驻服务。
- `r1_rgb_probe.py`：只对已知 `0x26` 地址定点读取，不扫整个 I²C 总线。
- `r1_rgb_trial.py`：单台主机使用的短时 RGB 协议验证，原状态基线写死为 `(2,255,0,0)`；**不要在其他机器或状态改变后直接执行**。
- `r1_rgb_control.py`：前屏与 Web 共用的 RGB 控制；按固定 PCI 控制器路径动态解析 I²C 总线，读写后回读。
- `r1_web.py` / `r1_web.html` / `r1_fan_config.py`：HTTPS Web 管理和经校验的持久风扇曲线。密码、证书和曲线实例文件只保存在本机，不进入仓库。
- `experiments/`：当次风扇通道验证、回退和采样脚本，含当次主机 PWM/背光基线；只作为历史复现资料，不是通用安装程序。
- `tests/`：不接触真实硬件的手势、风扇读数、RGB 回退测试。

不要提交 `drives.json`，可用 `drives.example.json` 作为格式参考。RGB 开关不控制风扇；温控服务独立于前屏和 Web 运行。Web 仅监听已配置的家庭 IPv4 地址并要求 HTTPS 登录，但应用层来源限制不能替代 PVE 主机防火墙。

## 安全边界与回退

本项目没有把 R1 Toolbox 的 fnOS 包安装到 PVE，也没有复制其专有程序或强制 `force_id=0x8628`。本机识别到的芯片是 IT8613E；第三方 `it87` 驱动使用固定源码版本构建和哈希校验，二进制不在仓库内。内核升级后必须重新验证/构建，不能假定可直接复用。

只停止前屏：`systemctl disable --now r1-panel.service`，确认控制台和背光恢复；这不会停止温控。若要回退旧屏幕版，先停当前服务，再从 `stable-2026-10-03-swipe-cache` 取旧代码。**不要同时启动两份读取触控和写 framebuffer 的进程**。温控回退必须先检查温度、RPM 和 PWM，再按实际安全状态处理；不要直接把风扇强制降回最低值。无需为前屏回退重启 PVE 或虚拟机。

离线验证：在项目虚拟环境内运行 `.venv/bin/python -m unittest discover -s tests -v`。驱动探测、I²C 写入和现场画面效果必须分别实测，单元测试不能替代硬件验证。
