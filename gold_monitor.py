#!/usr/bin/env python3
# 小黄鱼数据工坊 金价监控 - GitHub Actions 版
# TOKEN 通过环境变量 GOLD_TOKEN 传入（仓库 Secrets 设置）
import time, secrets, json, hashlib, hmac, urllib.request, os

TOKEN = os.environ.get("GOLD_TOKEN", "")
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

SHOW = [("ICBC","工商银行","5g"),("CCB","建设银行","5g"),("BCM","交通银行","10g"),
        ("CMB","招商银行","10g"),("SGE","上金所","1g"),("HKGX","香港金银","1g"),
        ("FX678","伦敦黄金",None),("FX678","纽约黄金12",None)]

def main():
    d = call_api("/api/miniprogram/latest")["data"]
    prev = {}
    if os.path.exists(STATE):
        try: prev = json.load(open(STATE)).get("prices", {})
        except Exception: pass
    prices = d.get("价格", {})
    cur, rows = {}, []
    for code, cname, spec in SHOW:
        node = prices.get(code, {})
        if spec:
            v = node.get(spec, {}).get("单价"); label = f"{cname} {spec}"
        else:
            v = node.get("值"); label = cname
        if v is None: continue
        cur[label] = v
        delta, cls = "", ""
        if label in prev and abs(prev[label]-v) > 1e-9:
            dd = v - prev[label]
            arrow = "↑" if dd > 0 else "↓"
            delta, cls = (f"（{arrow}{abs(dd):.2f}）", "up" if dd>0 else "down")
        rows.append((label, v, delta, cls))
    diff = d.get("价差", {})
    diff_txt = ", ".join(f"{k.replace('_SGE','')} +{v:.2f}" for k, v in diff.items())
    json.dump({"ts": time.time(), "prices": cur}, open(STATE, "w"))

    print(f"数据时间 {d.get('时间','')}")
    for label, v, delta, _ in rows:
        print(f"{label}: {v:.2f} {delta}")

    trs = "\n".join(
        f'<tr class="{cls}"><td>{label}</td><td>{v:.2f}</td><td>{delta}</td></tr>'
        for label, v, delta, cls in rows)
    history = []
    if os.path.exists("history.json"):
        try: history = json.load(open("history.json"))
        except Exception: pass
    history.append({"time": d.get("时间",""), "prices": cur})
    history = history[-2000:]
    json.dump(history, open("history.json","w"), ensure_ascii=False)
    hist_rows = "\n".join(
        "<tr><td>%s</td>%s</tr>" % (h["time"], "".join(f"<td>{h['prices'].get(l,'-')}</td>" for l,_ ,_ ,_ in [(x[0],0,0,0) for x in rows]))
        for h in history[-48:])
    head = "".join(f"<th>{l}</th>" for l,_,_,_ in rows)
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
<div class="time">数据时间：{d.get('时间','')} ｜ 页面随 GitHub Actions 自动更新（每30分钟）</div>
<table><tr><th>品种</th><th>价格 (元/克)</th><th>变化</th></tr>{trs}</table>
<div class="diff">银行溢价（vs 上金所）：{diff_txt}</div>
<h1 style="font-size:16px">近期走势（最近48次采集）</h1>
<div class="scroll"><table><tr><th>时间</th>{head}</tr>{hist_rows}</table></div>
</body></html>"""
    open(HTML, "w").write(html)

if __name__ == "__main__":
    main()
