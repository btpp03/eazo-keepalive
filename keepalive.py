#!/usr/bin/env python3
"""eazo 沙箱保活 / 体检.

作用：定时 GET 沙箱的 /health，把 e2b 微虚机从「闲置冻结」里顶醒。
沙箱被冻结时进程还在（内存留着），只是网络断掉；一个进来的 HTTP 请求就会让它解冻，
哪吒 agent 会在 ~10s 内恢复上报。

判定：
  HTTP 200                     -> 正常（沙箱醒着）
  非 200 / 超时（重试后仍失败）  -> 走 eazo session API 看沙箱是不是被换了新的
                                    同一台 -> 只是没醒来，报警
                                    换新的 -> 沙箱被回收，需要重建（报警 + 给出新地址）

通知规则（本用户的规矩）：
  - 定时触发 + 一切正常            -> 静默（一个字都不发）
  - 手动 dispatch                 -> 必发（无论成功失败）
  - 异常                          -> 必发 ❌
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HOST = (os.environ.get("EAZO_SANDBOX_HOST") or "").strip()
TOKEN = (os.environ.get("EAZO_TOKEN") or "").strip()
CHAT = (os.environ.get("EAZO_CHAT_ID") or "").strip()
TG_TOK = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT = (os.environ.get("TG_CHAT_ID") or "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY") or "btpp03/eazo-keepalive"
EVENT = os.environ.get("GITHUB_EVENT_NAME") or "local"
FORCE = (os.environ.get("FORCE") or "").lower() in ("1", "true", "yes")
REPO_URL = "https://github.com/" + REPO

UA = {"User-Agent": "eazo-keepalive/1.0 (+%s)" % REPO_URL}


def log(*a):
    print(*a, flush=True)


def tg(text):
    if not (TG_TOK and TG_CHAT):
        log("[tg] no token/chat configured, skip")
        return False
    body = json.dumps({
        "chat_id": TG_CHAT,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % TG_TOK,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode())
            log("[tg] ok=%s mid=%s" % (j.get("ok"), (j.get("result") or {}).get("message_id")))
            return bool(j.get("ok"))
    except Exception as e:
        log("[tg] FAILED:", repr(e)[:200])
        return False


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=dict(UA))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")[:200], time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200], time.time() - t0
    except Exception as e:
        return 0, repr(e)[:200], time.time() - t0


def probe(attempts=3, gap=15):
    url = "https://%s/health" % HOST
    last = None
    for i in range(1, attempts + 1):
        code, body, dt = http_get(url)
        log("[probe %d/%d] %s -> HTTP %s  %.2fs  %s" % (i, attempts, url, code, dt, body.replace("\n", " ")))
        if code == 200:
            return True, code, body, dt
        last = (code, body, dt)
        if i < attempts:
            time.sleep(gap)
    return False, last[0], last[1], last[2]


def session_info():
    """问 eazo：这个 chat 现在的沙箱是哪一台（换过就是新 ID）。"""
    if not (TOKEN and CHAT):
        return None, "EAZO_TOKEN / EAZO_CHAT_ID 未配置"
    url = ("https://eazo.ai/creator/api/v5/session?chat_id=%s&agent=coding&builder_model=eazo_auto" % CHAT)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/json",
        "Origin": "https://eazo.ai",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
        d = j.get("data") or {}
        return d, None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode(errors="replace")[:200])
    except Exception as e:
        return None, repr(e)[:200]


def main():
    if not HOST:
        tg("❌ eazo 保活｜EAZO_SANDBOX_HOST 没配，脚本跑不了\n" + REPO_URL)
        return 2
    log("host=%s event=%s force=%s repo=%s" % (HOST, EVENT, FORCE, REPO))

    ok, code, body, dt = probe()
    manual = EVENT == "workflow_dispatch"

    if ok and not FORCE:
        log("RESULT=OK")
        if manual:
            tg("✅ eazo 保活正常\n沙箱醒着：HTTP 200 / %.2fs\n%s\n%s"
               % (dt, "https://%s/health" % HOST, REPO_URL))
        else:
            log("healthy scheduled tick -> silent")
        return 0

    # 失败（或 force）：问 eazo 现在这台沙箱是哪个
    log("probe failed, asking eazo session api ...")
    d, err = session_info()
    if err:
        log("session api error:", err)
        tg("❌ eazo 沙箱没响应，且查不到会话\n探测：HTTP %s (%.2fs) %s\n会话查询失败：%s\n%s"
           % (code, dt, body[:120], err, REPO_URL))
        return 1

    new_host = (d.get("httpUrl") or "").split("//")[-1].strip("/")
    sid = d.get("sandboxId") or "?"
    log("eazo says: sandboxId=%s httpUrl=%s" % (sid, d.get("httpUrl")))

    if FORCE and ok:
        tg("ℹ️ eazo 保活 force 模式\n沙箱醒着（HTTP 200 / %.2fs），会话查询也正常\n"
           "sandboxId=%s\n%s" % (dt, sid, REPO_URL))
        return 0

    if new_host and new_host != HOST:
        tg("⚠️ eazo 沙箱换了新的（旧的没了）\n"
           "旧：%s\n新：%s\nsandboxId=%s\n需要用新地址重建节点 + 哪吒\n%s"
           % (HOST, new_host, sid, REPO_URL))
        log("RESULT=NEW_SANDBOX")
        return 3

    tg("❌ eazo 沙箱没响应（同一台，没换）\n"
       "探测：HTTP %s (%.2fs) %s\nsandboxId=%s\n地址：https://%s/health\n"
       "可能还在冻结里，或已经挂了 —— 需要看一眼\n%s"
       % (code, dt, body[:120], sid, HOST, REPO_URL))
    log("RESULT=DEAD_SAME_SANDBOX")
    return 1


if __name__ == "__main__":
    sys.exit(main())
