# 海康 R1 前屏状态面板

2026-10-03 已实机验收：海康 R1 原生 376×960 DSI 前屏可显示 PVE 状态、CPU/NVMe/硬盘温度、内存、管理 IP 和 VM 100/111 状态。每 15 秒更新一次实时数据，不提供虚拟机电源控制或联网功能。

## 触控与显示

- 左滑按“信息面板（tty2）→ PVE 控制台（tty1）→ 黑屏 → 信息面板”前进，右滑反向切换。
- 上滑调亮、下滑调暗；单击已识别但当前不执行操作，留给后续功能。
- 黑屏回信息面板时，先在暗屏下切换 tty、解除 framebuffer blank 并绘制缓存画面，最后恢复亮度；亮屏后再刷新实时数据。用户实测明显比旧版快，且没有控制台闪屏；试运行日志中的一次黑屏回面板切换约 0.87 秒。
- “黑屏”是清空 framebuffer 并关闭背光，不代表已确认 LCD 硬件完全断电。

## 当前部署与回退

PVE 上当前运行的是临时服务 `r1-panel-optimized.service`，脚本为 `/root/r1-screen/r1_panel_develop.py`，使用 `/root/r1-screen/.venv/bin/python` 与系统的 `libcairo.so.2`。**未设置开机自启**；重启 PVE 后需重新核对实际状态。停止面板不影响 PVE 或虚拟机：

```sh
systemctl status r1-panel-optimized.service
systemctl stop r1-panel-optimized.service
```

旧稳定脚本 `/root/r1-screen/r1_panel.py` 保留未改，SHA-256 为 `1cf45791491472e4bd8bb5c7c69a9298ee92a3b54b1045a3e29d1b68f0605ed4`。需要回退时，先停止当前前屏服务，再运行旧脚本；不要同时运行两份触控程序，也不要通过重启 PVE/虚拟机修复前屏。旧代码另保存在 GitHub `stable-2026-10-03` 分支。

## 私有仓库与本机配置

GitHub `main` 为当前已验收版本，`develop` 用于下一阶段开发。仓库代码与 PVE 运行副本的触控和显示逻辑一致，但为了不上传本机硬盘序列号，仓库版从未追踪的 `drives.json` 读取硬盘 `/dev/disk/by-id/` 标识；PVE 运行副本仍内置这些标识。复制 `drives.example.json` 为 `drives.json`，逐项填写本机 `label` 与 `by_id`。没有此文件时面板可运行，但不显示逐盘温度。不要提交 `drives.json`。

触控设备通过 `CST0836:00 0911:5288` 名称定位，不依赖易变的 event 编号。旧的 `KD_GRAPHICS` 试验曾造成画面卡住，当前版本未使用。前屏服务仍是临时单元；部署、开机自启和屏幕硬件控制须另行设计与验证。
