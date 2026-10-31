# Web 管理页

R1 上的 HTTPS 管理服务由 `r1-web.service` 提供，源码入口是 `r1_web.py`。它同时承担三种角色：给浏览器看的页面、给未登录访客看的脱敏只读图、以及向其他服务转发控制命令的网关。

## 架构

```
浏览器 ──HTTPS(8443)──> r1_web.py ──root-only unix socket──> r1_panel_next.py（唯一的前屏进程）
                              │
                              ├──> r1_fan_control_ipc.py（请求全速/自动、读状态）
                              ├──> /var/lib/r1-fan-curve/curves.json（曲线读写）
                              └──> r1_rgb_control.py（RGB 状态）
```

关键设计：**Web 进程不直接写 framebuffer，也不直接写 PWM**。所有控制都通过只允许 root 访问的 Unix socket 交给对应进程，Web 只做渲染和转发。

## 访问

- 入口：`https://<R1的IP>:8443`
- 只监听配置的家庭 IPv4 地址，**不监听 `::`**
- 自签证书，首次访问需手动信任。建议先核对指纹：

```sh
openssl x509 -in /var/lib/r1-web/server.crt -noout -fingerprint -sha256
```

- 初始管理员密码只写在 `/var/lib/r1-web/initial-password`（root 可读），`init` 时不会打印到终端
- 登录后 8 小时会话；改密成功后所有会话立即失效，需重新登录

## 渲染模板

**前屏和 Web 页共用同一套模板**，即文件名带 `_web` 的两份：

| 模板 | 页面 |
| --- | --- |
| `r1_web_information_web.svg` | 信息页 |
| `r1_web_fans_web.svg` | 风扇页 |

`PanelRenderer.draw()` 与 Web 端都固定以 `web_layout=True` 调用 `render_information()` / `render_fans()`，所以两处画面一致。

`r1_web_information.svg` 和 `r1_web_fans.svg` 是早期布局——虚拟机是底部独立的「虚拟机状态」卡片，第一张卡标题为「处理器」。生产路径已经不再使用它们，只有 `tests/test_web.py` 里不带参数的默认调用仍会渲染，属于待清理的历史遗留；删除时需同步调整那些测试。

## 未登录能看什么

`/public/panel.png` 和 `/public/fans.png` 两张只读图，约 15 秒刷新一次。

- 在 Web 进程内**离屏绘制**，不读取实体屏 framebuffer、不切换实体页
- 不提供任何可点击的控件；图里画出来的按钮只是状态示意
- 硬盘序列号统一显示为 `****`，虚拟机只显示 `VM 100` 这类编号，不显示名称或主机 IP
- 仍然要求来源地址在允许范围内

## 登录后能做什么

- 镜像实体屏画面（约 1.5 秒读取一次 framebuffer），支持信息页 / 风扇页 / PVE 控制台 / 黑屏
- 切页、调亮度、画面单击
- 编辑风扇曲线、提交「全速 1 分钟」或「自动」
- 开关 RGB、切换常亮 / 呼吸 / 彩虹与颜色
- 修改管理员密码（8–256 字符，不能与旧密码相同）

**PVE 控制台页只镜像画面，不能在网页里输入命令。** 黑屏是纯黑图 + 关背光，不代表 LCD 已断电。

## 风扇曲线的限制

页面纵轴是输出百分比，按 `round(原始 PWM × 100 / 255)` 换算：最低可输入 47%（对应安全下限 PWM 120），100% 对应 PWM 255。

- 拖动节点不会写配置，只有点保存才提交
- 提交时**仍然传原始 PWM**，由后端校验、温控服务回读确认
- 新曲线不得低于已验证基准
- 缺温度、缺盘传感器或 RPM 为 0 时，直接请求 PWM 255

## RGB 试用的特殊开关

Web 上的 RGB 试用需要 `/var/lib/r1-web/rgb-verified` 这个 root-only 标记文件才会解锁。它用于「先短时测试、回读、再恢复测试前灯光」的流程。

**不要把该标记复制到别的硬件上。**

## 维护命令

```sh
systemctl status r1-panel.service r1-fan-curve.service r1-web.service --no-pager
systemctl is-enabled r1-panel.service r1-fan-curve.service r1-web.service
ss -lnt 'sport = :8443'
cat /run/r1-panel/state.json
cat /run/r1-fan-curve/status.json
```

## 安全边界

- Web 有会话、CSRF 校验和来源限制，但这些都是**应用层**措施
- **PVE 主机防火墙默认没有启用**，应用层限制不能替代网络层访问控制。公网暴露前必须自行配置防火墙或走内网 / VPN
- 不要把 `/var/lib/r1-web/` 下的 `admin.json`、`server.key`、`initial-password` 提交到仓库或截图外发
- `http://127.0.0.1:8766/` 是本机模拟预览，不是生产 Web 的 HTTP 副本，详见 [ui-preview.md](ui-preview.md)
