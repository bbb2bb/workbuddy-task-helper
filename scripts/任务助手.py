#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 成长计划任务自动执行脚本（整合版）

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
        分组固定顺序：已经领过的 → 手动任务（前两段只展示，不编号、选不了）
        → a 限时 → b 每日 → c 已完成待领 → d 不限时；字母可直接当命令（输 a = 做整段）。
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

    return items, energy


# --------------------------------------------------------------------------
# 分组：清单切成几段，每段有个字母代号，字母可以直接当命令用（输 a = 把 a 组全做掉）
# --------------------------------------------------------------------------
GROUP_ORDER = ["claimed", "manual", "a", "b", "c", "d"]

GROUP_NAMES = {
    "claimed": "已经领过的",
    "manual": "手动任务【脚本无法完成】",
    "a": "限时任务",
    "b": "每日任务",
    "c": "已完成待领取任务",
    "d": "不限时任务",
}

LETTERED = ("a", "b", "c", "d")      # 只有这四个能当命令用；claimed / manual 只展示


def group_of(it):
    """这条任务归哪一组。

    claimed 已领过（不编号、选不了，只展示）
    manual  手动任务（脚本做不了，同样不编号、选不了，只展示）
    c 已完成待领（进度满了只差领奖，白捡的，优先于 a/b/d）
    a 限时 / b 每日 / d 不限时
    """
    if it["state"] == "claimed":
        return "claimed"
    if not it["auto"]:
        return "manual"
    if it["state"] == "claimable":
        return "c"
    if it["deadline"]:
        return "a"
    if it["daily"]:
        return "b"
    return "d"


def sort_key(it):
    """组顺序固定；组内「有截止的按最紧的先」，其余稳定按代码排。"""
    g = group_of(it)
    gi = GROUP_ORDER.index(g)
    if g == "claimed":
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
    if it["daily"]:
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


def group_head(key, glist):
    """组标题：a 限时任务　（3 个，共 +250 积分 +15 能量）"""
    if key == "claimed":
        return "已经领过的", "　（%d 个）" % len(glist)
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
    name = "%s %s" % (key, GROUP_NAMES[key]) if key in LETTERED else GROUP_NAMES[key]
    return name, sub


def render(items, acc, energy, daily_only=False):
    """打印带编号的清单，返回 (index, groups)。

    分组固定顺序：已经领过的 → 手动任务（这两段只展示，不编号、选不了）
    → a 限时 → b 每日 → c 已完成待领 → d 不限时。字母可以直接当命令用：输 a 就把 a 组全做掉。
    index  编号 → 条目（只有带字母的四段才有编号）
    groups 字母 → 该组条目（供按组执行）
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
        gname, sub = group_head(key, glist)
        # 「手动任务」紧贴在「已经领过的」下面（中间不空行），其余每段前空一行
        if not (key == "manual" and prev_key == "claimed"):
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
    return index, groups


# --------------------------------------------------------------------------
# 选择解析
# --------------------------------------------------------------------------
def parse_selection(s, index, groups):
    """把 "1,3,5" / "1-5" / "a" / "a c" / "0" 解析成条目列表。

    返回 (items, 错误说明)。groups 是 render 给的「字母 → 该组条目」。
    """
    s = s.strip().lower().replace("，", ",").replace("、", ",")
    if s == "0":
        picked = [index[i] for i in sorted(index) if index[i]["auto"]]
        if not picked:
            return None, "现在没有能替你做的任务"
        return picked, None

    picked, nums, letters = [], [], []
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        if len(part) == 1 and part in LETTERED:
            letters.append(part)
            continue
        if part == "e":
            return None, ("「e」这个字母已经不用了 —— 手动任务（脚本做不了）"
                          "只列在清单最上面，要你自己在客户端里做。")
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            nums += list(range(a, b + 1))
            continue
        ls = "".join(LETTERED)
        m = re.fullmatch(r"([%s])-([%s])" % (ls, ls), part)
        if m:
            i, j = LETTERED.index(m.group(1)), LETTERED.index(m.group(2))
            if i > j:
                i, j = j, i
            letters += list(LETTERED[i:j + 1])
            continue
        if part.isdigit():
            nums.append(int(part))
            continue
        return None, ("看不懂「%s」—— 可以输编号（1 3 5）或范围（4-8），"
                      "整组（%s，也可写 a-b）、0（全部能做的）"
                      % (part, " ".join(LETTERED)))

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
def exec_selected(acc, picked):
    """跑一个账号挑中的任务。

    返回 (parsed, raw)：parsed = (accounts, unparsed, totals, mode)；执行异常时为 None。
    """
    codes = [it["code"] for it in picked]
    cmd = [PY, "-u", os.path.join(HERE, "task_runner.py"),
           acc["prefix"], "--yes", "--gap", "1.0"]
    for c in codes:
        cmd += ["--only", c]

    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return None, ""
    raw = (p.stdout or "") + (p.stderr or "")
    try:
        return zh.parse_logs(raw), raw
    except Exception:
        return None, raw


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
        for it in manual:                    # 手动任务也列出来，免得看着像"全做完了"
            pa["tasks"].setdefault(it["code"], {
                "state": "blocked", "note": it["why"] or "要你手动做",
                "credit": 0, "energy": 0, "cur": None, "tgt": None})
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
    print(zh.render_accounts(blocks, [], totals, "", headline=headline, notes=notes))
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


TITLE = "WorkBuddy 成长计划任务自动执行脚本"


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

            index, groups = render(items, acc, energy, False)
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

            picked, err = parse_selection(s, index, groups)
            if err:
                print()
                print("  × %s" % err)
                continue

            keys = {group_of(it) for it in picked}
            tail = ""
            if len(keys) == 1:
                g = keys.pop()
                if g in LETTERED:
                    tail = "（%s %s）" % (g, GROUP_NAMES[g])
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
            parsed, raw = exec_selected(acc, picked)
            print()
            if parsed and parsed[0]:
                pacs, unparsed, tot, mode = parsed
                pa = next((x for x in pacs if x["uid"].startswith(acc["prefix"])), pacs[0])
                refine_daily(pa, picked)
                print(zh.render_accounts([pa], unparsed, tot, mode))
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
