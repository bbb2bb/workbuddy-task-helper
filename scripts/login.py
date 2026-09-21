#!/usr/bin/env python3
"""WorkBuddy 账号登录 → 落盘 auths/workbuddy-<uid>.json

纯 Python 标准库实现，不需要 Go / Docker。
流程（与上游 cmd/login + login.sh 同口径）：

  1. POST {copilot.tencent.com}/v2/plugin/auth/state?platform=CLI   拿 state + 授权链接
  2. 在浏览器打开授权链接，完成登录
  3. GET  /v2/plugin/auth/token?state=<state>                       轮询拿 token
  4. GET  /v2/plugin/login/account?state=<state>                    拿 uid / 昵称
  5. 凭证写入 <项目根>/auths/workbuddy-<uid>.json（权限 600）

只使用账号自己的登录态，不发送除登录外的任何请求。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "https://copilot.tencent.com"
ORIGIN = "https://www.codebuddy.cn"
UA = "CLI/2.63.2 CodeBuddy/2.63.2"

# 与 task_common._resolve_auths_dir 同口径：脚本位于 scripts/，项目根为其上一级
AUTH_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auths"
)

POLL_INTERVAL = 2.0
POLL_MAX = 150  # 2s × 150 = 5 分钟


def _headers(token=None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": UA,
    }
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _call(method, path, body=None, token=None, timeout=30):
    """上游 {code,msg,data} 信封；code != 0 抛 RuntimeError。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=_headers(token),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError("HTTP %d: %s" % (e.code, raw[:200]))
    try:
        env = json.loads(raw)
    except Exception:
        raise RuntimeError("返回不是 JSON：%s" % raw[:200])
    if env.get("code") not in (0, None):
        raise RuntimeError("code=%s msg=%s" % (env.get("code"), env.get("msg")))
    return env.get("data") or {}


def _open_browser(url):
    """尽力打开浏览器；失败不影响流程。"""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
        elif os.name == "nt":
            os.startfile(url)  # noqa
        else:
            subprocess.Popen(["xdg-open", url])
        return True
    except Exception:
        return False


def main():
    print("=" * 60)
    print("  WorkBuddy 账号登录")
    print("=" * 60)
    print()

    try:
        d = _call("POST", "/v2/plugin/auth/state?platform=CLI", body={})
    except RuntimeError as e:
        print("❌ 取授权链接失败：%s" % e)
        print("   请检查网络能否访问 copilot.tencent.com")
        return 1

    state = d.get("state")
    auth_url = d.get("authUrl") or d.get("auth_url")
    if not state or not auth_url:
        print("❌ 上游没返回授权链接：%s" % json.dumps(d, ensure_ascii=False)[:300])
        return 1

    print("第 1 步 · 请在浏览器里完成登录。")
    print()
    print("  授权链接（已尝试自动打开，没弹出来就手动复制）：")
    print()
    print("  " + auth_url)
    print()
    if _open_browser(auth_url):
        print("  已尝试为你打开浏览器……")
    else:
        print("  自动打开失败，请手动复制上面这条链接到浏览器。")
    print()
    print("第 2 步 · 登录完成后回到这个窗口，什么都不用按，我会自动检测。")
    print()

    token = None
    for i in range(POLL_MAX):
        try:
            d = _call("GET", "/v2/plugin/auth/token?state=" + state)
            if d.get("accessToken"):
                token = d
                break
        except RuntimeError:
            pass
        if i % 5 == 0:
            sys.stdout.write("\r  等待登录中…… %d 秒" % int(i * POLL_INTERVAL))
            sys.stdout.flush()
        time.sleep(POLL_INTERVAL)

    print("\r  等待登录中…… 完成            ")
    if not token:
        print()
        print("❌ 等待超时（5 分钟），没检测到登录完成。")
        print("   重新双击一次即可重来。")
        return 1

    uid = nick = ent = ""
    try:
        acct = _call(
            "GET",
            "/v2/plugin/login/account?state=" + state,
            token=token["accessToken"],
        )
        uid = acct.get("uid") or ""
        nick = acct.get("nickname") or ""
        ent = acct.get("enterpriseId") or ""
    except RuntimeError as e:
        print("⚠️  取账号信息失败（token 已拿到，仍会落盘）：%s" % e)

    if not uid:
        uid = "unknown-%d" % int(time.time())

    expires_in = token.get("expiresIn") or 0
    auth = {
        "account": {"uid": uid, "enterpriseId": ent, "nickname": nick},
        "auth": {
            "accessToken": token["accessToken"],
            "refreshToken": token.get("refreshToken") or "",
            "expiresAt": int(time.time()) + int(expires_in),
            "domain": token.get("domain") or "",
            "realm": "cn",
        },
    }

    os.makedirs(AUTH_DIR, exist_ok=True)
    path = os.path.join(AUTH_DIR, "workbuddy-%s.json" % uid)
    existed = os.path.exists(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    print()
    print("=" * 60)
    print("  ✅ 登录成功")
    print("=" * 60)
    print("  账号 uid : %s" % uid)
    print("  昵称     : %s" % (nick or "(未取到)"))
    print("  凭证文件 : %s" % ("覆盖更新" if existed else "新增"))
    print("  %s" % path)
    if expires_in:
        print("  有效时长 : 约 %.1f 小时" % (int(expires_in) / 3600.0))
    print()
    print("  登录流程结束，正在刷新账号列表……")
    return 0


if __name__ == "__main__":
    sys.exit(main())
