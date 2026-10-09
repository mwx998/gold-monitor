#!/usr/bin/env python3
# 小黄鱼数据工坊 金价监控 - GitHub Actions 版
# 微信推送: Server酱 (Secrets: SERVERCHAN_KEY)  令牌: Secrets: GOLD_TOKEN
import time, secrets, json, hashlib, hmac, urllib.request, urllib.parse, urllib.error, os, sys

TOKEN = os.environ.get("GOLD_TOKEN", "")
SCT_KEY = os.environ.get("SERVERCHAN_KEY", "")
THRESHOLD = 900.0  # 上金所跌破该价提醒
BASE = "https://mp.68gold.cn"
STATE = "gold_state.json"
HTML = "index.html"

def call_api(path, token=TOKEN, method="GET", body=""):
    ts = str(int(time.time()))
    nonce = f"{secrets.token_hex(4)}_{secrets.randbelow(1<<20):x}_{secrets.token_hex(10)}"
    msg = "\n".join([method, path, ts, nonce, hashlib.sha256(body.encode()).hexdigest()])
    sig = hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(BASE + path, data=body.encode() if body else None, method=method, headers={
        "X-MP-Timestamp": ts, "X-MP-Nonce": nonce, "X-MP-Signature": sig,
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

def serverchan(title, text):
    if not SCT_KEY:
        print("SERVERCHAN_KEY 未配置，跳过微信推送")
        return False
    data = urllib.parse.urlencode({"title": title, "desp": text}).encode()
    req = urllib.request.Request(f"https://sctapi.ftqq.com/{SCT_KEY}.send", data=data)
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        ok = r.get("code") == 0
        print("微信推送:", "PUSH_OK" if ok else r)
        return ok
    except Exception as e:
        print("微信推送失败:", e)
        return False

SHOW = [("ICBC","工商银行","5g"),("CCB","建设银行","5g"),("BCM","交通银行","10g"),
        ("CMB","招商银行","10g"),("SGE","上金所","1g"),("HKGX","香港金银","1g"),
        ("FX678","伦敦黄金",None),("FX678","纽约黄金12",None)]

def main():
    # 令牌过期检测：拉不到数据时用Server酱提醒换令牌（不占用金价播报额度以外太多）
    try:
        raw = call_api("/api/miniprogram/latest")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            serverchan("🔴 金价监控令牌过期", "接口返回 %d，需要重新用 Fiddler 抓包换新令牌，否则监控已停摆。" % e.code)
            sys.exit(1)
        raise
    d = raw["data"]
    prev = {}
    state = {}
    if os.path.exists(STATE):
        try: state = json.load(open(STATE))
        except Exception: pass
    prev = state.get("prices", {})
    data_time = d.get("时间", "")
    prices = d.get("价格", {})
    cur, rows, md = {}, [], []
    for code, cname, spec in SHOW:
        node = prices.get(code, {})
        if spec:
            v = node.get(spec, {}).get("单价"); label = f"{cname} {spec}"
        else:
            v = node.get("值"); label = cname
        if v is None: continue
        cur[label] = v
        delta = ""
        if label in prev and abs(prev[label]-v) > 1e-9:
            dd = v - prev[label]
            arrow = "↑" if dd > 0 else "↓"
            delta = f"（{arrow}{abs(dd):.2f}）"
        rows.append((label, v, delta, "up" if "↑" in delta else ("down" if "↓" in delta else "")))
        md.append(f"{label}: **{v:.2f}** 元/克{delta}")
    diff = d.get("价差", {})
    diff_txt = ", ".join(f"{k.replace('_SGE','')} +{v:.2f}" for k, v in diff.items())
    state.update({"ts": time.time(), "prices": cur, "data_time": data_time, "below": any_below})
    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    print(f"数据时间 {d.get('时间','')}")
    for label, v, delta, _ in rows:
        print(f"{label}: {v:.2f} {delta}")

    # 跌破阈值检测：只在“跌破”或“收复”的瞬间提醒，避免每小时重复打扰
    alerts = []
    any_below = any(v < THRESHOLD for l, v in cur.items() if l.startswith(("工商","建设","交通","招商")))
    was_below = state.get("below", False)
    if any_below and not was_below:
        for label, v in cur.items():
            if label.startswith(("工商","建设","交通","招商")) and v < THRESHOLD:
                alerts.append(f"⚠️ {label} 现价 **{v:.2f}** 元/克，已跌破 {THRESHOLD:.0f} 元/克！")
    elif was_below and not any_below:
        alerts.append(f"✅ 银行小金条价格已收复 {THRESHOLD:.0f} 元/克。")

    # 微信推送：数据有更新才播报；跌破/收复提醒始终发送
    if SCT_KEY:
        if data_time and data_time == state.get("data_time") and not alerts:
            print("数据无更新（休市），跳过本次播报")
        else:
            body_md = f"### 小黄鱼金价监控\n\n数据时间 {data_time}\n\n" + "\n\n".join(md)
            if diff_txt: body_md += f"\n\n溢价(vs上金所)：{diff_txt}"
            if alerts:
                body_md = "\n\n".join(alerts) + "\n\n---\n\n" + body_md
                serverchan("⚠️ 金价跌破900提醒", body_md)
            else:
                serverchan("金价播报 " + time.strftime("%m-%d %H:%M"), body_md)

    # 网页报告
    trs = "\n".join(
        f'<tr class="{cls}"><td>{label}</td><td>{v:.2f}</td><td>{delta}</td></tr>'
        for label, v, delta, cls in rows)
    history = []
    if os.path.exists("history.json"):
        try: history = json.load(open("history.json"))
        except Exception: pass
    history.append({"time": d.get("时间",""), "prices": cur, "alerts": bool(alerts)})
    history = history[-2000:]
    json.dump(history, open("history.json","w"), ensure_ascii=False)
    labels = [r[0] for r in rows]
    hist_rows = "\n".join(
        "<tr><td>%s</td>%s</tr>" % (h["time"], "".join(
            "<td>%s</td>" % h["prices"].get(l, "-") for l in labels))
        for h in history[-48:])
    head = "".join(f"<th>{l}</th>" for l in labels)
    alert_banner = ('<div style="background:#5c1a1a;border:1px solid #ff6b6b;padding:12px;border-radius:8px;margin-bottom:16px">'
                    + "<br>".join(alerts) + "</div>") if alerts else ""
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
<div class="time">数据时间：{d.get('时间','')} ｜ 每小时自动更新，微信同步提醒</div>
{alert_banner}
<table><tr><th>品种</th><th>价格 (元/克)</th><th>变化</th></tr>{trs}</table>
<div class="diff">银行溢价（vs 上金所）：{diff_txt}</div>
<h1 style="font-size:16px">近期走势（最近48次采集）</h1>
<div class="scroll"><table><tr><th>时间</th>{head}</tr>{hist_rows}</table></div>
</body></html>"""
    open(HTML, "w").write(html)

if __name__ == "__main__":
    main()
