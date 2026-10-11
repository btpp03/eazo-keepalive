# eazo-keepalive

保活 **eazo.ai**（AI app builder）里那台免费的 e2b 微虚机 —— 它上面跑着：
- 哪吒探针 agent（面板 `nezhak2.btpp.ggff.net`，记录名 `eazo`）
- VLESS+WS 节点（沙箱 8080，经 e2b 代理 `https://8080-<sandboxId>.e2b.app` 对外）
- VMess+WS 节点（沙箱 8001，经 cloudflared 的 `*.trycloudflare.com` 隧道）

## 为什么需要保活

e2b 微虚机**闲置一段时间会被 pause**：进程和内存都留着，但网络断掉 ——
外面看就是「哪吒掉线了」，而箱子里三个进程一个都没死。**没有任何东西会自动唤醒它。**
一个进来的 HTTP 请求就会让它解冻，agent 在 ~10 秒内恢复上报。

实测：2026-10-11 沙箱从 18:44 冻结到 21:02（**2 小时 18 分**），期间面板 `LastActive`
一动不动；一个 GET 过去，当场就活了。

## 两条腿

| 腿 | 谁在跑 | 频率 | 干什么 |
|---|---|---|---|
| A. 直连 GET | cron-job.org → `https://<host>/health` | 10 分钟 | **真正的保活**：把沙箱顶醒。不需要 PAT、不需要 GitHub，秒级精准 |
| B. 本仓 workflow | cron-job.org → `workflow_dispatch` | 30 分钟 | 体检 + 报警（沙箱换了新的 / 彻底没响应时通知 TG） |

腿 A 是必须的；腿 B 只在出问题时才会发消息，平时**一句话都不发**。

## workflow 行为

`keepalive.py`：

1. `GET https://$EAZO_SANDBOX_HOST/health`，失败重试 3 次 × 25s 超时。
2. 200 → 正常。**定时触发静默退出**；**手动 dispatch 会推一条 TG**。
3. 一直失败 → 调 eazo session API 问「这个 chat 现在的沙箱是哪台」：
   - 还是同一台 → ❌ TG：沙箱没响应（可能还冻着，也可能真挂了）。
   - 换了新的 ID → ⚠️ TG：沙箱被回收了，给出新地址，需要重建节点。

手动跑（验证用）：Actions → *eazo keepalive* → Run workflow → 勾 `force`。
`force` 会跳过「活着就早退」，把整条链路走一遍。

## Secrets

| 名字 | 值 |
|---|---|
| `EAZO_SANDBOX_HOST` | `<port>-<sandboxId>.e2b.app`，例如 `7070-i8r6k1uasvra8z0u0s1ot.e2b.app` |
| `EAZO_TOKEN` | eazo 的 bearer（`localStorage._authing_token`，JWT exp 到 2036 年） |
| `EAZO_CHAT_ID` | `v5chat_...` |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 通知 bot |

仓库里不落任何节点链接、不落凭据 —— 全部走 Secrets。

## 沙箱真被回收了怎么办（重建 runbook）

workflow 会报出新 sandboxId。重建只要三步（都在沙箱里跑，走 7070 那个
OpenClacky agent 的 WS API，没法 SSH）：

1. 起节点：下载 sing-box，写 `vless` + `ws` 入站到 `8080`，`setsid` 拉起来。
   链接形状：
   `vless://<uuid>@8080-<newId>.e2b.app:443?encryption=none&security=tls&sni=<host>&fp=chrome&type=ws&host=<host>&path=%2Fws#eazo-oregon`
   服务端**不开 TLS** —— e2b 代理那一层终结 TLS。
2. 起哪吒：`curl -sL https://amd64.ssss.nyc.mn/agent -o agent && chmod +x agent`，
   `setsid nohup ./agent -s nezhak2.btpp.ggff.net:443 -p <KEY> --tls --disable-auto-update ...`
   （别用 `sb.sh` 那一行 —— 它的 curl/wget 探测有 bug，在这台机器上会「假装安装成功」）
3. 起第二节点：`cloudflared tunnel --url http://127.0.0.1:8001` 拿一个新的
   `*.trycloudflare.com` 域名，配 vmess `path=/vmess-argo`。

新链接要**重新发给用户**（不能进 repo）。

## 沙箱里的实测规格

- 2 vCPU / 3.9 GB / Debian 12 / x86_64
- 出口 IP `35.252.97.251`（The Dalles, Oregon, US，AS396982 Google LLC，GCP us-west1）
- 没有 SSH；对外只有 `<port>-<sandboxId>.e2b.app`（HTTP + WebSocket，无需鉴权）
- 面板里显示成 `Unikraft / 1 核 / 512MB` 是 agent 从 SMBIOS 抓到的假值，别信
