#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 task_runner.py 的英文日志翻译成人能看懂的中文清单。

设计原则：**不改上游 task_runner.py**（将来上游更新直接覆盖即可，翻译层照旧工作）。
本脚本只读它的输出、重新组织，输出一张「每个任务一行」的中文表格。

两种用法：
  1) 命令行管道（③一键完成全部任务 用）：
         python3 -u scripts/task_runner.py ALL --yes 2>&1 | python3 scripts/中文结果.py
  2) 被 任务助手.py 导入：
         parse_logs(raw)        -> 结构化结果
         render_accounts(...)   -> 自己排版（可加总述 headline 与每号小结论 notes）
"""
import re
import sys

# ---------------------------------------------------------------- 任务名对照
CODE_NAMES = {
    "first_buddy": "领养第一只 Buddy",
    "create_canvas": "创建设计画布",
    "chat_5": "对话活跃 5 次",
    "Model_chat_GLM5.2": "用 GLM-5.2 对话一次",
    "RichMeow_Chat": "桌面端对话体验",
    "Buddy_App": "Buddy 应用体验",
    "Buddy_App_QQ": "企鹅教师助手",
    "automation_1": "创建定时任务",
    "Library_read": "资料库阅读",
    "template_5": "使用模板 5 次",
    "playbook_prompt": "灵感案例做同款",
    "expert_5": "召唤专家 5 次",
    "Expert_team_use_3": "使用专家团 3 次",
    "Hp_Appearance": "切换主题皮肤",
    "Expert_lighthouse": "召唤轻量云专家",
    "skill_1": "使用技能",
    "black_cat": "夜猫子（深夜对话）",
    "Expert_Philanthropy": "公益捐赠",
    "chat_3_times": "开学季 · 对话 3 次",
    "expert_use": "开学季 · 召唤专家",
    "share_invite": "开学季 · 分享活动",
    "desktop_chat_1_time": "开学季 · 桌面端体验",
    "task_student_verify": "开学季 · 学生认证",
    "Sequential_Tasks_1": "小程序首次对话",
    "school_season": "校园日",
    # 官方后加、脚本暂不处理的任务（出现时按原名兜底）
    "wb_wechat_oa_subscribe_task": "关注微信服务号",
}

# 每天最多计入 1 次、要累计多天的任务 —— 进度单位是「天」不是「次」
# （官方描述：夜猫子每晚 1 次，累计 3 天。上游 light_up 对它 cap=1）
CUMULATIVE_DAILY = {"black_cat"}

# 状态优先级：数值大的最终胜出（一个任务会打多行，取最终结论）
STATE_RANK = {
    "ok": 100,       # 这次真的完成并领到了
    "part": 96,      # 进度涨了，但任务本身还没满（累计型任务）
    "partial": 95,   # 任务完成了，但领奖没成功
    "already": 90,   # 之前已经领过 / 今天的份额已经算过
    "fail": 80,
    "blocked": 70,   # 做不了（需要真人动作）
    "pending": 65,   # 做了动作但没生效（进度没动 / 没做满）：比「跳过」重，比「做不了」轻
    "skip": 60,
    "absent": 30,    # 这个号压根没有这个任务
    "unknown": 10,
}

# 状态 → 符号。**结果屏已经不用符号了**（2026-09-21 已定案：状态词就够了），
# 这里保留是为了一旦想退回符号版能直接取用；改 STATE_RANK 时仍然顺手同步一下。
MARK = {
    "ok": "✓",
    "part": "◐",
    "partial": "~",
    "already": "-",
    "skip": "·",
    "fail": "!",
    "blocked": "×",
    "pending": "○",
    "absent": "∅",
    "unknown": "?",
}

# 结果屏上每行显示的状态词（不用符号，因为后面本来就跟了说明）
# 键必须跟 STATE_RANK 完全一致 —— 加了新状态记得两边一起加。
STATE_WORD = {
    "ok": "已完成",
    "part": "进行中",
    "partial": "领奖失败",
    "already": "之前已领",
    "fail": "失败",
    "blocked": "做不了",
    "pending": "未完成",
    "skip": "跳过",
    "absent": "无此任务",
    "unknown": "读不到",
}

# 「今天该做的都做到了」——判定账号是否完成时，这些状态算通过
OK_STATES = ("ok", "part", "already", "absent")

RE_ACCOUNT = re.compile(r"^==\s*([0-9a-fA-F]{4,})\s*\(([^)]*)\)\s*==\s*$")
RE_TASK = re.compile(r"^\[task_runner\]\s+([0-9a-fA-F]{4,})\s+(\S+?):\s*(.*)$")
RE_ENERGY = re.compile(r"query energy balance=(\S+)")
RE_CREDIT = re.compile(r"credit=\+?(-?\d+)\s+energy=\+?(-?\d+)")
RE_DONE = re.compile(r"^task_runner done:\s*(.*)$")

RE_REREAD = re.compile(r"query re-read\s+(\d+)\s*/\s*(\d+)")
RE_PAREN = re.compile(r"[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]")
RE_ARROW = re.compile(r"->\s*(\d+)\s*/\s*(\d+)")
# 「{status} -> in_progress/3」这种：箭头后是状态名 + 斜杠 + 数字。
# 这是**动作跑完之后**回读到的真实进度，比前面括号里那个执行前的旧值权威。
RE_ARROW_STATE = re.compile(r"->\s*[a-z_]+\s*/\s*(\d+)")


def prog_of(rest):
    """从一行日志里抠出「当前/目标」进度；抠不到返回 None。

    顺序要紧。上游的日志形如：
        query accepted(0/5) -> in_progress/3（部分点亮，未达 target）
    前面括号里是**执行前**的旧进度，箭头后面才是**执行后**的新进度。
    所以必须先去认箭头 —— 否则那个 0/5 会把 3 挤掉（旧版正是踩了这个坑，
    结果「进行中 3/5」被降级显示成「跳过」）。
    """
    m = RE_ARROW.search(rest)               # -> 3/5，自带目标
    if m:
        return int(m.group(1)), int(m.group(2))
    m = RE_ARROW_STATE.search(rest)         # -> in_progress/3，目标去前面的括号借
    if m:
        m2 = RE_PAREN.search(rest)
        return int(m.group(1)), (int(m2.group(2)) if m2 else 0)
    for rx in (RE_REREAD, RE_PAREN):
        m = rx.search(rest)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def classify(rest, code=""):
    """把一行日志的正文归类成 (状态, 说明, 积分增量, 能量增量)。

    判定顺序即优先级，越靠前的语义越"终局"。
    """
    if "already_claimed" in rest:
        c, e = _cr(rest)
        return "already", "之前已经领过了", c, e
    if "claim 200 ok" in rest or "本轮已入账" in rest:
        c, e = _cr(rest)
        return "ok", "完成并已入账", c, e
    # 领养链路：buddy/first 直接发奖，后面跟的 claim 会是 already_claimed，
    # 必须在这里先判成 ok，否则这次新领的 +300 会被误标成"之前已领"。
    if "buddy/first" in rest and "credit=+" in rest:
        c, e = _cr(rest)
        return "ok", "领养成功并入账", c, e
    # "（点亮）" 必须整串匹配：另有 "（部分点亮，未达 target）" 形态属未完成。
    if "（点亮）" in rest:
        return "ok", "已完成", 0, 0
    if "不可伪造" in rest:
        return "blocked", "做不了（要真人操作或真实捐款）", 0, 0
    # 必须在下面 "失败" 之前：claim 失败发生在任务已点亮之后
    if "claim 失败" in rest:
        return "partial", "任务完成了，但领奖没成功，下次再试", 0, 0
    if "ERR" in rest or "失败" in rest:
        detail = _clean(rest)
        # 前缀要短：整行留给这条尾巴的空间只有 40 列，中文说明越长，右边英文原文越少。
        # 旧文案「领奖没成功（服务器返回错误）」占 26 列，把英语错误码挤成了「claim 500…」。
        note = "领奖失败" if "claim" in rest else "操作失败"
        if detail:
            # 英文原文照留（方便判断原因）；真放不下时由 fit 按显示宽度截断并补「…」，
            # 这样一眼能看出「被截了」，而不是以为服务器就返回这么短。
            note += "　%s" % fit(detail, 44)
        return "fail", note, 0, 0
    if "非夜猫窗口" in rest:
        return "skip", "不在深夜时段（23 点–次日 8 点），跳过", 0, 0
    if "dry-run" in rest or "dry_run" in rest or "dry run" in rest:
        return "skip", "本次是试运行，没有真的执行", 0, 0
    # 部分完成形态：进度涨了但没满（含 school 域的"部分点亮"）
    if "部分点亮" in rest or "未达 target" in rest or "未变化" in rest or "未完成" in rest:
        n = prog_of(rest)
        if n and n[1] and n[0] > 0:
            cur, tgt = n
            if code in CUMULATIVE_DAILY:
                # 不写「（每天最多计入 1 次）」——「已累计 x/y 天」+ 状态词「进行中」
                # 已经把这层意思说全了，留着只会撑破结果屏一行。
                return "part", "已累计 %d/%d 天，还差 %d 天" % (cur, tgt, tgt - cur), 0, 0
            return "part", "已做到 %d/%d，还差 %d 次，下次继续" % (cur, tgt, tgt - cur), 0, 0
        # 走到这里＝这一轮确实动手了，但进度没推上去（拿不到数字，或数字是 0）。
        # 别归到「跳过」——跳过是"不用管"，这里是"没做成"，语义正好相反。
        if "部分点亮" in rest:
            return "pending", "这次没做满，下次继续", 0, 0
        return "pending", "这次没做成功，下次再试", 0, 0
    if "已领，跳过" in rest or "已完成/已领" in rest or "claimed" in rest:
        return "already", "之前已经领过了", 0, 0
    if "任务不存在" in rest:
        return "absent", "这个号没有这个任务", 0, 0
    if "非映射任务" in rest or "未映射" in rest:
        return "skip", "不在自动完成范围内（官方新增任务，要手动做）", 0, 0
    if "不存在" in rest or "跳过" in rest or "skip" in rest:
        return "skip", "跳过（不需要处理）", 0, 0
    if "无需上报" in rest:
        return "skip", "不需要做", 0, 0
    return "unknown", _clean(rest), 0, 0


def _cr(rest):
    m = RE_CREDIT.search(rest)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def _clean(rest):
    """清洗技术日志残留：去掉行尾无意义的 ERR / 失败 标记与多余标点。"""
    # 先剥行尾的箭头 + ERR（顺序不能反：先 replace 会把箭头吃掉，正则就匹配不到了）
    s = re.sub(r"\s*(->|→|-{1,2}>?)\s*ERR\s*$", "", rest.strip())
    s = re.sub(r"\s*(->|→)\s+", " ", s).strip()
    s = s.strip(" :,，。；;")
    return s[:60] if s else ""


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


def clip(s, w):
    """按显示宽度截断（不补省略号，交给调用方决定）。"""
    out, acc = [], 0
    for ch in s:
        cw = width(ch)
        if acc + cw > w:
            break
        out.append(ch)
        acc += cw
    return "".join(out)


def fit(s, w):
    """截到 w 列以内；真被截了就补一个「…」。"""
    if width(s) <= w:
        return s
    return clip(s, max(1, w - 1)) + "…"


# ---------------------------------------------------------------- 解析
def parse_logs(raw):
    """把 task_runner.py 的输出解析成结构化结果。

    返回 (accounts, unparsed, totals, mode)：
      accounts  [{uid, nick, energy, tasks: {code: {state, note, credit, energy, cur, tgt}}}]
      unparsed  没归类的原始行
      totals    最后一行 task_runner done 的汇总
      mode      "REAL" / "DRY-RUN"
    """
    lines = raw.splitlines()

    accounts = []
    unparsed = []
    totals = {}
    mode = ""

    cur = None
    for line in lines:
        line = line.rstrip()

        if line.startswith("mode="):
            for kv in line.split():
                if kv.startswith("mode="):
                    mode = kv.split("=", 1)[1]
            continue

        m = RE_ACCOUNT.match(line)
        if m:
            cur = {"uid": m.group(1), "nick": m.group(2), "energy": None, "tasks": {}}
            accounts.append(cur)
            continue

        m = RE_DONE.search(line)
        if m:
            for kv in m.group(1).split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    totals[k] = v
            continue

        if cur is None:
            if line.strip() and "in_period" not in line and "非进行期" not in line:
                unparsed.append(line)
            continue

        m = RE_ENERGY.search(line)
        if m:
            cur["energy"] = m.group(1)
            continue

        m = RE_TASK.match(line)
        if m:
            uid8, code, rest = m.group(1), m.group(2), m.group(3)
            if code in ("query", "school", "school/tasks", "mp"):
                # 开学季活动期状态行：技术噪音，不展示给用户
                if "in_period" not in line and "非进行期" not in line:
                    unparsed.append(line)
                continue
            # 同一个 uid 的行归到同一个账号
            if not cur["uid"].startswith(uid8):
                tgt_acc = next((a for a in accounts if a["uid"].startswith(uid8)), None)
                if tgt_acc is None:
                    tgt_acc = {"uid": uid8, "nick": "", "energy": None, "tasks": {}}
                    accounts.append(tgt_acc)
                cur = tgt_acc

            old = cur["tasks"].get(code)

            # 进度回读行："xxx: query re-read 1/3 accept_status=in_progress"
            # 只记进度、不产生结论——否则这种技术行会被当成任务结果展示。
            if rest.startswith("query re-read"):
                n = prog_of(rest)
                if n:
                    if old is None:
                        old = {"state": "unknown", "note": "（只读到进度，没有结论）",
                               "credit": 0, "energy": 0, "cur": None, "tgt": None}
                        cur["tasks"][code] = old
                    old["cur"], old["tgt"] = n
                else:
                    unparsed.append(line)
                continue

            state, note, c, e = classify(rest, code)
            if old is None or STATE_RANK[state] >= STATE_RANK[old["state"]]:
                entry = {
                    "state": state,
                    "note": note,
                    "credit": c if state == "ok" else (old or {}).get("credit", 0),
                    "energy": e if state == "ok" else (old or {}).get("energy", 0),
                    "cur": (old or {}).get("cur"),
                    "tgt": (old or {}).get("tgt"),
                }
                n = prog_of(rest)
                if n:
                    entry["cur"], entry["tgt"] = n
                cur["tasks"][code] = entry
            continue

        # 兜底：无冒号的散行。开学季活动期状态行属技术噪音，不展示
        if line.strip() and "in_period" not in line and "非进行期" not in line:
            unparsed.append(line)

    return accounts, unparsed, totals, mode


def account_verdict(a, unit="个任务"):
    """一个账号的战果判定。返回 (是否全部做到, 一行文字)。"""
    tasks = a.get("tasks") or {}
    if not tasks:
        return None, ""
    bad = [c for c, t in tasks.items() if t["state"] not in OK_STATES]
    if not bad:
        return True, "%s · 全部完成" % unit
    names = "、".join(CODE_NAMES.get(c, c) for c in bad[:3])
    if len(bad) > 3:
        names += " 等"
    return False, "%s · 还有 %d 项没完成（%s）" % (unit, len(bad), names)


# ---------------------------------------------------------------- 渲染
ORDER = {"ok": 0, "part": 1, "partial": 2, "already": 3, "blocked": 4,
         "pending": 5, "skip": 6, "absent": 7, "fail": 8, "unknown": 9}

RES_W = 78                  # 结果屏宽度：跟清单屏（任务助手.py 的 W）保持一致


def render_accounts(accounts, unparsed=None, totals=None, mode="",
                    headline=None, notes=None):
    """把解析结果排成给人看的文字，返回字符串。

    headline  顶部总述（多账号汇总用），如「全部 3 个账号的每日任务都做到了」
    notes     {uid 前缀: 追加在账号标题下的一行小结论}

    版式：整屏跟清单屏一样按 RES_W(=78) 列排。每行 =
        缩进 + 任务名（不够补点）+ 状态词一列 + 说明
    说明太长会按剩余空间截断并补「…」，保证不折行。
    """
    unparsed = unparsed or []
    totals = totals or {}
    notes = notes or {}
    dry = mode == "DRY-RUN"

    out = []
    if headline:
        out.append("=" * RES_W)
        out.append("")
        out.append("  " + fit(headline, RES_W - 2))
        out.append("")
        out.append("=" * RES_W)
        out.append("")

    grand_c = grand_e = 0

    for a in accounts:
        title = "账号：%s" % (a["nick"] or a["uid"][:8])
        if a["nick"]:
            title += "（uid %s）" % a["uid"][:8]
        if a.get("energy"):
            title += "    能量余额 %s" % a["energy"]

        note = a.get("verdict") or notes.get(a["uid"][:8]) or ""
        out.append("=" * RES_W)
        out.append("  " + fit(title, RES_W - 2))
        if note:
            out.append("  " + fit(note, RES_W - 2))
        out.append("=" * RES_W)
        out.append("")

        tasks = a["tasks"]
        if not tasks:
            if a.get("blank"):
                out.append("  " + a["blank"])
            else:
                out.append("  （没有读到任何任务）")
                out.append("")
                out.append("  这通常说明登录凭证已经过期了。")
                out.append("  回到第一屏按 n 重新登录一次，再跑这个。")
            out.append("")
            continue

        items = sorted(tasks.items(), key=lambda kv: (ORDER.get(kv[1]["state"], 9), kv[0]))

        name_w = 26
        stat_w = 8                       # 「之前已领」「领奖失败」最宽，8 格够
        for code, t in items:
            name = CODE_NAMES.get(code, code)
            word = STATE_WORD.get(t["state"], "读不到")
            tail = t["note"]
            if t["state"] == "ok" and (t["credit"] or t["energy"]):
                tail = "+%d 积分" % t["credit"]
                if t["energy"]:
                    tail += " +%d 能量" % t["energy"]
                grand_c += t["credit"]
                grand_e += t["energy"]
            if not tail:
                tail = "（没有读到结论）"
            dots = "." * max(2, name_w - width(name))
            head = "  %s %s " % (name + dots, pad(word, stat_w))
            room = RES_W - width(head)
            if width(tail) > room:
                tail = clip(tail, max(4, room - 1)) + "…"
            out.append(head + tail)
        out.append("")

    out.append("-" * RES_W)
    if totals:
        if grand_c or grand_e:
            out.append("  这次一共领到：+%d 积分 +%d 能量" % (grand_c, grand_e))
        else:
            out.append("  这次没有新的积分入账。")
        out.append(
            "  任务统计：共 %s 个 · 新完成 %s · 之前已领 %s · 跳过 %s · 失败 %s"
            % (
                totals.get("total", "?"),
                totals.get("ok", "?"),
                totals.get("already", "?"),
                totals.get("skipped", "?"),
                totals.get("fail", "?"),
            )
        )
    else:
        out.append("  没读到汇总信息。")

    if dry:
        out.append("")
        out.append("  说明：这次是「试运行」——只是查看，上面的任务一个都没真的去做。")
        out.append("       要真的做，回到第一屏选一个账号，或按 j / k 批量跑。")

    if unparsed:
        out.append("")
        head = "  == 技术细节（看不懂可以忽略）"
        out.append(head + "=" * max(2, RES_W - width(head)))
        out.extend("    " + x for x in unparsed[:30])

    out.append("=" * RES_W)
    return "\n".join(out)


def main():
    raw = sys.stdin.read()
    accounts, unparsed, totals, mode = parse_logs(raw)

    if not accounts:
        out = ["没有读到任何账号信息。", "",
               "可能原因：",
               "  1) 还没有登录 —— 请先双击「①登录账号（双击我）.command」",
               "  2) 登录凭证已过期 —— 重新双击「①」即可", ""]
        if unparsed:
            out.append("原始输出：")
            out.extend("  " + x for x in unparsed[:20])
        print("\n".join(out))
        return 0

    print(render_accounts(accounts, unparsed, totals, mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
