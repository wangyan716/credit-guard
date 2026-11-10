#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""credit-guard / 积分守门 —— WorkBuddy 积分消耗审计

只读脚本：读取 ~/.workbuddy/workbuddy.db 的账单视图（session_usage.credit_json，
即应用自己记的积分），以及（可选）projects/*/*.jsonl 里的请求级 token 结构。

用法：
    python audit.py                  # 一屏报告（约 1 秒，只读 DB）
    python audit.py --deep           # 追加请求级成本结构（扫 jsonl，约 10 秒）
    python audit.py --days 30        # 只看最近 30 天
    python audit.py --json           # 机器可读
    python audit.py --out r.md       # 另存 markdown
    python audit.py --budget 50      # 今日消耗 > 50 则退出码 2

两套账本说明（别混用）：
  * 账单口径（DB credit_json）= 应用记的积分，用于「总量、哪一类、哪个会话」。
  * 结构口径（jsonl rawUsage）= 带 token 拆分，用于「为什么贵」，覆盖率约 40~100%，只做结构归因。
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import sqlite3
import statistics
import sys

WB = os.path.join(os.path.expanduser("~"), ".workbuddy")
DB = os.path.join(WB, "workbuddy.db")
AUTO_PAT = ("自动收集", "自动修复", "定时", "轮询", "巡检")


def db_ro(path):
    """只读打开，避免和正在运行的 App 抢写锁。"""
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)


def load_sessions(days=None):
    """账单口径：每个会话的积分 + 元信息。"""
    con = db_ro(DB)
    rows = con.execute(
        """select s.id, coalesce(s.custom_title,s.title,''), s.created_at,
                  coalesce(s.is_background_automation,0), s.cwd, u.credit_json
           from sessions s join session_usage u on u.session_id = s.id"""
    ).fetchall()
    con.close()
    now = dt.datetime.now()
    out = []
    for sid, title, ca, isauto, cwd, cj in rows:
        if not cj:
            continue
        try:
            credit = sum(float(v) for v in json.loads(cj).values())
        except Exception:
            continue
        if credit <= 0:
            continue
        when = dt.datetime.fromtimestamp(ca / 1000)
        if days and (now - when).days > days:
            continue
        auto = bool(isauto) or any(p in title for p in AUTO_PAT)
        out.append(dict(id=sid, title=title, when=when, credit=credit,
                        auto=auto, cwd=cwd or ""))
    return out


def automation_runs():
    """自动化运行记录：跑了多少次、多少次是空转。"""
    if not os.path.exists(DB):
        return None
    con = db_ro(DB)
    try:
        rows = con.execute(
            "select coalesce(thread_title,''), created_at, coalesce(failure_code,'') from automation_runs"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    zero_kw = ("无新", "无待处理", "静默结束", "本轮无", "total=0", "total\": 0")
    zero = sum(1 for t, _, _ in rows if any(k in t for k in zero_kw))
    return dict(total=len(rows), zero_output=zero,
                ratio=(zero / len(rows)) if rows else 0.0)


def load_requests(days=None):
    """结构口径：请求级 token / 积分。"""
    reqs, seen = [], set()
    limit_ts = (dt.datetime.now() - dt.timedelta(days=days)).timestamp() * 1000 if days else 0
    for f in glob.glob(os.path.join(WB, "projects", "*", "*.jsonl")):
        try:
            fh = open(f, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                pd = o.get("providerData") or {}
                ru = pd.get("rawUsage")
                if not ru:
                    continue
                rid = pd.get("conversationRequestId") or o.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                ts = o.get("timestamp") or 0
                if ts < limit_ts:
                    continue
                reqs.append(dict(
                    sid=o.get("sessionId") or "", ts=ts,
                    model=pd.get("requestModelName") or pd.get("model") or "?",
                    credit=float(ru.get("credit") or 0),
                    prompt=int(ru.get("prompt_tokens") or 0),
                    cache_read=int(ru.get("cache_read_input_tokens") or 0),
                    output=int(ru.get("completion_tokens") or 0),
                    reasoning=int((ru.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
                ))
    # 按会话时间排序得到「第几轮」，用于看上下文膨胀
    reqs.sort(key=lambda r: r["ts"])
    seq = collections.Counter()
    for r in reqs:
        seq[r["sid"]] += 1
        r["no"] = seq[r["sid"]]
    return reqs


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def build(days=None, deep=False):
    sess = load_sessions(days)
    if not sess:
        return dict(empty=True)
    total = sum(s["credit"] for s in sess)
    auto = [s for s in sess if s["auto"]]
    human = [s for s in sess if not s["auto"]]
    auto_credit = sum(s["credit"] for s in auto)
    runs = automation_runs()

    # 长会话：结构口径里请求数 > 100
    long_ids = set()
    reqs = load_requests(days) if deep else []
    if deep:
        cnt = collections.Counter(r["sid"] for r in reqs)
        long_ids = {k for k, v in cnt.items() if v > 100}

    cat = collections.Counter()
    for s in sess:
        cat["自动化(无人值守)"] += s["credit"] if s["auto"] else 0
        cat["超长会话(>100轮)"] += s["credit"] if (not s["auto"] and s["id"] in long_ids) else 0
        cat["有人参与·常规"] += s["credit"] if (not s["auto"] and s["id"] not in long_ids) else 0

    byday = collections.defaultdict(lambda: [0.0, 0.0])
    for s in sess:
        d = s["when"].strftime("%Y-%m-%d")
        byday[d][0] += s["credit"]
        if s["auto"]:
            byday[d][1] += s["credit"]
    days_sorted = sorted(byday.items())

    rep = dict(
        empty=False, total=total, n_sess=len(sess),
        first=min(s["when"] for s in sess), last=max(s["when"] for s in sess),
        auto=dict(n=len(auto), credit=auto_credit, pct=pct(auto_credit, total),
                  median=statistics.median([s["credit"] for s in auto]) if auto else 0),
        human=dict(n=len(human), credit=sum(s["credit"] for s in human),
                   pct=pct(sum(s["credit"] for s in human), total)),
        runs=runs, cat=dict(cat), byday=days_sorted,
        top=sorted(sess, key=lambda s: -s["credit"])[:10],
    )

    if deep and reqs:
        agg = collections.defaultdict(lambda: [0, 0, 0.0])
        for r in reqs:
            a = agg[r["model"]]
            a[0] += 1
            a[1] += r["prompt"]
            a[2] += r["credit"]
        firsts = [r for r in reqs if r["no"] == 1]
        big = [r for r in reqs if r["prompt"] > 120000]
        by_no = collections.defaultdict(list)
        for r in reqs:
            n = r["no"]
            k = "第1轮" if n == 1 else "2-5轮" if n <= 5 else "6-15轮" if n <= 15 \
                else "16-40轮" if n <= 40 else "41-100轮" if n <= 100 else "100轮以上"
            by_no[k].append(r)
        rep["deep"] = dict(
            n_req=len(reqs), total_credit=sum(r["credit"] for r in reqs),
            baseline_in=statistics.mean([r["prompt"] for r in firsts]) if firsts else 0,
            baseline_credit=statistics.mean([r["credit"] for r in firsts]) if firsts else 0,
            cache_read_pos=sum(1 for r in reqs if r["cache_read"] > 0),
            big_n=len(big), big_credit=sum(r["credit"] for r in big),
            models=[(k, v[0], v[1], v[2], (v[2] / v[1] * 1e6) if v[1] else 0)
                    for k, v in sorted(agg.items(), key=lambda kv: -kv[1][2]) if v[2] > 0],
            growth=[(k, len(by_no[k]), statistics.mean([r["prompt"] for r in by_no[k]]),
                     statistics.mean([r["credit"] for r in by_no[k]]))
                    for k in ("第1轮", "2-5轮", "6-15轮", "16-40轮", "41-100轮", "100轮以上")
                    if k in by_no],
        )
    return rep


def render(rep):
    if rep.get("empty"):
        return "没有找到积分记录（~/.workbuddy/workbuddy.db 无 session_usage 数据）。"
    L = []
    add = L.append
    add("=" * 62)
    add("积分消耗审计  |  %s ~ %s" % (rep["first"].strftime("%Y-%m-%d"),
                                    rep["last"].strftime("%Y-%m-%d")))
    add("=" * 62)
    add("总消耗 %s 积分 / %d 个有消费的会话" % (f"{rep['total']:,.0f}", rep["n_sess"]))
    add("")
    add("【哪一类最贵】")
    for k in ("自动化(无人值守)", "超长会话(>100轮)", "有人参与·常规"):
        v = rep["cat"].get(k, 0)
        if v:
            add("  %-16s %10s   占 %4.1f%%" % (k, f"{v:,.0f}", pct(v, rep["total"])))
    a = rep["auto"]
    add("  → 无人值守自动化：%d 次运行 / %s 积分（%.0f%%，中位 %.1f 积分/次）"
        % (a["n"], f"{a['credit']:,.0f}", a["pct"], a["median"]))
    r = rep["runs"]
    if r and r["total"]:
        add("  → 这些运行里 %d/%d 次（%.0f%%）产出为零" % (r["zero_output"], r["total"], r["ratio"] * 100))
    add("")
    add("【最贵的会话】")
    for s in rep["top"]:
        add("  %8s  %-22s %s%s" % (f"{s['credit']:,.0f}", (s["title"] or "(无标题)")[:22],
                                   s["when"].strftime("%m-%d"),
                                   "  [自动化]" if s["auto"] else ""))
    add("")
    add("【每日趋势（最近 14 天有消费的）】")
    tail = rep["byday"][-14:]
    peak = max([v[0] for _, v in tail] or [1])
    for d, (tot, au) in tail:
        bar = "#" * min(60, int(tot / max(1.0, peak) * 60))
        add("  %s %7.0f  %-60s 自动化占 %3.0f%%" % (d, tot, bar, pct(au, tot)))
    if "deep" in rep:
        d = rep["deep"]
        add("")
        add("【为什么贵（请求级结构，覆盖 %d 次请求 / %s 积分）】"
            % (d["n_req"], f"{d['total_credit']:,.0f}"))
        add("  会话基座：第 1 轮平均输入 %.0f token、%.2f 积分/会话" % (d["baseline_in"], d["baseline_credit"]))
        add("  大输入：>120k token 的请求 %d 次，占该样本 %.0f%% 积分" % (d["big_n"], pct(d["big_credit"], d["total_credit"])))
        add("  缓存命中：%d/%d 次请求读到缓存（0 = 每次全量重算）" % (d["cache_read_pos"], d["n_req"]))
        add("  %-20s %6s %14s %10s %14s" % ("模型", "请求", "输入token", "积分", "积分/百万输入"))
        for k, n, p, c, rate in d["models"][:8]:
            add("  %-20s %6d %14s %10s %14.0f" % (k[:20], n, f"{p:,}", f"{c:,.0f}", rate))
        add("  上下文随轮次膨胀：")
        for k, n, pi, cr in d["growth"]:
            add("    %-9s n=%4d  平均输入 %8.0f tok  平均 %6.2f 积分" % (k, n, pi, cr))
    add("")
    add("【按策略该做什么】见 references/policy.md")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--budget", type=float, default=None, help="今日预算；超出则退出码 2")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("找不到 %s" % DB)
        return 1
    rep = build(args.days, args.deep)

    if args.budget is not None and not rep.get("empty"):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        used = dict(rep["byday"]).get(today, [0.0, 0.0])[0]
        print("今日已用 %.1f 积分 / 预算 %.0f" % (used, args.budget))
        if used > args.budget:
            print("⚠ 超出今日预算 %.1f 积分" % (used - args.budget))
            return 2
        return 0

    text = json.dumps(rep, ensure_ascii=False, indent=1, default=str) if args.json else render(rep)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("\n已写入 %s" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
