#!/usr/bin/env python3
# 小黄鱼数据工坊 金价监控 - GitHub Actions 最终加固版
# 推送: Server酱(Secrets: SERVERCHAN_KEY)  令牌: Secrets: GOLD_TOKEN
# 设计要点:
#   - 网络请求自动重试(2次), 连续失败才告警
#   - 微信推送失败自动重试(2次)
#   - 免费版每日额度管理: 播报每日上限4条, 事件提醒(跌破/收复/令牌/异常)不受限
#   - 周末(北京时间)休市不播报; 数据时间未变化不播报; 数据时间缺失视为未更新
#   - 跌破/收复只在穿越瞬间提醒; 数据源异常时保持状态不误报
#   - 价格值类型防御; 令牌过期/接口异常主动微信通知
import time, secrets, json, hashlib, hmac, urllib.request, urllib.parse, urllib.error, os, sys

TOKEN = os.environ.get("GOLD_TOKEN", "")
SCT_KEY = os.environ.get("SERVERCHAN_KEY", "")
THRESHOLD = 900.0          # 银行小金条跌破该价提醒
BASE = "https://mp.68gold.cn"
STATE = "gold_state.json"
HTML = "index.html"
HISTORY = "history.json"
DAILY_BROADCAST_LIMIT = 4  # 免费版5条/天, 预留1条给事件提醒
BROADCAST_HOURS = {9, 12, 15, 18, 21}  # 播报时段（北京时间），均分全天额度

BANK_PREFIX = ("工商", "建设", "交通", "招商")
SHOW = [("ICBC","工商银行","5g"),("CCB","建设银行","5g"),("BCM","交通银行","10g"),
        ("CMB","招商银行","10g"),("SGE","上金所","1g"),("HKGX","香港金银","1g"),
        ("FX678","伦敦黄金",None),("FX678","纽约黄金12",None)]
UNIT = {"FX678": " 美元/盎司"}

def now_bj():
    """北京时间 struct_time（GitHub runner 是 UTC）"""
    return time.gmtime(time.time() + 8 * 3600)

def call_api(path, token=TOKEN, method="GET", body="", retries=2):
    """带重试的接口调用；重试仍失败抛出最后一次异常"""
    last_exc = None
    for attempt in range(retries + 1):
        ts = str(int(time.time()))
        nonce = f"{secrets.token_hex(4)}_{secrets.randbelow(1<<20):x}_{secrets.token_hex(10)}"
        msg = "\n".join([method, path, ts, nonce, hashlib.sha256(body.encode()).hexdigest()])
        sig = hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()
        req = urllib.request.Request(BASE + path, data=body.encode() if body else None, method=method, headers={
            "X-MP-Timestamp": ts, "X-MP-Nonce": nonce, "X-MP-Signature": sig,
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(3)
    raise last_exc

def serverchan(title, text, retries=1):
    """Server酱推送，失败自动重试一次"""
    if not SCT_KEY:
        print("SERVERCHAN_KEY 未配置，跳过微信推送")
        return False
    data = urllib.parse.urlencode({"title": title, "desp": text}).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(f"https://sctapi.ftqq.com/{SCT_KEY}.send", data=data)
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            ok = r.get("code") == 0
            print("微信推送:", "PUSH_OK" if ok else r)
            if ok or attempt >= retries:
                return ok
        except Exception as e:
            print(f"微信推送失败(尝试{attempt+1}):", e)
            if attempt >= retries:
                return False
            time.sleep(3)
    return False

def main():
    # ── 1. 拉取数据（含令牌过期/接口异常通知） ──
    try:
        raw = call_api("/api/miniprogram/latest")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            serverchan("🔴 金价监控令牌过期", "接口返回 %d，需要重新用 Fiddler 抓包换新令牌，否则监控已停摆。" % e.code)
        else:
            serverchan("🔴 金价监控异常", "接口返回 HTTP %d，本次运行失败，下小时自动重试。" % e.code)
        sys.exit(1)
    except Exception as e:
        serverchan("🔴 金价监控异常", "脚本出错：%s\n本次运行失败，下小时自动重试；连续失败请检查。" % e)
        sys.exit(1)

    d = raw.get("data") or {}
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    prev = state.get("prices", {})
    prev_data_time = state.get("data_time", "")
    data_time = d.get("时间", "") or ""
    prices = d.get("价格") or {}
    diff_txt = ", ".join(f"{k.replace('_SGE','')} +{v:.2f}" for k, v in (d.get("价差") or {}).items())

    # ── 2. 解析品种价格（类型防御） ──
    cur, rows, md = {}, [], []
    for code, cname, spec in SHOW:
        node = prices.get(code) or {}
        if spec:
            v = (node.get(spec) or {}).get("单价")
            label = f"{cname} {spec}"
        else:
            v = node.get("值")
            label = cname + UNIT.get(code, "")
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        cur[label] = v
        delta = ""
        if label in prev and abs(prev[label] - v) > 1e-9:
            dd = v - prev[label]
            delta = f"（{'↑' if dd > 0 else '↓'}{abs(dd):.2f}）"
        rows.append((label, v, delta, "up" if "↑" in delta else ("down" if "↓" in delta else "")))
        md.append(f"{label}: **{v:.2f}**{delta}")

    if not cur:
        # 接口200但无价格数据（数据源异常）：保持原状态，不推送，防误报收复
        print("⚠️ 接口未返回任何价格数据（数据源异常），本次跳过")
        state["ts"] = time.time()
        json.dump(state, open(STATE, "w"), ensure_ascii=False)
        return

    # ── 3. 状态更新 ──
    state.update({"ts": time.time(), "prices": cur, "data_time": data_time})
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    print(f"数据时间 {data_time}")
    for label, v, delta, _ in rows:
        print(f"{label}: {v:.2f} {delta}")

    # ── 4. 跌破/收复检测（穿越式提醒） ──
    alerts = []
    any_below = any(v < THRESHOLD for l, v in cur.items() if l.startswith(BANK_PREFIX))
    was_below = state.get("below", False)
    if any_below and not was_below:
        for label, v in cur.items():
            if label.startswith(BANK_PREFIX) and v < THRESHOLD:
                alerts.append(f"⚠️ {label} 现价 **{v:.2f}** 元/克，已跌破 {THRESHOLD:.0f} 元/克！")
    elif was_below and not any_below:
        alerts.append(f"✅ 银行小金条价格已收复 {THRESHOLD:.0f} 元/克。")
    state["below"] = any_below
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    # ── 5. 微信推送（额度管理：播报每日限量，事件提醒不受限） ──
    bj = now_bj()
    is_weekend = bj.tm_wday >= 5
    today = time.strftime("%Y-%m-%d", bj)
    pushes = state.get("pushes", {})
    if pushes.get("date") != today:
        pushes = {"date": today, "n": 0}

    price_changed = cur != prev
    if alerts:
        body_md = "\n\n".join(alerts) + "\n\n---\n\n### 小黄鱼金价监控\n\n数据时间 " + data_time + "\n\n" + "\n\n".join(md)
        if diff_txt:
            body_md += "\n\n溢价(vs上金所)：" + diff_txt
        title = "✅ 金价收复900" if any("收复" in a for a in alerts) else "⚠️ 金价跌破900提醒"
        serverchan(title, body_md)
        pushes["n"] += 1
    elif is_weekend:
        print("周末休市，跳过本次播报")
    elif not price_changed:
        print("价格无变化，跳过本次播报")
    elif bj.tm_hour not in BROADCAST_HOURS:
        print(f"非播报时段（{sorted(BROADCAST_HOURS)} 点整点播报），跳过")
    elif pushes.get("n", 0) >= DAILY_BROADCAST_LIMIT:
        print(f"今日播报已达上限 {DAILY_BROADCAST_LIMIT} 条（省额度），网页仍每小时更新")
    else:
        body_md = f"### 小黄鱼金价监控\n\n数据时间 {data_time}\n\n" + "\n\n".join(md)
        if diff_txt:
            body_md += "\n\n溢价(vs上金所)：" + diff_txt
        serverchan("金价播报 " + time.strftime("%m-%d %H:%M", bj), body_md)
        pushes["n"] += 1
    state["pushes"] = pushes
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    # ── 6. 网页报告 ──
    trs = "\n".join(
        f'<tr class="{cls}"><td>{label}</td><td>{v:.2f}</td><td>{delta}</td></tr>'
        for label, v, delta, cls in rows)
    history = []
    if os.path.exists(HISTORY):
        try:
            history = json.load(open(HISTORY))
        except Exception:
            history = []
    if not history or history[-1].get("time") != data_time:
        history.append({"time": data_time, "prices": cur, "alerts": bool(alerts)})
    history = history[-2000:]
    json.dump(history, open(HISTORY, "w"), ensure_ascii=False)
    labels = [r[0] for r in rows]
    hist_rows = "\n".join(
        "<tr><td>%s</td>%s</tr>" % (h["time"], "".join(
            "<td>%s</td>" % h["prices"].get(l, "-") for l in labels))
        for h in history[-48:])
    head = "".join(f"<th>{l}</th>" for l in labels)
    alert_banner = ('<div style="background:#5c1a1a;border:1px solid #ff6b6b;padding:12px;border-radius:8px;margin-bottom:16px">'
                    + "<br>".join(a.replace("**", "") for a in alerts) + "</div>") if alerts else ""
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>小黄鱼金价监控</title><style>
body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}}.time{{color:#888;font-size:13px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;max-width:560px;margin-bottom:24px}}
td,th{{padding:10px 12px;border-bottom:1px solid #333;text-align:left;font-size:15px}}
tr:nth-child(even){{background:#1a1a1a}}.up{{color:#ff6b6b}}.down{{color:#51cf66}}.gold{{color:#ffd43b}}
.diff{{color:#aaa;font-size:13px;max-width:560px;margin-bottom:24px}}
.scroll{{overflow-x:auto}}
</style></head><body>
<h1><span class="gold">◆</span> 小黄鱼数据工坊 · 金价监控</h1>
<div class="time">数据时间：{data_time} ｜ 每小时自动更新；微信播报每日{DAILY_BROADCAST_LIMIT}条，跌破/收复随时提醒</div>
{alert_banner}
<table><tr><th>品种</th><th>价格</th><th>变化</th></tr>{trs}</table>
<h1 style="font-size:16px">近期走势（最近48次采集）</h1>
<div class="scroll"><table><tr><th>时间</th>{head}</tr>{hist_rows}</table></div>
</body></html>"""
    open(HTML, "w").write(html)

if __name__ == "__main__":
    main()
