#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 任务助手（整合版）

双击上一层的「WorkBuddy任务助手.command」使用 —— 全项目只有这一个入口，
登录、挑任务、批量跑全都在这里。

第一屏：账号列表（数字选）+ 四个功能键
    n)  登录新账号（开授权网页 → 回来自动检测 → 自动刷进列表）
    j)  所有账号 · 全量完成任务（含每日任务）
    k)  所有账号 · 全量完成每日任务
    r)  重新扫描账号列表（登录完会自动扫，这是安全网）
    q)  退出

选具体账号后：只读拉取该账号的任务清单 → 输入编号挑任务 → 直接执行 → 自动回到清单。
（输完即执行，不再有 y 二次确认 —— 重复执行是安全的，已完成的任务会自动跳过。）

设计原则（与 中文结果.py 一致）：**不改上游 task_runner.py**。
本脚本只做四件事：查清单、让用户挑、把挑中的编号翻译成 `--only` 参数去调用上游脚本、
把上游日志交给 中文结果.py 排成中文清单。

编号说明：编号只在本次清单内有效，每次执行完重新查询、重新编号。
        分组固定顺序：已经领过的 → 手动任务 → 已完成的每日任务（前三段只展示，不编号、选不了）
        → a 每日 → b 限时 → c 已完成待领 → d 不限时；字母按显示位置动态分配（谁排第一谁是 a），可直接当命令（输 a = 做整段）。
"""
import datetime as dt
import glob
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 项目根目录（本文件的上两级，与文件夹名无关）
os.chdir(ROOT)
sys.path.insert(0, HERE)

import task_common as tc              # noqa: E402
import task_runner as tr              # noqa: E402
import 中文结果 as zh                  # noqa: E402  结果翻译层（复用它的解析与排版）

PY = sys.executable                   # 当前解释器已由 .command 保证 ≥3.10
TZ = dt.timezone(dt.timedelta(hours=8))

# 积分余额端点（fork cmd/credit：POST /v2/billing/meter/get-user-resource）。
# 用来跑前/跑后对账真实入账增量——解析层对 school 域 claim / 抽奖等不带
# credit= 字段的入账看不到，余额差是唯一权威口径。
PATH_CREDIT = "/v2/billing/meter/get-user-resource"
_CREDIT_BODY = {
    "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
    "Status": [0, 3],
    # 时间窗：现在 → 101 年后，覆盖所有有效套餐
    "PackageEndTimeRangeBegin": None,   # 运行时填 time.strftime
    "PackageEndTimeRangeEnd": "2127-09-22 00:00:00",
}


def _credit_remain(auth):
    """读账号当前积分余额（remain）。失败返回 None，不影响主流程。

    响应 envelope：data.Response.Data.Accounts[].{Capacity*|CycleCapacity*}；
    Cycle 期套餐用 CycleCapacityRemain，否则用 CapacityRemain（与上游
    ResourceSummary 同口径）。
    """
    body = dict(_CREDIT_BODY)
    body["PackageEndTimeRangeBegin"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        st, r = tc.do_post(auth, tc.billing_base(auth), PATH_CREDIT, body)
    except Exception:
        return None
    if st != 200 or not isinstance(r, dict) or r.get("code") != 0:
        return None
    data = (r.get("data") or {}).get("Response") or {}
    data = data.get("Data") or {}
    remain = 0
    for a in data.get("Accounts") or []:
        if a.get("CycleCapacitySize"):
            remain += a.get("CycleCapacityRemain") or 0
        else:
            remain += a.get("CapacityRemain") or 0
    return remain


def _balances(auth):
    """读 (积分余额, 能量余额)；任一失败对应位给 None。两个只读 GET/POST 并发。"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fc = pool.submit(_credit_remain, auth)
        fe = pool.submit(_energy_balance, auth)
        return fc.result(), fe.result()


def _energy_balance(auth):
    """读能量余额（GET /v2/activity/growth/energy）。失败返回 None。"""
    try:
        st, r = tc.do_get(auth, tc.chat_base(auth), tr.PATH_ENERGY)
        if st == 200 and isinstance(r, dict) and r.get("code") == 0:
            return (r.get("data") or {}).get("balance")
    except Exception:
        pass
    return None


def _delta(before, after):
    """余额前后差；任一为 None 返回 0（无法对账时留给逐任务相加兜底）。"""
    if before is None or after is None:
        return 0
    try:
        return int(after) - int(before)
    except (TypeError, ValueError):
        return 0

W = 78                                # 整屏宽度
NAME_W = 30                           # 任务名一列的上限（显示宽度）
MIN_NAME_W = 12                       # 任务名一列的下限：再挤也不能比这窄，否则名字没法认
REWARD_W = 18                         # 奖励一列
PREFIX_W = 6                          # 行首「  1  」或空白占位

# 第一屏菜单里那几个「不是账号」的项，用内部标记传出去（不是真实账号）
NEW_ACCOUNT = "__LOGIN__"     # 登录新账号
ALL_FULL = "__ALL_FULL__"     # 所有账号 · 全量完成任务
ALL_DAILY = "__ALL_DAILY__"   # 所有账号 · 全量完成每日任务
RESCAN = "__RESCAN__"         # 重新扫描账号列表

# 脚本明确不做、或需要真人的任务：给一句准确的人话（键为官方 task_code）
MANUAL_NOTES = {
    "Expert_Philanthropy": "要真的捐一笔款（真人操作），脚本不代做",
    "task_student_verify": "要真人在微信里完成学生认证，脚本不代做",
    "wb_wechat_oa_subscribe_task": "官方后加的任务，脚本暂不支持；手机微信关注一下即可",
}

# 官方没标 recurring、但实际每天都能做一次的任务（每天限 1 次）
DAILY_EXTRA_CODES = {"black_cat"}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def width(s):
    """显示宽度：中日韩字符算 2 格。"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x1100 and (
            0x2E80 <= ord(ch) <= 0xA4CF
            or 0xAC00 <= ord(ch) <= 0xD7A3
            or 0xF900 <= ord(ch) <= 0xFAFF
            or 0xFE30 <= ord(ch) <= 0xFE6F
            or 0xFF00 <= ord(ch) <= 0xFF60
            or 0xFFE0 <= ord(ch) <= 0xFFE6
        ) else 1
    return w


def pad(s, w):
    return s + " " * max(0, w - width(s))


def cut(s, w):
    """按显示宽度截断（宁可少一字，不留半个汉字）。"""
    out, cur = "", 0
    for ch in s:
        cw = width(ch)
        if cur + cw > w:
            break
        out += ch
        cur += cw
    return out


def ask(prompt=""):
    """input 的安全版：管道/EOF 时不炸。"""
    try:
        return input(prompt)
    except EOFError:
        return "q"
    except KeyboardInterrupt:
        print()
        return "q"


def rel_time(ts):
    d = dt.datetime.now().timestamp() - ts
    if d < 90:
        return "刚刚登录"
    if d < 3600:
        return "%d 分钟前登录" % (d / 60)
    if d < 86400:
        return "%d 小时前登录" % (d / 3600)
    return "%d 天前登录" % (d / 86400)


def deadline_note(end):
    """把 valid_end 变成人话；没有截止时间返回 None。"""
    if not end:
        return None
    if isinstance(end, (int, float)):
        d = dt.datetime.fromtimestamp(end, TZ)
    else:
        try:
            d = dt.datetime.fromisoformat(str(end))
        except ValueError:
            return str(end)[:10]
    days = (d.date() - dt.datetime.now(TZ).date()).days
    # 文案越短越好 —— 它占的每一格都是从任务名那一列抢来的。
    # 统一成「11月13日·剩53天」：日期 + 间隔点 + 一句短话，全程无空格。
    if days < 0:
        return "%d月%d日·已过期" % (d.month, d.day)
    if days == 0:
        return "%d月%d日·今天截止" % (d.month, d.day)
    return "%d月%d日·剩%d天" % (d.month, d.day, days)


def deadline_days(end):
    """距离截止还有几天（负数 = 已过期，None = 没有截止时间）。

    只用来排序：清单里要按「最紧的先」排，光有文字描述排不了。
    """
    if not end:
        return None
    if isinstance(end, (int, float)):
        d = dt.datetime.fromtimestamp(end, TZ)
    else:
        try:
            d = dt.datetime.fromisoformat(str(end))
        except ValueError:
            return None
    return (d.date() - dt.datetime.now(TZ).date()).days


# --------------------------------------------------------------------------
# 任务清单
# --------------------------------------------------------------------------
def ability(code):
    """这个任务脚本能不能自动做？返回 (能不能, 不能的原因)。"""
    if code in FEAT_CODES:            # 功能型行（签到/旅行/抽奖/活跃地图）放行
        return True, ""
    if code in MANUAL_NOTES:
        return False, MANUAL_NOTES[code]
    spec = tr.MAPPING.get(code)
    if spec is None:
        return False, "脚本还没有支持这个任务，需要你在客户端手动做"
    if spec.get("unforgeable"):
        return False, "需要真人操作，脚本不代做"
    return True, ""


def _norm_state(ast, cur, target):
    ast = (ast or "").lower()
    if ast == "claimed":
        return "claimed"
    if ast == "completed":
        return "claimable"
    if ast in ("in_progress", "accepted"):
        return "doing"
    if target and cur >= target:
        return "claimable"
    return "todo"


def make_item(t, domain):
    code = t.get("task_code") or "?"
    if domain == "school":
        ast = t.get("status")
        cur = t.get("progress") or 0
        target = t.get("target_count") or 0
        credit = t.get("reward_credit") or 0
        energy = 0
        end = None
        ttype = t.get("task_type") or ""
    else:
        ast = t.get("accept_status")
        prog = t.get("progress") or {}
        cur = prog.get("current") or 0
        target = prog.get("target") or 0
        credit = t.get("reward_credit") or 0
        energy = t.get("reward_energy") or 0
        end = t.get("valid_end")
        ttype = t.get("task_type") or ""

    auto, why = ability(code)
    note = deadline_note(end)
    # 「每日可领」判据：官方 task_type=recurring（每天重置一次）；black_cat 官方描述
    # 写"每天 1 次，累计 3 天"，task_type 却是 single，单独补进来。
    daily = ttype == "recurring" or code in DAILY_EXTRA_CODES
    return {
        "code": code,
        "title": (t.get("title") or code).strip(),
        "domain": domain,
        "state": _norm_state(ast, cur, target),
        "cur": cur,
        "target": target,
        "credit": credit,
        "energy": energy,
        "deadline": note,
        "dl_days": deadline_days(end),
        "repeat": ttype == "recurring",
        "daily": daily,
        "auto": auto,
        "why": why,
        "raw_state": ast,
    }


# --------------------------------------------------------------------------
# 额外功能行（签到 / 猫猫旅行 / 开学季抽奖 / 活跃地图）
#
# 这些不是 task_runner 拉来的任务，是脚本自己加的「功能型」行，归进「a 每日」
# 组展示。code 用 feat_ 前缀，ability() 放行、exec_selected() 拦截调 _exec_feat，
# 与 task_runner 的 MAPPING 互不干扰。状态探查（_probe_*）只读，执行逻辑在
# _exec_checkin / _exec_travel / _exec_streak / _exec_school_lottery 四个函数里，
# 端点参照 linguo2625469/workbuddy2api-panel 实测口径。
# --------------------------------------------------------------------------
FEAT_CODES = ("feat_checkin", "feat_travel", "feat_school_lottery", "feat_streak")


def _probe_travel(auth, base):
    """GET /activity/growth/buddy/travel/status（只读）。返回 travel 状态 dict。"""
    try:
        st, r = tc.do_get(auth, base, "/activity/growth/buddy/travel/status")
        if st == 200 and isinstance(r, dict) and r.get("code") == 0:
            return (r.get("data") or {})
    except Exception:
        pass
    return {}


def _probe_streak(auth, base):
    """GET /activity/growth/streak（只读）。返回连登+档位状态 dict。"""
    try:
        st, r = tc.do_get(auth, base, "/activity/growth/streak")
        if st == 200 and isinstance(r, dict) and r.get("code") == 0:
            data = (r.get("data") or {})
            sk = data.get("streak") or {}
            rs = data.get("redemption_status") or {}
            return {
                "days": sk.get("days") or 0,
                "next_tier": sk.get("next_tier") or "",
                "next_tier_remaining": sk.get("next_tier_remaining"),
                "tiers_locked": all(
                    rs.get("tier_%s_status" % t) == "locked"
                    for t in ("7d", "14d", "28d")),
                "makeup_cards": (data.get("makeup_cards") or {}).get("balance") or 0,
            }
    except Exception:
        pass
    return {}


def _probe_lottery(auth):
    """GET /portal/activity/school/config（只读）。返回抽奖余额 dict。"""
    try:
        import school_open_day_2026 as sk
        cfg, chance = sk.fetch_lottery_config(auth["token"])
        return {
            "in_period": cfg.get("in_period"),
            "balance": chance.get("balance") or 0,
            "total_earned": chance.get("total_earned") or 0,
            "end_at": cfg.get("end_at") or "",
        }
    except Exception:
        pass
    return {}


def _travel_state_text(travel):
    """把 travel/status 的返回翻成清单状态文字。"""
    st = travel.get("state")
    limit = travel.get("daily_limit_reached")
    if st == "traveling":
        loc = (travel.get("location") or {}).get("name") or "外出中"
        arrive = travel.get("arrive_at") or 0
        if arrive:
            t = dt.datetime.fromtimestamp(arrive, TZ)
            return "旅行中·%s·%s到站" % (loc, t.strftime("%H:%M"))
        return "旅行中·%s" % loc
    if st == "idle" and limit:
        return "今日已派出"
    return "可派出"


def _feature_items(auth):
    """探4个额外功能的只读状态，返回 feature item 列表。

    每个 item 的 domain="feature"，code 用 feat_ 前缀，daily=True 归「a 每日」组。
    feat_state 是清单屏显示的状态文字（state_text 优先用它）。
    3 个端点并发探查（旅行/streak/lottery）；签到是幂等写，不探只读状态。
    """
    base = tc.chat_base(auth)
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_travel = pool.submit(_probe_travel, auth, base)
        f_streak = pool.submit(_probe_streak, auth, base)
        f_lottery = pool.submit(_probe_lottery, auth)
        travel = f_travel.result()
        streak = f_streak.result()
        lottery = f_lottery.result()

    items = []

    # 1) 每日签到（POST daily-checkin 幂等；只读无状态，执行时已签返回 10001）
    items.append({
        "code": "feat_checkin", "title": "每日签到", "domain": "feature",
        "state": "todo", "feat_state": "每日可签", "cur": 0, "target": 0,
        "credit": 100, "energy": 0, "deadline": "", "dl_days": None,
        "repeat": True, "daily": True, "auto": True, "why": "",
        "raw_state": "feature",
    })

    # 2) 猫猫旅行
    items.append({
        "code": "feat_travel", "title": "猫猫旅行", "domain": "feature",
        "state": "todo", "feat_state": _travel_state_text(travel),
        "cur": 0, "target": 0,
        "credit": travel.get("reward_credit") or 0, "energy": 0,
        "deadline": "", "dl_days": None, "repeat": True, "daily": True,
        "auto": True, "why": "", "raw_state": "feature",
    })

    # 3) 开学季抽奖
    bal = lottery.get("balance") or 0
    in_period = lottery.get("in_period")
    if not in_period:
        feat_state = "活动已结束"
        state = "claimed"
    elif bal > 0:
        feat_state = "抽奖余额 %d 次" % bal
        state = "todo"
    else:
        feat_state = "无抽奖机会"
        state = "claimed"
    items.append({
        "code": "feat_school_lottery", "title": "开学季小程序抽奖", "domain": "feature",
        "state": state, "feat_state": feat_state, "cur": bal, "target": 0,
        "credit": 0, "energy": 0, "deadline": "", "dl_days": None,
        "repeat": False, "daily": True, "auto": True, "why": "",
        "raw_state": "feature",
    })

    # 4) 活跃地图（连登天数 + 档位）
    s_days = streak.get("days") or 0
    s_next = streak.get("next_tier_remaining")
    s_tier = (streak.get("next_tier") or "").replace("d", "天")
    if s_days and s_next is not None:
        feat_state = "连登%d天·距%s档还差%d天" % (s_days, s_tier, s_next)
    elif s_days:
        feat_state = "连登%d天" % s_days
    else:
        feat_state = "未开始连登"
    items.append({
        "code": "feat_streak", "title": "活跃地图", "domain": "feature",
        "state": "todo", "feat_state": feat_state, "cur": s_days, "target": 0,
        "credit": 0, "energy": 0, "deadline": "", "dl_days": None,
        "repeat": True, "daily": True, "auto": True, "why": "",
        "raw_state": "feature",
    })

    return items


def collect(acc):
    """拉该账号的全部任务（只读，三个域合并）。返回 (items, energy_balance)。

    这四个接口互相不依赖，所以并发去要 —— 串行实测要 3.1 秒（1.3+1.3+0.3+0.2），
    并发只要最慢那个的时间，约 1.4 秒。并发前每个请求都是独立的 urllib 调用，
    不共享可变状态，所以是安全的。
    """
    auth = tc.load_auth(acc["prefix"])

    def _growth():
        # 这个域失败要往上抛：凭证过期就是靠它暴露的
        return list(tc.list_tasks(auth))

    def _mp():
        try:
            return list(tr._mp_list_tasks(auth))
        except Exception:
            return []

    def _school():
        try:
            tasks, in_period = tr.school_fetch_tasks(auth)
            return list(tasks) if in_period else []
        except Exception:
            return []

    def _energy():
        try:
            st, r = tc.do_get(auth, tc.chat_base(auth), tr.PATH_ENERGY)
            if st == 200 and isinstance(r, dict) and r.get("code") == 0:
                return (r.get("data") or {}).get("balance")
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_growth = pool.submit(_growth)
        f_mp = pool.submit(_mp)
        f_school = pool.submit(_school)
        f_energy = pool.submit(_energy)
        growth = f_growth.result()        # 抛异常时 with 会等其他线程收尾再抛
        mp = f_mp.result()
        sch = f_school.result()
        energy = f_energy.result()

    # 合并顺序固定（growth → mp → school），跟串行版完全一致，谁先回来都一样
    items, seen = [], set()
    for t in growth:
        items.append(make_item(t, "growth"))
        seen.add(t.get("task_code"))
    for t in mp:
        if t.get("task_code") not in seen:
            items.append(make_item(t, "mp"))
            seen.add(t.get("task_code"))
    for t in sch:
        if t.get("task_code") not in seen:
            items.append(make_item(t, "school"))
            seen.add(t.get("task_code"))

    return items + _feature_items(auth), energy


# --------------------------------------------------------------------------
# 分组：清单切成几段，每段有个字母代号，字母可以直接当命令用（输 a = 把 a 组全做掉）
# --------------------------------------------------------------------------
GROUP_ORDER = ["claimed", "manual", "done_daily", "a", "b", "c", "d"]

GROUP_NAMES = {
    "claimed": "已经领过的",
    "manual": "手动任务【脚本无法完成】",
    "done_daily": "已完成的每日任务",
    "a": "每日任务",
    "b": "限时任务",
    "c": "已完成待领取任务",
    "d": "不限时任务",
}

LETTERED = ("a", "b", "c", "d")      # 只有这四个能当命令用；claimed / manual / done_daily 只展示


def group_of(it):
    """这条任务归哪一组。

    claimed 已领过（不编号、选不了，只展示）
    manual  手动任务（脚本做不了，同样不编号、选不了，只展示）
    done_daily 已完成的每日任务（每天重置的那种，今天已领过，排 manual 之后）
    c 已完成待领（进度满了只差领奖，白捡的，优先于 a/b/d）
    a 每日 / b 限时 / d 不限时
    """
    if it["state"] == "claimed":
        return "done_daily" if it["daily"] else "claimed"
    if not it["auto"]:
        return "manual"
    if it["state"] == "claimable":
        return "c"
    if it["deadline"]:
        return "b"
    if it["daily"]:
        return "a"
    return "d"


def sort_key(it):
    """组顺序固定；组内「有截止的按最紧的先」，其余稳定按代码排。"""
    g = group_of(it)
    gi = GROUP_ORDER.index(g)
    if g in ("claimed", "done_daily"):
        return (gi, it["title"])
    dl = it["dl_days"] if it["dl_days"] is not None else 9999
    return (gi, dl, it["code"])


def reward_text(it):
    parts = []
    if it["credit"]:
        parts.append("+%d 积分" % it["credit"])
    if it["energy"]:
        parts.append("+%d 能量" % it["energy"])
    return " ".join(parts) if parts else "—"


def state_text(it):
    if it.get("feat_state"):          # 功能型行（签到/旅行/抽奖/活跃地图）
        return it["feat_state"]
    if it["state"] == "claimed":
        return "已领过"
    if not it["auto"]:
        return "做不了"
    # 夜猫子这种要累计多天的，直接说「已累计 1/3 天」，比「进行中 1/3」清楚
    if it["code"] in zh.CUMULATIVE_DAILY and it["target"]:
        return "已累计 %d/%d 天" % (it["cur"], it["target"])
    prog = " %d/%d" % (it["cur"], it["target"]) if it["target"] else ""
    base = {"todo": "未完成", "doing": "进行中", "claimable": "已达成待领"}.get(it["state"], "")
    return base + prog


def tail_text(it):
    """行尾附加信息：截止时间 / 每天可做。已领过的不再显示（没必要）。"""
    if it["state"] == "claimed":
        return ""
    parts = []
    if it["deadline"]:
        parts.append("[%s]" % it["deadline"])
    if it["daily"] and not it.get("code", "").startswith("feat_"):
        parts.append("每天可做")
    return "".join("　" + p for p in parts)


def build_line(prefix, it, name_w, with_tail=True):
    """拼一整行任务。name_w 是「任务名 + 点线」这一列的总宽（会自动截断换 …）。

    with_tail=False 时只拼到状态为止，行尾的截止时间 / 每天可做不拼
    （那种情况下它们会另起一行，见 render）。
    """
    name = it["title"]
    if width(name) > name_w:
        name = cut(name, max(1, name_w - 2)) + "…"
    line = "%s%s %s %s %s" % (
        prefix, name, "." * max(1, name_w - width(name)),
        pad(reward_text(it), REWARD_W), state_text(it))
    return line + (tail_text(it) if with_tail else "")


def pick_name_width(items):
    """自动检测：这批任务里，任务名一列最多能占多宽，才能让每行的状态、
    截止时间、每天可做都完整显示、且整行不超 W。

    从上限 NAME_W 往下试，取第一个「所有行都放得下」的值。
    ★ 找不到就返回下限 MIN_NAME_W —— 那说明有行连压到下限都放不下，
      交给 render 里的「规则3」（尾巴另起一行）兜底。
      下限存在的意义：宁可给尾巴换行，也不能把任务名压到认不出来。
    没有截止时间、没有「每天可做」时结果就是 NAME_W —— 跟以前完全一样。

    注意是「按组」调用（render 里每组各算一次），不是整份清单算一次：
    这样只有真带尾巴的那一组才让位，「已经领过的」那种无关的组不受牵连。
    """
    for c in range(NAME_W, MIN_NAME_W - 1, -1):
        if all(width(build_line(" " * PREFIX_W, it, c)) <= W for it in items):
            return c
    return MIN_NAME_W


def fit_no_tail(it, prefix, name_w):
    """规则3 用：把名字压到「不带尾巴那一行」刚好放得下，绝不低于 MIN_NAME_W。"""
    c = name_w
    while c > MIN_NAME_W and width(build_line(prefix, it, c, with_tail=False)) > W:
        c -= 1
    return c


def group_head(key, glist, letter_map=None):
    """组标题：a 每日任务　（3 个，共 +250 积分 +15 能量）

    letter_map 把内部 key 映射成显示字母（a/b/c/d 按位置分配，和组名无关）。
    """
    if key in ("claimed", "done_daily"):
        return GROUP_NAMES[key], "　（%d 个）" % len(glist)
    c = sum(it["credit"] for it in glist)
    e = sum(it["energy"] for it in glist)
    if c and e:
        sub = "　（%d 个，共 +%d 积分 +%d 能量）" % (len(glist), c, e)
    elif c:
        sub = "　（%d 个，共 +%d 积分）" % (len(glist), c)
    elif e:
        sub = "　（%d 个，共 +%d 能量）" % (len(glist), e)
    else:
        sub = "　（%d 个）" % len(glist)
    if key in LETTERED and letter_map:
        name = "%s %s" % (letter_map[key], GROUP_NAMES[key])
    elif key in LETTERED:
        name = "%s %s" % (key, GROUP_NAMES[key])
    else:
        name = GROUP_NAMES[key]
    return name, sub


def render(items, acc, energy, daily_only=False):
    """打印带编号的清单，返回 (index, groups, letter_map)。

    分组固定顺序：已经领过的 → 手动任务 → 已完成的每日任务（前三段只展示，不编号、选不了）
    → a 每日 → b 限时 → c 已完成待领 → d 不限时。字母按显示位置动态分配（谁排第一谁是 a），
    可以直接当命令用：输 a 就把 a 组全做掉。
    index       编号 → 条目（只有带字母的四段才有编号）
    groups      内部 key → 该组条目（供按组执行）
    letter_map  内部 key → 显示字母（如 {"a":"a","b":"b",...}，字母动态分配）
    """
    items = sorted(items, key=sort_key)

    buckets = {}
    for it in items:
        buckets.setdefault(group_of(it), []).append(it)

    todo = [it for it in items if it["auto"] and it["state"] != "claimed"]
    c_sum = sum(it["credit"] for it in todo)

    # 抬头两段：第一段是「谁」，第二段是「这号现在什么情况」。
    # 拼得下就一行，拼不下就自动折成两行（不再撑破 78 列）。
    seg1 = "  %s（uid %s）" % (acc["nick"] or "(无昵称)", acc["prefix"])
    if energy is not None:
        seg1 += "    能量余额 %s" % energy
    seg2 = ("每日任务 %d 个" % len(items)) if daily_only else ("共 %d 个任务" % len(items))
    if todo:
        seg2 += " · 还能领 %d 个" % len(todo)
        if c_sum:
            seg2 += "，约 +%d 积分" % c_sum
    joined = seg1 + "    " + seg2
    heads = [joined] if width(joined) <= W else [seg1, "    " + seg2]

    print()
    print("=" * W)
    for L in heads:
        print(L)
    if daily_only:
        print("  （每天都能领一次，建议每天跑一次这个入口）")
    print("=" * W)

    num = 0
    index = {}
    groups = {}
    # 字母按显示位置动态分配：谁排第一个谁就是 a，和组名无关
    letter_map = {}          # 内部 key → 显示字母
    for k in GROUP_ORDER:
        if k in LETTERED:
            letter_map[k] = "abcd"[len(letter_map)]
    prev_key = None
    for key in GROUP_ORDER:
        glist = buckets.get(key, [])
        if not glist:
            continue
        # 任务名一列多宽：每组各算一次（组内统一 → 列还是齐的）。
        # 按组而不是按整份清单算，是为了不让「已经领过的」这种无关的组
        # 跟着「限时任务」一起被压窄。
        name_w = pick_name_width(glist)
        if key in LETTERED:
            groups[key] = glist
        gname, sub = group_head(key, glist, letter_map)
        # 「手动任务」「已完成的每日任务」都紧贴上一组下面（不空行），其余每段前空一行
        if key not in ("manual", "done_daily"):
            print()
        prev_key = key
        # 可选的段（带字母 a/b/c/d）用 = 起头，醒目；只展示、选不了的段（已领过 / 手动任务）
        # 用 — 起头，弱化。靠横幅本身的醒目程度就能分出「能做的」和「只是给你看看的」。
        if key in LETTERED:
            print("  == %s%s %s"
                  % (gname, sub, "=" * max(2, W - 6 - width(gname) - width(sub))))
        else:
            print("  — %s%s %s"
                  % (gname, sub, "—" * max(2, W - 5 - width(gname) - width(sub))))
        for it in glist:
            if key in LETTERED:
                num += 1
                index[num] = it
                prefix = "  %2d  " % num
            else:
                prefix = " " * PREFIX_W   # 已领过 / 手动任务不编号（选不了）
            line = build_line(prefix, it, name_w)
            if width(line) <= W:
                print(line.rstrip())
            else:
                # 规则3：名字已经压到下限还是放不下 → 保住任务名，把尾巴挪到下一行。
                # 平时不会走到这里（规则1/2 就能解决），这是极端情况的安全网。
                c = fit_no_tail(it, prefix, name_w)
                print(build_line(prefix, it, c, with_tail=False).rstrip())
                print("      └ %s" % tail_text(it).strip("　").replace("　", "  "))
            if not it["auto"]:
                print("      └ 不自动做：%s" % it["why"])
    print()
    if daily_only and not todo:
        print("  今天的每日任务都已经领过了，明天再来。")
        print()
    print("-" * W)
    return index, groups, letter_map


# --------------------------------------------------------------------------
# 选择解析
# --------------------------------------------------------------------------
def parse_selection(s, index, groups, letter_map=None):
    """把 "1,3,5" / "1-5" / "a" / "a c" / "0" 解析成条目列表。

    返回 (items, 错误说明)。groups 是 render 给的「内部 key → 该组条目」。
    letter_map 是 render 给的「内部 key → 显示字母」反向映射后用来把
    用户输入的字母（a/b/c/d 按位置分配）翻译回内部 key。
    """
    s = s.strip().lower().replace("，", ",").replace("、", ",")
    # 反向映射：用户输入的显示字母（a/b/c/d 按位置分配）→ 内部 key
    if letter_map:
        rev = {v: k for k, v in letter_map.items()}
    else:
        rev = {k: k for k in LETTERED}
    avail = sorted(rev.keys())           # 用户可见的字母集，排序后 "abcd"

    if s == "0":
        picked = [index[i] for i in sorted(index) if index[i]["auto"]]
        if not picked:
            return None, "现在没有能替你做的任务"
        return picked, None

    picked, nums, letters = [], [], []
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        if len(part) == 1 and part in rev:
            letters.append(rev[part])         # 存内部 key
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            nums += list(range(a, b + 1))
            continue
        ls = "".join(avail)
        m = re.fullmatch(r"([%s])-([%s])" % (ls, ls), part)
        if m:
            i, j = avail.index(m.group(1)), avail.index(m.group(2))
            if i > j:
                i, j = j, i
            letters += [rev[c] for c in avail[i:j + 1]]
            continue
        if part.isdigit():
            nums.append(int(part))
            continue
        return None, ("看不懂「%s」—— 可以输编号（1 3 5）或范围（4-8），"
                      "整组（%s，也可写 a-b）、0（全部能做的）"
                      % (part, " ".join(avail)))

    for k in letters:
        glist = groups.get(k)
        if not glist:
            return None, "「%s」这一组现在没有任务" % k
        for it in glist:
            if it["auto"] and it not in picked:
                picked.append(it)

    for n in nums:
        if n not in index:
            return None, "没有编号 %d（有效范围 1-%d，或输 0 表示全部）" % (n, max(index))
        if index[n] not in picked:
            picked.append(index[n])
    if not picked:
        return None, "没有选中任何任务"
    return picked, None


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def _exec_school_lottery(auth):
    """真实执行开学季抽奖：查余额 → 循环抽到空 → 返回 entry。

    复用 school_open_day_2026 的 fetch_lottery_config / post_lottery_draw /
    lottery_prize_text，不重复实现抽奖逻辑。返回 entry 给 render_accounts 显示。
    """
    import school_open_day_2026 as sk
    import uuid

    try:
        cfg, chance = sk.fetch_lottery_config(auth["token"])
    except Exception as e:
        return {"state": "fail", "note": "抽奖查询失败：%s" % e,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    if not cfg.get("in_period"):
        return {"state": "already", "note": "活动已结束",
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    bal = chance.get("balance") or 0
    if bal <= 0:
        return {"state": "already", "note": "无抽奖机会",
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    results = []
    total_credit = 0
    prev_bal = bal
    stall = 0
    while bal > 0:
        draw_uuid = str(uuid.uuid4())
        try:
            st, r = sk.post_lottery_draw(auth["token"], draw_uuid)
        except Exception:
            break
        if st != 200 or (isinstance(r, dict) and r.get("code") not in sk.OK_CODES):
            code = (r or {}).get("code") if isinstance(r, dict) else None
            if code == 40900:          # no chance，正常边界
                bal = 0
                break
            break                       # 其他错误，停
        d = (r or {}).get("data") or {}
        prize_code = d.get("prize_code") or "?"
        credit = d.get("credit_amount") or 0
        bal = d.get("chance_balance", bal)
        if bal >= prev_bal:             # 余额未降：防死循环
            stall += 1
            if stall >= 3:
                break
        else:
            stall = 0
        prev_bal = bal
        pinfo = sk.LOTTERY_PRIZE_LABELS.get(prize_code, {})
        results.append({"label": pinfo.get("label", prize_code),
                        "type": pinfo.get("type", "unknown"),
                        "credit": credit})
        total_credit += credit
        if bal > 0:
            time.sleep(1.0)             # 间隔，防频控

    if results:
        vouchers = [r for r in results if r["type"] == "voucher"]
        # 主行 note：积分总和 + 兑换券计数（不管什么券，并入到兑换券N张）
        parts = []
        if total_credit > 0:
            parts.append("+%d积分" % total_credit)
        if vouchers:
            parts.append("兑换券×%d" % len(vouchers))
        note = "抽%d次：%s" % (len(results), "，".join(parts)) if parts else "抽%d次" % len(results)
        # detail 行：券名顿号分隔，后缀只在末尾显示一次
        if vouchers:
            names = "、".join(r["label"] for r in vouchers)
            detail = "中奖券：%s（前往小程序活动页领取）" % names
        else:
            detail = ""
    else:
        note = "抽了但没中奖"
        detail = ""
    # 余额归零=抽完(ok)；中途停=未抽完(pending)
    state = "ok" if bal == 0 else "pending"
    return {"state": state, "note": note, "credit": total_credit,
            "energy": 0, "cur": 0, "tgt": 0, "detail": detail}


# --------------------------------------------------------------------------
# 每日签到 / 猫猫旅行 / 连登管家 三个 feat_* 真实执行函数
#   端点路径与 body 形态参照 linguo2625469/workbuddy2api-panel 的
#   internal/upstream/streak.go + travel.go + blackcat.go 实测口径。
#   全部走上游已实现的端点，不复用 task_runner.py 的 --only 流程。
# --------------------------------------------------------------------------
PATH_DAILY_CHECKIN = "/v2/billing/meter/daily-checkin"      # 签到（billing 域，web）
PATH_TRAVEL_DEPART  = "/activity/growth/buddy/travel/depart"  # 派出（chat 域）
PATH_TRAVEL_CLAIM   = "/activity/growth/buddy/travel/claim"   # 领奖（chat 域）
PATH_STREAK_FULL    = "/activity/growth/streak"               # 连登完整状态（chat 域）
PATH_REDEEM         = "/activity/growth/redeem"               # 兑换档位（chat 域）
PATH_LOTTERY_SUMMARY = "/activity/growth/lottery/summary"     # 抽奖次数（chat 域）
PATH_LOTTERY_DRAW   = "/activity/growth/lottery/draw"         # 抽奖一次（chat 域）


def _exec_checkin(auth):
    """每日签到：POST /v2/billing/meter/daily-checkin（幂等；已签返回业务错误）。

    返回 entry：成功 ok +100积分；已签 already；其他 fail。
    """
    try:
        st, r = tc.do_post(auth, tc.billing_base(auth), PATH_DAILY_CHECKIN, {})
    except Exception as e:
        return {"state": "fail", "note": "签到请求失败：%s" % e,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    if st != 200 or not isinstance(r, dict):
        # 官方对"今天已签到"返回 HTTP 400 + body 含提示文字，不是 200
        hint = ""
        if isinstance(r, dict):
            hint = r.get("msg") or r.get("message") or r.get("raw") or str(r)[:60]
        else:
            hint = str(r)[:60]
        if "already" in hint.lower() or "已签" in hint or "明天再来" in hint:
            return {"state": "already", "note": hint,
                    "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
        return {"state": "fail", "note": "签到失败 %s" % hint,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    code = r.get("code")
    if code == 0:
        # 签到成功 +100 积分（固定奖励，从 data 里读更准但兜底 100）
        d = (r.get("data") or {})
        credit = d.get("credit") or 100
        return {"state": "ok", "note": "已签到 +100 积分",
                "credit": credit, "energy": 0, "cur": 0, "tgt": 0}
    # 已签等业务错误：上游 code 不固定（panel 用 IsAlreadyCheckin 判关键词）
    msg = (r.get("msg") or r.get("message") or "")[:80]
    if "already" in msg.lower() or "已签" in msg or "明天再来" in msg or code in (10001, 409):
        return {"state": "already", "note": msg or "今天已签到",
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    return {"state": "fail", "note": "签到失败 code=%s %s" % (code, msg),
            "credit": 0, "energy": 0, "cur": 0, "tgt": 0}


def _exec_travel(auth):
    """猫猫旅行状态机：GET status → 按状态调 depart 或 claim。

    idle + 未派出 → POST depart 派出（part「今日已派出」）
    idle + 已派出 → already「今日已领」（有 reward_credit 时尝试 claim 兜底）
    traveling   → part「旅行中·loc·HH:MM到站」（只读，等下次领奖）
    arrived      → POST claim with record_id → ok「+N 积分」
    """
    import uuid
    base = tc.chat_base(auth)
    try:
        st, r = tc.do_get(auth, base, "/activity/growth/buddy/travel/status")
    except Exception as e:
        return {"state": "fail", "note": "旅行查询失败：%s" % e,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    if st != 200 or not isinstance(r, dict) or r.get("code") != 0:
        return {"state": "fail", "note": "旅行查询失败：HTTP %d" % st,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    travel = (r.get("data") or {})
    state = travel.get("state") or "idle"
    limit = travel.get("daily_limit_reached")
    record_id = travel.get("record_id")
    reward = travel.get("reward_credit") or 0

    if state == "arrived" and record_id:
        # 到站领奖
        try:
            st2, r2 = tc.do_post(auth, base, PATH_TRAVEL_CLAIM,
                                 {"record_id": record_id})
        except Exception as e:
            return {"state": "fail", "note": "领奖失败：%s" % e,
                    "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
        if st2 == 200 and isinstance(r2, dict) and r2.get("code") == 0:
            got = ((r2.get("data") or {}).get("reward_credit")) or reward
            return {"state": "ok", "note": "领奖 +%d 积分" % got,
                    "credit": got, "energy": 0, "cur": 0, "tgt": 0}
        return {"state": "fail", "note": "领奖失败：HTTP %d" % st2,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    if state == "traveling":
        # 只读：旅行中（与清单 feat_state 共用文案）
        text = _travel_state_text(travel)
        return {"state": "part", "note": text,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    if state == "idle" and limit:
        # 今日已派出过、未到站或已领：算 already
        return {"state": "already", "note": "今日已派出",
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    if state == "idle":
        # 派出（location_id 1-4 都行，选 1）
        try:
            st3, r3 = tc.do_post(auth, base, PATH_TRAVEL_DEPART,
                                 {"location_id": 1})
        except Exception as e:
            return {"state": "fail", "note": "派出失败：%s" % e,
                    "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
        if st3 == 200 and isinstance(r3, dict) and r3.get("code") == 0:
            return {"state": "part", "note": "今日已派出，等待到站",
                    "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
        msg = (r3.get("msg") or r3.get("message") or "")[:80]
        if "limit" in msg.lower() or "已派出" in msg:
            return {"state": "already", "note": "今日已派出",
                    "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
        return {"state": "fail", "note": "派出失败：%s" % msg,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}

    # 兜底
    return {"state": "unknown", "note": "未知旅行状态：%s" % state,
            "credit": 0, "energy": 0, "cur": 0, "tgt": 0}


def _exec_streak(auth):
    """连登管家闭环：GET streak full → 遍历可兑换档位 POST redeem →
    GET lottery/summary 拿 chances → 循环 POST lottery/draw 抽完。

    返回 entry：note 主行汇总「兑换 N 档 +X 积分 抽 M 次 +Y 积分」；
    detail 行列兑换档位明细 + 抽奖奖品明细。
    """
    import json, uuid
    base = tc.chat_base(auth)

    # 1) 拉连登完整状态
    try:
        st, r = tc.do_get(auth, base, PATH_STREAK_FULL)
    except Exception as e:
        return {"state": "fail", "note": "连登查询失败：%s" % e,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    if st != 200 or not isinstance(r, dict) or r.get("code") != 0:
        return {"state": "fail", "note": "连登查询失败：HTTP %d" % st,
                "credit": 0, "energy": 0, "cur": 0, "tgt": 0}
    data = r.get("data") or {}
    rs = data.get("redemption_status") or {}
    # tier_7d_status / tier_14d_status / tier_28d_status + tiers[]
    tier_status = {
        "7d":  rs.get("tier_7d_status") or "locked",
        "14d": rs.get("tier_14d_status") or "locked",
        "28d": rs.get("tier_28d_status") or "locked",
    }
    tiers = rs.get("tiers") or []

    # 2) 遍历可兑换档位（非 locked / 非 claimed）
    redeemed = []        # [{tier, credit, energy, cards, chances}]
    total_credit_redeem = 0
    total_chances = 0
    for t in tiers:
        tier = t.get("tier") or ""
        status = tier_status.get(tier) or "locked"
        if status in ("locked", "claimed"):
            continue
        try:
            st2, r2 = tc.do_post(auth, base, PATH_REDEEM,
                                  {"tier": tier, "client_token": str(uuid.uuid4())})
        except Exception:
            continue
        if st2 == 200 and isinstance(r2, dict) and r2.get("code") == 0:
            entry = {
                "tier": tier, "credit": t.get("credit") or 0,
                "energy": t.get("energy") or 0,
                "cards": t.get("cards") or 0,
                "chances": t.get("chances") or 0,
            }
            redeemed.append(entry)
            total_credit_redeem += entry["credit"]
            total_chances += entry["chances"]
        # 403 = locked（上游状态可能跟 GET 时不同步），其他错误静默跳过

    # 3) 查抽奖次数（含刚兑换的）
    chances = 0
    try:
        st3, r3 = tc.do_get(auth, base, PATH_LOTTERY_SUMMARY)
        if st3 == 200 and isinstance(r3, dict) and r3.get("code") == 0:
            chances = (r3.get("data") or {}).get("chances") or 0
    except Exception:
        pass
    # 兑换累加的 chances 兜底（GET 失败时也能抽）
    chances = max(chances, total_chances)

    # 4) 抽奖循环
    draws = []
    total_credit_draw = 0
    stall = 0
    while chances > 0:
        try:
            st4, r4 = tc.do_post(auth, base, PATH_LOTTERY_DRAW,
                                  {"client_token": str(uuid.uuid4())})
        except Exception:
            break
        if st4 != 200 or not isinstance(r4, dict) or r4.get("code") != 0:
            # no chance / 限流 → 停
            break
        d = (r4.get("data") or {})
        credit = d.get("credit_amount") or d.get("credit") or 0
        prize_code = d.get("prize_code") or d.get("prize") or ""
        draws.append({"credit": credit, "prize_code": prize_code,
                      "raw": json.dumps(d, ensure_ascii=False)[:120]})
        total_credit_draw += credit
        chances -= 1
        stall += 1
        if stall > 50:        # 防死循环
            break
        if chances > 0:
            time.sleep(1.0)

    # 5) 组装返回
    total_credit = total_credit_redeem + total_credit_draw
    parts = []
    if redeemed:
        parts.append("兑换%d档 +%d积分" % (len(redeemed), total_credit_redeem))
    if draws:
        parts.append("抽%d次 +%d积分" % (len(draws), total_credit_draw))
    note = "、".join(parts) if parts else "无可兑换档位"
    # detail 行：档位 + 抽奖券（如果有）
    detail_parts = []
    if redeemed:
        detail_parts.append("兑换档位：%s" % "、".join(
            "%s(+%dc+%d券)" % (e["tier"], e["credit"], e["chances"]) for e in redeemed))
    if draws:
        vouchers = [d for d in draws if d["prize_code"] and "credit" not in (d["prize_code"] or "").lower()]
        if vouchers:
            detail_parts.append("中奖券：%s" % "、".join(d["prize_code"] for d in vouchers))
    detail = " └ ".join(detail_parts) if detail_parts else ""

    state = "ok" if (redeemed or draws) else "already"
    return {"state": state, "note": note, "credit": total_credit,
            "energy": 0, "cur": 0, "tgt": 0, "detail": detail}


def _exec_feat(acc, feat_items, parsed):
    """执行 feat_* 功能行。

    feat_checkin      → 每日签到（_exec_checkin，幂等）
    feat_travel       → 猫猫旅行状态机（_exec_travel：派出/领奖）
    feat_streak       → 连登兑换 + 抽奖闭环（_exec_streak）
    feat_school_lottery → 开学季小程序抽奖（_exec_school_lottery）
    """
    if parsed is None:
        accounts, unparsed, totals, mode = [], [], {}, ""
    else:
        accounts, unparsed, totals, mode = parsed

    uid8 = acc["prefix"]
    acc_entry = next((a for a in accounts if a["uid"].startswith(uid8)), None)
    if acc_entry is None:
        acc_entry = {"uid": acc.get("uid", uid8),
                     "nick": acc.get("nick", ""), "energy": None, "tasks": {}}
        accounts.append(acc_entry)

    auth = tc.load_auth(acc["prefix"])
    for it in feat_items:
        code = it["code"]
        if code == "feat_checkin":
            acc_entry["tasks"][code] = _exec_checkin(auth)
        elif code == "feat_travel":
            acc_entry["tasks"][code] = _exec_travel(auth)
        elif code == "feat_streak":
            acc_entry["tasks"][code] = _exec_streak(auth)
        elif code == "feat_school_lottery":
            acc_entry["tasks"][code] = _exec_school_lottery(auth)
        else:
            acc_entry["tasks"][code] = {
                "state": "skip", "note": "未实现",
                "credit": 0, "energy": 0,
                "cur": it.get("cur"), "tgt": it.get("target"),
            }

    return (accounts, unparsed, totals, mode)


def exec_selected(acc, picked):
    """跑一个账号挑中的任务。

    返回 (parsed, raw)：parsed = (accounts, unparsed, totals, mode)；执行异常时为 None。
    feat_* 功能行不走 task_runner，由 _exec_feat 执行（签到/旅行/连登/抽奖四项真实执行）。
    """
    feat_items = [it for it in picked if it["code"] in FEAT_CODES]
    real_items = [it for it in picked if it["code"] not in FEAT_CODES]

    raw = ""
    parsed = None
    if real_items:
        codes = [it["code"] for it in real_items]
        cmd = [PY, "-u", os.path.join(HERE, "task_runner.py"),
               acc["prefix"], "--yes", "--gap", "1.0"]
        for c in codes:
            cmd += ["--only", c]
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True,
                               text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return None, ""
        raw = (p.stdout or "") + (p.stderr or "")
        try:
            parsed = zh.parse_logs(raw)
        except Exception:
            parsed = None

    if feat_items:
        parsed = _exec_feat(acc, feat_items, parsed)

    return parsed, raw


def refine_daily(parsed_acc, picked):
    """修正「累计型每日任务」的结论。

    夜猫子这类任务是「每晚算 1 次、要累计 3 天」，上游脚本每次只补 1 次，
    日志里只会写一句「未达 target」——单看它分不清今天到底算没算数。
    拿执行前的进度一比就清楚了：
      进度涨了  → 今天这次算数了
      进度没涨  → 今天的份额之前已经算过，不用再管
    """
    for it in picked:
        if not it["daily"] or not it["target"] or it["target"] <= 1:
            continue
        entry = parsed_acc["tasks"].get(it["code"])
        # pending 也要放进来：进度没解析出来时状态会落到 pending，
        # 但夜猫子的「今天算没算数」照样得靠前后进度对比判断。
        if not entry or entry["state"] not in ("part", "skip", "pending"):
            continue
        after = entry.get("cur")
        if after is None:
            continue
        cap1 = it["code"] in zh.CUMULATIVE_DAILY      # 每天最多计入 1 次
        unit = "天" if cap1 else "次"
        if after > it["cur"]:
            entry["state"] = "part"
            entry["note"] = "今天已计入 %d/%d %s，还差 %d %s%s" % (
                after, it["target"], unit, it["target"] - after, unit,
                "（每天最多计入 1 次）" if cap1 else "")
        elif cap1:
            entry["state"] = "already"
            entry["note"] = "今天的份额之前已经计入过了（%d/%d %s）" % (
                after, it["target"], unit)
        else:
            entry["note"] = "已做到 %d/%d，还差 %d 次，下次继续" % (
                after, it["target"], it["target"] - after)


def _merge_totals(dst, src):
    """把多次 task_runner 的汇总行加起来（每个账号跑一次，各有一行 done）。"""
    for k in ("accounts", "total", "ok", "already", "skipped", "pending",
              "fail", "credit", "energy"):
        try:
            dst[k] = int(dst.get(k, 0)) + int(src.get(k, 0))
        except (TypeError, ValueError):
            dst.setdefault(k, src.get(k))


def run_all(accounts, daily_only=False):
    """所有账号一起跑：先只读扫一遍 → 逐个执行 → 先给总述、再给逐号明细。

    daily_only=True  → 只做「每天都能领一次」的那些（菜单里的 k）
    daily_only=False → 做这个号所有能做的（菜单里的 j，含每日任务）

    没有任何确认步骤，选中即开跑。
    """
    label = "每日任务" if daily_only else "任务"

    print()
    print("=" * W)
    print("  所有账号 · %s一键执行" % label)
    print("=" * W)
    print("  先看每个号有哪些%s（只读），再逐个执行。" % label)

    # ── 1) 只读扫描，确定每个号这次要跑哪些 ──
    plan = []
    for i, acc in enumerate(accounts, 1):
        name = acc["nick"] or acc["prefix"]
        print()
        print("  （%d/%d）查询「%s」的%s（只读）……"
              % (i, len(accounts), name, label))
        t0 = time.time()
        try:
            items, _ = collect(acc)
            pool = [it for it in items if it["daily"]] if daily_only else list(items)
            print("        用时 %.1f 秒。" % (time.time() - t0))
            if not pool:
                plan.append((acc, None,
                             "今天没有每日任务。" if daily_only else "没有读到任何任务。"))
            else:
                plan.append((acc, pool, None))
        except Exception as e:
            plan.append((acc, None, "查询失败：%s" % e))

    # ── 2) 逐个执行 ──
    blocks, notes, totals, results = [], {}, {}, []
    had_error = False
    # 余额对账：跑前/跑后真实余额差，覆盖解析层盲区（school claim/抽奖不带 credit= 字段）。
    # ledger_ok=False（一个号都没对上账）时回退到逐任务相加（grand_c）。
    ledger = {"credit": 0, "energy": 0}
    ledger_ok = False

    for i, (acc, pool, err) in enumerate(plan, 1):
        name = acc["nick"] or acc["prefix"]

        def _skip(blank, verdict, why):
            blocks.append({"uid": acc["prefix"], "nick": acc["nick"], "energy": None,
                           "tasks": {}, "blank": blank})
            notes[acc["prefix"]] = verdict
            results.append((name, False, verdict))

        def _blame_hint(msg):
            print()
            print("  ×「%s」%s" % (name, msg))
            print("    多半是登录过期 —— 回到上一屏按 n 重新登录即可。")

        if err:
            if err.endswith("。"):           # 「今天没有每日任务。」这类不是错误
                _skip(err, err.rstrip("。"), err.rstrip("。"))
            else:
                had_error = True
                _skip("没跑成：%s" % err, "本次没跑成", err)
                _blame_hint(err)
            continue

        runnable = [it for it in pool if it["auto"]]
        manual = [it for it in pool if not it["auto"]]
        print()
        print("  （%d/%d）执行「%s」的%s %d 个……"
              % (i, len(accounts), name, label, len(pool)))

        if not runnable:
            _skip("%s都要你手动做，脚本代不了。" % label,
                  "全都要手动做", "要手动做")
            continue

        # 跑前余额（积分+能量），跑后再读一次，差值=本轮真实入账
        try:
            auth = tc.load_auth(acc["prefix"])
        except Exception:
            auth = None
        before = _balances(auth) if auth else (None, None)

        parsed, raw = exec_selected(acc, runnable)
        if parsed is None or not parsed[0]:
            had_error = True
            _skip("没有拿到执行结果，请重试一次。", "本次没跑成", "没有拿到执行结果")
            if raw:
                print()
                print(raw)
            continue

        pacs, unparsed, tot, _mode = parsed
        pa = next((x for x in pacs if x["uid"].startswith(acc["prefix"])), pacs[0])
        if daily_only:
            refine_daily(pa, runnable)       # 夜猫子那类累计型的进度对比
        # school 域 claim 日志不带 credit= 字段，解析层拿不到入账数；用任务清单里的
        # reward_credit 把「本轮新完成」的 school 任务补上（余额对账是权威总数，
        # 这里只是逐任务明细的兜底，让单账号小结也能看到 +X 积分）。
        for it in runnable:
            e = pa["tasks"].get(it["code"])
            if not e or e.get("state") != "ok":
                continue
            if e.get("credit") or e.get("energy"):
                continue                      # 解析层已拿到真实入账值（growth claim）
            if it.get("credit") or it.get("energy"):
                e["credit"] = it["credit"]
                e["energy"] = it["energy"]
                e["note"] = "完成并入账"
        for it in manual:                    # 手动任务也列出来，免得看着像"全做完了"
            pa["tasks"].setdefault(it["code"], {
                "state": "blocked", "note": it["why"] or "要你手动做",
                "credit": 0, "energy": 0, "cur": None, "tgt": None})
        # 跑后余额 → 入账增量（只计正增量；负增量=消耗，不算入账）
        after = _balances(auth) if auth else (None, None)
        d_c = _delta(before[0], after[0])
        d_e = _delta(before[1], after[1])
        if before[0] is not None and after[0] is not None:
            ledger_ok = True
        if d_c > 0:
            ledger["credit"] += d_c
        if d_e > 0:
            ledger["energy"] += d_e
        if after[1] is not None:
            pa["energy"] = after[1]
        ok, text = zh.account_verdict(pa, "%s %d 个" % (label, len(pool)))
        pa["verdict"] = text
        blocks.append(pa)
        _merge_totals(totals, tot)
        results.append((name, bool(ok), text))

    # ── 3) 先给总述，再给每个号的明细 ──
    print()
    print()
    done = [r for r in results if r[1]]
    rest = [r for r in results if not r[1]]
    if results and not rest:
        headline = "全部 %d 个账号的%s都做到了" % (len(results), label)
    else:
        headline = "%d 个账号：整号做完 %d 个 · 还有没做完的 %d 个" % (
            len(results), len(done), len(rest))
        if rest:
            headline += "（%s）" % "、".join(r[0] for r in rest[:4])
    print(zh.render_accounts(blocks, [], totals, "", headline=headline,
                             notes=notes,
                             ledger=ledger if ledger_ok else None))
    return not had_error


def login_new_account():
    """在第一屏按 n 时调用：走一遍登录流程，把凭证落到 auths/。

    直接复用 login.py 里的 main()（它自己的 __main__ 守卫保证导入安全），
    所以授权链接、自动开浏览器、5 分钟倒计时轮询全都是现成的。
    """
    print()
    print("=" * W)
    print("  登录新账号")
    print("=" * W)
    try:
        import login as login_mod
    except Exception as e:
        print("  ✗ 加载登录模块失败：%s" % e)
        ask("  按回车键返回……")
        return False
    try:
        code = login_mod.main()
    except KeyboardInterrupt:
        print()
        print("  已取消登录。")
        ask("  按回车键返回……")
        return False
    if code == 0:
        print()
        print("  登录流程结束，正在刷新账号列表……")
    else:
        print()
        print("  这次没登录成功（不影响已有账号）。")
    return code == 0


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def list_accounts():
    out = []
    for p in sorted(glob.glob(os.path.join(tc.AUTHS, "workbuddy-*.json"))):
        try:
            a = tc.load_auth(os.path.basename(p))
        except BaseException:
            continue
        out.append({
            "uid": a["uid"],
            "prefix": a["uid"][:8],
            "nick": (a.get("nick") or "").strip(),
            "file": os.path.basename(p),
            "mtime": os.path.getmtime(p),
        })
    return out


def pick_account(accounts):
    """第一屏：账号列表 + 那几个「不是账号」的功能项。

    返回：某个账号 dict / 哨兵常量 / None（退出）。
    程序不退出时永远重新扫一遍账号目录，所以登录完的新账号会自动出现。
    """
    while True:
        print()
        print("=" * W)
        print("  请选择要操作的账号")
        print("  提示：分号分批领取，积分不易集中过期")
        print("=" * W)
        print()
        if accounts:
            for i, a in enumerate(accounts, 1):
                print("   %2d)  %s  uid %s    %s" % (
                    i, pad(a["nick"] or "(无昵称)", 16), a["prefix"], rel_time(a["mtime"])))
        else:
            print("   （还没有任何账号 —— 按 n 登录一个）")
        n = len(accounts)
        print()
        print("    n)  登录新账号")
        print("        开授权网页，回来自动检测（最多等 5 分钟）")
        print("    j)  所有账号 · 全量完成任务")
        print("    k)  所有账号 · 全量完成每日任务")
        if n:
            print("        （j / k 都是 %d 个号一次跑完）" % n)
        print()
        print("    r)  重新扫描账号（登录完会自动扫，一般用不到）")
        print("    q)  退出程序")
        print()
        s = ask("  输入：").strip().lower()

        if s in ("q", "quit", "exit"):
            return None
        if s == "n":
            return NEW_ACCOUNT
        if s == "j":
            return ALL_FULL
        if s == "k":
            return ALL_DAILY
        if s == "r":
            return RESCAN
        if s.isdigit() and 1 <= int(s) <= n:
            return accounts[int(s) - 1]

        print()
        if s == "0":
            print("  × 现在的菜单里没有 0。想一次跑完所有账号，用 j（全量）或 k（每日）。")
        elif s in ("a", "b", "c", "d"):
            print("  × 字母 a b c d 要先选一个账号才能用（它们是任务分组）。")
        elif not n:
            print("  × 还没有账号，按 n 登录一个吧。")
        else:
            print("  × 没看懂。请输入 1 ~ %d 选账号，或 n / j / k / r / q。" % n)


TITLE = "WorkBuddy 任务助手"


def banner():
    """程序启动时的标题横幅（只印一次）。"""
    print()
    print("=" * W)
    print("  " + TITLE)
    print("=" * W)


def task_hint():
    """任务清单正上方那两句说明 —— 只在真的要看清单时才印。"""
    print()
    print("  最上面两段只列给你看（已领过、手动任务）—— 脚本做不了，选不了、不占编号。")
    print("  下面四段带字母 —— 输字母＝把整段做掉；每段里最上面的是最该先做的。")


def main():
    banner()
    accounts = list_accounts()

    while True:
        acc = pick_account(accounts)
        if acc is None:
            print()
            print("  已退出。")
            return 0

        if acc is RESCAN:
            print()
            print("  正在重新扫描账号……")
            accounts = list_accounts()
            print("  %s" % ("发现 %d 个账号。" % len(accounts) if accounts
                            else "还是没扫到账号。按 n 登录一个吧。"))
            continue

        if acc is NEW_ACCOUNT:
            login_new_account()
            before = len(accounts)
            accounts = list_accounts()          # 自动检测：新账号直接进列表
            if len(accounts) > before:
                print()
                print("  ✅ 账号列表已自动更新，现在共 %d 个账号。" % len(accounts))
            continue

        if acc in (ALL_FULL, ALL_DAILY):
            if not accounts:
                print()
                print("  × 还没有账号可以跑。先按 n 登录一个。")
                continue
            run_all(accounts, daily_only=(acc is ALL_DAILY))
            print()
            ask("  按回车键回到账号选择……")
            continue

        items = None
        energy = None
        while True:
            if items is None:
                print()
                print("  正在查询「%s」的任务清单（只读）……"
                      % (acc["nick"] or acc["prefix"]))
                t0 = time.time()
                try:
                    items, energy = collect(acc)
                except Exception as e:
                    print()
                    print("  ✗ 查询失败：%s" % e)
                    print("    若反复失败，通常是登录过期 —— 回到上一屏按 n 重新登录即可。")
                    break
                if not items:
                    print()
                    print("  ✗ 没有读到任何任务。多半是登录凭证过期了。")
                    print("    回到上一屏按 n 重新登录后再试。")
                    break
                print("  查询完成，用时 %.1f 秒。" % (time.time() - t0))
                task_hint()

            index, groups, letter_map = render(items, acc, energy, False)
            print("  要做什么？")
            print("  *支持多选/混合输入(空格/顿号/逗号)")
            print("  执行全部可执行任务 : 0          换账号 : t          退出 : q")
            print()
            s = ask("  输入：").strip().lower()

            if s in ("q", "quit", "exit"):
                print()
                print("  已退出。")
                return 0
            if s in ("t", "tab"):
                break
            if not s:
                continue

            picked, err = parse_selection(s, index, groups, letter_map)
            if err:
                print()
                print("  × %s" % err)
                continue

            keys = {group_of(it) for it in picked}
            tail = ""
            if len(keys) == 1:
                g = keys.pop()
                if g in LETTERED:
                    disp = letter_map.get(g, g) if letter_map else g
                    tail = "（%s %s）" % (disp, GROUP_NAMES[g])
            print()
            if all(not it["auto"] for it in picked):
                print("  × 这 %d 个任务脚本都做不了，得你自己在客户端里做：" % len(picked))
                for it in picked:
                    print("    · %s" % it["title"])
                continue
            print("  开始执行这 %d 个任务%s：" % (len(picked), tail))
            for it in picked:
                tags = []
                if it["deadline"]:
                    tags.append(it["deadline"])
                if it["daily"]:
                    tags.append("每天可做")
                extra = "　[%s]" % " · ".join(tags) if tags else ""
                print("    · %s  %s%s" % (it["title"], reward_text(it), extra))
            if any(not it["auto"] for it in picked):
                print()
                print("  注意：其中有脚本做不了的任务，会如实跳过，不影响其他任务。")
            print()
            print("  正在执行，请稍候（每个任务几秒到十几秒，中途没有输出是正常的）……")
            try:
                auth = tc.load_auth(acc["prefix"])
            except Exception:
                auth = None
            before = _balances(auth) if auth else (None, None)
            parsed, raw = exec_selected(acc, picked)
            print()
            if parsed and parsed[0]:
                pacs, unparsed, tot, mode = parsed
                pa = next((x for x in pacs if x["uid"].startswith(acc["prefix"])), pacs[0])
                refine_daily(pa, picked)
                # school 域 claim 日志不带 credit=：用任务清单 reward_credit 兜底
                for it in picked:
                    e = pa["tasks"].get(it["code"])
                    if not e or e.get("state") != "ok":
                        continue
                    if e.get("credit") or e.get("energy"):
                        continue
                    if it.get("credit") or it.get("energy"):
                        e["credit"] = it["credit"]
                        e["energy"] = it["energy"]
                        e["note"] = "完成并入账"
                # 跑前/跑后余额对账真实入账
                after = _balances(auth) if auth else (None, None)
                d_c = _delta(before[0], after[0])
                d_e = _delta(before[1], after[1])
                ledger_ok = before[0] is not None and after[0] is not None
                ledger = ({"credit": d_c, "energy": d_e}
                          if ledger_ok else None)
                if after[1] is not None:
                    pa["energy"] = after[1]
                print(zh.render_accounts([pa], unparsed, tot, mode, ledger=ledger))
            else:
                print(raw or "  ✗ 没有拿到执行结果。")
            print()
            ask("  按回车键刷新清单……")
            items = None          # 执行过 → 下次强制重新查询

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("  已中断。")
        sys.exit(130)
