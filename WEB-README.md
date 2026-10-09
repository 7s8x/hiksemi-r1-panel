# R1 前屏 Web 管理（2026-10-09 状态）

生产入口为 `https://192.168.50.3:8443`；`r1-web.service` 与独立温控服务为 active/enabled。前屏在受保护的 24 小时长测中为 active/disabled，预计 2026-10-09 19:52 CST 自动停用，不会开机自启。Web 仅监听 R1 家庭 IPv4 地址，不监听 `::`；经家庭网或 Tailscale 子网路由访问。首次访问的自签 HTTPS 证书需与 R1 上的指纹核对；2026-10-06 核对值为 `DD:F4:7D:CD:02:07:92:A3:08:E0:5E:3E:7D:8E:7F:D3:3B:7A:F4:0B:F8:3E:D0:C8:39:0A:42:B1:5C:E4:F4:1F`：

```sh
openssl x509 -in /var/lib/r1-web/server.crt -noout -fingerprint -sha256
```

初始管理员密码仅存于 R1 的 `/var/lib/r1-web/initial-password`（root 可读）；不要把该文件、`admin.json`、私钥或控制台截图提交到 GitHub。Web 登录有 8 小时会话、CSRF（跨站请求伪造）校验和来源限制。**PVE 主机防火墙目前仍未启用**；用户明确选择先让 Web 常驻，防火墙后补。应用级限制不能替代主机防火墙，后续须单独核实和实施 PVE 防火墙。

## 功能与边界

- 页面每约 1.5 秒读取实体屏 framebuffer（帧缓冲）画面；当前仅支持信息页、风扇页及黑屏，PVE 控制台页因内屏故障暂时禁用。黑屏使用纯黑图像并关背光，不代表 LCD 硬件已断电。
- Web 的切页、亮度与画面单击命令经 root-only Unix socket 交给唯一的前屏进程；前屏停用时这些命令不可用，PVE 原生管理入口不受影响。
- 风扇曲线由独立 `r1-fan-curve.service` 应用。Web 使用温度横轴／PWM 纵轴的两张坐标图，绿色草稿节点可拖动，也可在下方精确输入；橙色虚线显示服务端返回的已验证基准。拖动不写配置；保存时由后端校验、温控服务回读确认。Web 只能提交曲线和“全速 1 分钟／自动”请求，不能直接写 PWM。新曲线不得低于原已验证曲线；缺温度、缺盘传感器或 RPM 为 0 时仍请求 PWM 255。
- RGB 开关、常亮、呼吸、彩虹与颜色已于 2026-10-05 在本机现场目视验证。Web 试用时通过 root-only 标记 `/var/lib/r1-web/rgb-verified` 解锁；先短时测试、回读，再恢复测试前灯光。不要把该标记复制到别的硬件。
- 2026-10-06 重启后 I²C 编号变化，原 `i2c-0/0x26` 无响应；RGB 控制器在已验证 PCI 路径 `0000:00:15.0/i2c_designware.0` 下的 `i2c-1/0x26` 回读 `(2,255,0,0)`。运行代码改为按该物理路径查找 `/dev/i2c-N`，不再固定编号；若路径或适配器身份不符则拒绝写入。修复前代码备份在 R1 `/root/r1-screen/r1_rgb_control.py.bak-20261006-bus`。故障排查不进行全地址扫描。
- 用户已现场确认总线修复后的 Web 界面和实体 RGB 开关均正常。私有 GitHub `main` 与 `stable-2026-10-06-web-rgb` 保存此次验收版（`7d392a94f679148c98b1f4d1566a6b51f28d860b`）；旧稳定分支不动。
- 2026-10-06 新增网页右上角“修改密码”入口。须在已登录状态输入当前密码、新密码和确认值，新密码限 8–256 字符且不能与旧密码相同；建议实际使用更长的随机密码。后端校验会话、同源和 CSRF，原子替换管理员密码摘要，成功后撤销所有 Web 会话并要求重新登录。不会在网页或日志回显密码。旧的 `/var/lib/r1-web/initial-password` 文件在改密后不再是有效密码，但本次未自动删除该 root-only 文件。修改前 Web 文件备份为 R1 `/root/r1-screen/r1_web.py.bak-20261006-password` 与 `r1_web.html.bak-20261006-password`。47 项离线测试包括使用临时凭据的完整 HTTP 登录→改密→旧会话失效→新密码登录；尚未使用真实密码改密，也未同步 GitHub 稳定分支。
- 将最低长度从 16 调为 8 前的 Web 文件另存为 R1 `/root/r1-screen/r1_web.py.bak-20261006-password-min16` 和 `r1_web.html.bak-20261006-password-min16`。页面上线回读显示“8–256 字符”，未授权请求仍为 401；前屏和温控服务未重启。
- 前屏占用 tty2 时仅隐藏 tty2 控制台光标，退出时恢复。旧控制台画面在实体 LCD 发生色块和停滞，Web 读取的 framebuffer 可能仍更新，故不能用 Web 正常推断实体正常。当前前屏改用 `pwrite` 写 `/dev/fb0`，短测正常，但长期根因尚未证明；不要通过此前导致暗屏的 `KD_GRAPHICS` 试法处理。
- `http://127.0.0.1:8766/` 是仅在本机回环地址运行的无硬件模拟预览，不连接 R1，也不接受真实管理密码；它不是生产 Web 的 HTTP 副本。

## 查看、维护与回退

```sh
systemctl status r1-panel.service r1-fan-curve.service r1-web.service --no-pager
systemctl is-enabled r1-panel.service r1-fan-curve.service r1-web.service
ss -lnt 'sport = :8443'
cat /run/r1-panel/state.json
cat /run/r1-fan-curve/status.json
```

本轮变更前的文件备份在 R1 `/root/r1-screen/backup-20261006-web-axis/`。若出现严重异常，先保持 SSH 连接并核对目标，从 R1 执行 `/bin/sh /root/r1-screen/r1_web_axis_trial_rollback.sh`；它会停用 Web、恢复 2026-10-06 变更前的前屏及风扇脚本与服务定义，不触碰 PVE、OpenWrt 或虚拟机。此回退会撤销 Web 常驻，后续需重新部署和验收。未经核实不要删除备份、密码或证书文件。

2026-10-06：RGB 总线修复后 45 项离线测试、网页脚本语法与服务定义检查通过。用户现场确认小黑块消失、坐标图拖动正常、网页全速按钮使风扇明显提速且显示倒计时；R1 回读 `full/PWM 255`、约 1700/4891 RPM，约 60 秒后自动回到 `auto`，曲线版本不变。HTTP 联调通过登录、CSRF、四状态画面、加速曲线应用后恢复、低于基准的拒绝。配置文件仍为原安全曲线；三项服务 active/enabled、`NRestarts=0`，限时回退计时器已取消。未重启 PVE 或虚拟机，因此尚未实测整机重启后的自动恢复。

2026-10-09 现状：上段为 10 月 6 日历史验收记录。用户已在上次整机重启后确认原生控制台正常；新版前屏的 2 分钟、30 分钟限时试验通过，现持续 24 小时受保护长测，现场反馈运行稳定。47 项离线测试通过。前屏仍 disabled、温控和 Web enabled；不要因为 Web 在线就提前取消停屏计时器或宣称 24 小时长测完成。恢复旧前屏标签/代码可使用 R1 `/root/r1-screen/r1_panel_repository.py.bak-20261009-fan-role`，但须先停前屏、确认回退目标并重新实测，不能覆盖运行文件后直接假定实体屏生效。
