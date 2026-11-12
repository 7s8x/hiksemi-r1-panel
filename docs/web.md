# Web 管理页

R1 上的管理服务由 `r1-web.service` 提供，源码入口是 `r1_web.py`。它同时承担三种角色：给浏览器看的页面、给未登录访客看的脱敏只读图、以及向其他服务转发控制命令的网关。

## 架构

```
浏览器 ──HTTP(8080, 默认)──> r1_web.py ──root-only unix socket──> r1_panel_next.py（唯一的前屏进程）
                              │
                              ├──> r1_fan_control_ipc.py（请求全速/高速/低速、读状态）
                              ├──> /var/lib/r1-fan-curve/curves.json 与 curves-low.json（高速/低速两档曲线读写）
                              └──> r1_rgb_control.py（RGB 状态）
```

关键设计：**Web 进程不直接写 framebuffer，也不直接写 PWM**。所有控制都通过只允许 root 访问的 Unix socket 交给对应进程，Web 只做渲染和转发。

## 访问

- 默认 **HTTP**，端口 `8080`，绑定 `0.0.0.0`（监听所有网卡），因此任意内网段都能直接输 IP 访问，无需预先知道网段。
- 需要 HTTPS 时二选一：
  - 直接 `r1_web.py serve --tls`（启用 `init` 生成的自签证书）；
  - 更推荐在前面挂反代（nginx/caddy）做 TLS，仅在自行审核网络入口与认证后使用；不要直接开放原始 HTTP 管理端口。
- 反代场景：默认忽略客户端的 `X-Forwarded-Proto` / `X-Forwarded-Host`。仅在 `R1_WEB_TRUSTED_PROXIES` 显式配置可信代理来源 CIDR 后，才使用这些头设置 cookie 和校验同源，例如同机反代配置 `R1_WEB_TRUSTED_PROXIES=127.0.0.1/32`。代理必须覆盖客户端传入的转发头。
- 来源白名单默认放行回环 + 全部 RFC1918 私网 + CGNAT；如需收紧，设置环境变量 `R1_WEB_ALLOWED`（逗号分隔 CIDR，如 `192.168.1.0/24,10.0.0.0/8`）。
- 若使用 `--tls` 自签证书，首次访问需手动信任。建议先核对指纹：

```sh
openssl x509 -in /var/lib/r1-web/server.crt -noout -fingerprint -sha256
```

- 初始管理员密码只写在 `/var/lib/r1-web/initial-password`（root 可读），`init` 时不会打印到终端
- 登录后 8 小时会话；改密成功后所有会话立即失效，需重新登录

## 本地访问日志

Web 服务默认会写一个轻量访问日志到 `/var/lib/r1-web/access.log`，便于运维排查。出于隐私考虑，日志**只记录时间戳、来源 IP、HTTP 方法、路径与状态码**，并且：

- 自动**剥离 URL 中的查询串**（避免记录会话令牌、密码等敏感参数）；
- 对 `/api/login`、`/api/password`、`/api/session` 等认证端点**整体脱敏**为 `<redacted>`；
- **绝不记录**请求体、会话 cookie 内容、实体屏画面或协议错误中的原始请求行；
- 剥离绝对 URL 的 userinfo，并转义控制字符，防止日志伪造；日志文件权限固定为 `0600`（仅属主可读写）。

日志按 1 MB 滚动，保留最近 3 个备份，避免无限增长。

如需关闭，设置环境变量 `R1_WEB_LOG=0`（也接受 `off` / `false` / `no` / `disable` / `disabled`）：

```sh
R1_WEB_LOG=0 r1_web.py serve
```

## 渲染模板

**前屏和 Web 页共用同一套模板**，即文件名带 `_web` 的两份：

| 模板 | 页面 |
| --- | --- |
| `r1_web_information_web.svg` | 信息页 |
| `r1_web_fans_web.svg` | 风扇页 |

`PanelRenderer.draw()` 与 Web 端都调用 `render_information()` / `render_fans()`，两个函数固定加载上面两份模板，所以两处画面一致。`web_layout` 关键字参数为兼容保留，现在传与不传结果相同。

更多部署及有效性规则见 [分发说明](distribution.md)。

早期另有一套 `r1_web_information.svg` / `r1_web_fans.svg`（虚拟机是底部独立的「虚拟机状态」卡片、第一张卡标题为「处理器」），生产路径早已不用，已从仓库移除。旧版本的校验清单 `SHA256SUMS-v1.3.1.txt` 里仍会提到这两个文件名，那是 v1.3.1 的历史快照。

## 未登录能看什么

`/public/panel.png` 和 `/public/fans.png` 两张只读图，约 15 秒刷新一次。

- 在 Web 进程内**离屏绘制**，不读取实体屏 framebuffer、不切换实体页
- 不提供任何可点击的控件；图里画出来的按钮只是状态示意
- 硬盘序列号统一显示为 `****`，虚拟机只显示 `VM 100` 这类编号，不显示名称或主机 IP
- 仍然要求来源地址在允许范围内

## 登录后能做什么

- 镜像实体屏画面（约 1.5 秒读取一次 framebuffer），支持信息页 / 风扇页 / PVE 控制台 / 黑屏
- 切页、调亮度、画面单击
- 分别编辑高速/低速两套温控曲线、提交「全速 1 分钟」「高速」或「低速」
- 开关 RGB、切换常亮 / 呼吸 / 彩虹与颜色
- 修改管理员密码（8–256 字符，不能与旧密码相同）

**PVE 控制台页只镜像画面，不能在网页里输入命令。** 黑屏是纯黑图 + 关背光，不代表 LCD 已断电。

## 风扇曲线的限制

页面纵轴是输出百分比，按 `round(原始 PWM × 100 / 255)` 换算：最低可输入 47%（对应安全下限 PWM 120），100% 对应 PWM 255。

- 拖动节点不会写配置，只有点保存才提交
- 提交时**仍然传原始 PWM**，由后端校验、温控服务回读确认
- 两档曲线分别保存：`curves.json`（高速）与 `curves-low.json`（低速）；低速文件缺失时回退到内置默认曲线
- 高速档新曲线不得低于已验证基准；低速档可更平缓，但仍不得低于 PWM 120、须保持递增并在紧急温度前达到 100%
- 全速是 60 秒临时覆盖，到期回到进入全速前所选档位，不是固定回高速
- 缺温度、缺盘传感器或 RPM 为 0 时，直接请求 PWM 255

## RGB 试用的特殊开关

Web 上的 RGB 试用需要 `/var/lib/r1-web/rgb-verified` 这个 root-only 标记文件才会解锁。它用于「先短时测试、回读、再恢复测试前灯光」的流程。

**不要把该标记复制到别的硬件上。**

## 维护命令

```sh
systemctl status r1-panel.service r1-fan-curve.service r1-web.service --no-pager
systemctl is-enabled r1-panel.service r1-fan-curve.service r1-web.service
ss -lnt 'sport = :8080'
cat /run/r1-panel/state.json
cat /run/r1-fan-curve/status.json
```

## 安全边界

- Web 有会话、CSRF 校验和来源限制，但这些都是**应用层**措施
- **PVE 主机防火墙默认没有启用**，应用层限制不能替代网络层访问控制。公网暴露前必须自行配置防火墙或走内网 / VPN
- 不要把 `/var/lib/r1-web/` 下的 `admin.json`、`server.key`、`initial-password` 提交到仓库或截图外发
- `http://127.0.0.1:8766/` 是本机模拟预览，不是生产 Web 的 HTTP 副本，详见 [ui-preview.md](ui-preview.md)


## 本候选的部署配置

HTTP 初始化不需要 openssl；TLS 证书用 `init --tls --tls-name <实际名称>` 显式生成。Web 环境变量可写入 `/var/lib/r1-web/web.env`，由 systemd 单元读取。域名访问配置 `R1_WEB_HOSTS`；反向代理还要配置 `R1_WEB_TRUSTED_PROXIES`，默认不信任转发头。`R1_WEB_PUBLIC=0` 关闭匿名双页图，登录后仍可用。详见主 README。

最多 32 个活动连接（含 TLS 握手），15 秒空闲/读超时。畸形 Content-Length、Transfer-Encoding、未消费正文和错误响应不复用连接。改密后清理初始密码文件，清理失败单独记录，不把已生效的改密误报为失败。
