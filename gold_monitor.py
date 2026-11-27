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
import time, secrets, json, hashlib, hmac, urllib.request, urllib.parse, urllib.error, os, sys, re

TOKEN = os.environ.get("GOLD_TOKEN", "")
SCT_KEY = os.environ.get("SERVERCHAN_KEY", "")
THRESHOLD = 765.0          # 银行小金条跌破该价提醒
EVENING_REPORT = (21, 30)   # 每日21:30档播报当日高低价（北京时间，落在21:30后的首次运行执行）
BASE = "https://mp.68gold.cn"
STATE = "gold_state.json"
HTML = "index.html"
HISTORY = "history.json"

BANK_PREFIX = ("工商", "建设", "交通", "招商", "中行", "农行", "邮储", "中信", "兴业", "平安", "民生", "浦发", "中国黄金")
SHOW = [("ICBC","工商银行","5g"),("CCB","建设银行","5g"),("BCM","交通银行","10g"),
        ("CMB","招商银行","10g"),("SGE","上金所","1g"),("HKGX","香港金银","1g"),
        ("FX678","伦敦黄金",None),("FX678","纽约黄金12",None)]
UNIT = {"FX678": " 美元/盎司"}

# 补充银行实时行情（金投网 jijinhao 接口，银行牌价每日10:00左右定盘，盘中一般不再变）
# 注：银行投资金条本身是每日定盘价（不同于上金所实时大盘），接口的 q3/q4 高低价对银行无意义
JIJINHAO_CODES = {
    "JO_78649": "中行金条", "JO_78650": "农行传世之宝", "JO_78656": "浦发投资金条",
    "JO_78651": "邮储投资金条", "JO_94699": "中信投资金条", "JO_346542": "兴业投资金条",
    "JO_346444": "平安和谐金条", "JO_346444_": "",  # 占位防错
    "JO_351185": "民生投资金条", "JO_351236": "中国黄金投资金条",
}
JIJINHAO_URL = "https://api.jijinhao.com/quoteCenter/realTime.htm"

def fetch_web_banks(retries=1):
    """拉取补充银行实时牌价，返回 {显示名: (价格, 更新时间HH:MM)}，失败返回 {}"""
    codes = ",".join(k for k, v in JIJINHAO_CODES.items() if v)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(f"{JIJINHAO_URL}?codes={codes}",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.cngold.org/"})
            raw = urllib.request.urlopen(req, timeout=20).read().decode('utf8', 'replace')
            data = json.loads(re.search(r'quote_json\s*=\s*(\{.*\})', raw, re.S).group(1))
            from datetime import datetime as _dt
            out = {}
            for code, short in JIJINHAO_CODES.items():
                if not short: continue
                q = data.get(code) or {}
                v = q.get("q1")
                if v is None: continue
                try: v = float(v)
                except (TypeError, ValueError): continue
                if not (100 < v < 2000):  # 牌价合理范围，过滤0/异常值
                    continue
                t = _dt.fromtimestamp(q["time"]/1000).strftime("%H:%M") if q.get("time") else ""
                out[short] = (v, t)
            return out
        except Exception as e:
            last = e
            time.sleep(3)
    print("补充行情抓取失败（不影响主监控）:", last)
    return {}

def now_bj():
    """北京时间 struct_time（GitHub runner 是 UTC）"""
    return time.gmtime(time.time() + 8 * 3600)

def call_api(path, token=None, method="GET", body="", retries=2):
    token = token if token is not None else TOKEN
    if not token:
        raise RuntimeError("GOLD_TOKEN 未配置（环境变量缺失）")
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
        if not (100 < v < 2000):
            continue
        cur[label] = v
        delta = ""
        if label in prev and abs(prev[label] - v) > 1e-9:
            dd = v - prev[label]
            delta = f"（{'↑' if dd > 0 else '↓'}{abs(dd):.2f}）"
        rows.append((label, v, delta, "up" if "↑" in delta else ("down" if "↓" in delta else "")))
        md.append(f"{label}: **{v:.2f}**{delta}")

    # 合并补充银行（金投网牌价，含更新时间）；失败不影响主监控
    try:
        web = fetch_web_banks()
    except Exception as e:
        print("补充行情异常（跳过）:", e)
        web = {}
    if web:
        print("补充银行:", web)
        for label, (v, vt) in web.items():
            if label in cur: continue
            cur[label] = v
            delta = ""
            if label in prev and abs(prev[label] - v) > 1e-9:
                dd = v - prev[label]
                delta = f"（{'↑' if dd > 0 else '↓'}{abs(dd):.2f}）"
            rows.append((f"{label}", v, f"（牌价{vt}）{delta}", "" if not delta else ("up" if "↑" in delta else "down")))
            md.append(f"{label}: **{v:.2f}**（牌价{vt}）{delta}")
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

    # ── 4. 跌破/收复检测 ──
    alerts = []
    bj = now_bj()
    is_weekend = bj.tm_wday >= 5
    today = time.strftime("%Y-%m-%d", bj)
    realtime_banks = {f"{cn} {sp}" for _, cn, sp in SHOW if cn.startswith(BANK_PREFIX)}
    any_below = any(v < THRESHOLD for l, v in cur.items() if l in realtime_banks)
    was_below = state.get("below", False)
    if any_below and not was_below:
        for label, v in cur.items():
            if label in realtime_banks and v < THRESHOLD:
                alerts.append(f"⚠️ {label} 现价 **{v:.2f}** 元/克，已跌破 {THRESHOLD:.0f} 元/克！")
    elif was_below and not any_below:
        alerts.append(f"✅ 银行小金条价格已收复 {THRESHOLD:.0f} 元/克。")
    state["below"] = any_below

    # 补充银行（每日定盘价）跌破检测：定盘价<765 → 每银行每天提醒一次
    try:
        fixed_alerted = state.get("fixed_alerted", {})
        fx_today = fixed_alerted.get("date") == today
        for label, v in cur.items():
            if label in realtime_banks or label in ("上金所 1g", "香港金银 1g"):
                continue
            if not (100 < v < 2000):
                continue
            if v < THRESHOLD and label not in (fixed_alerted.get("banks") or []):
                if not fx_today:
                    fixed_alerted = {"date": today, "banks": []}
                    fx_today = True
                alerts.append(f"⚠️ {label} 牌价 **{v:.2f}** 元/克，低于 {THRESHOLD:.0f}！")
                fixed_alerted["banks"].append(label)
        state["fixed_alerted"] = fixed_alerted
    except Exception as e:
        print("补充银行跌破检测异常:", e)

    # 日内最低兜底：瞬时跌破（采样间隙漏掉）也能发现，每银行每天最多提醒一次
    try:
        daily = call_api("/api/miniprogram/statistics/daily").get("data", {}).get("today", {}).get("summary", {})
        today = daily.get("ICBC", {}).get("5g", {}).get("high_time", "")[:10] or time.strftime("%Y-%m-%d", now_bj())
        low_alerted = state.get("low_alerted", {})
        if low_alerted.get("date") != today:
            low_alerted = {"date": today, "banks": []}
        for code, cname, spec in SHOW:
            if not cname.startswith(BANK_PREFIX):
                continue
            info = (daily.get(code) or {}).get(spec) or {}
            low = info.get("low")
            if low is None:
                continue
            try: low = float(low)
            except (TypeError, ValueError): continue
            if not (100 < low < 2000):
                continue
            if low < THRESHOLD and code not in low_alerted["banks"]:
                low_time = (info.get("low_time") or "")[11:16]
                alerts.append(f"⚠️ {cname} {spec} 日内最低 **{low:.2f}** 元/克（{low_time}），已触及 {THRESHOLD:.0f} 下方！")
                low_alerted["banks"].append(code)
        state["low_alerted"] = low_alerted
    except Exception as e:
        print("日内统计获取失败（不影响主流程）:", e)

    json.dump(state, open(STATE, "w"), ensure_ascii=False)

    # ── 5. 微信推送 ──
    price_changed = cur != prev
    if alerts:
        body_md = "\n\n".join(alerts) + "\n\n---\n\n### 小黄鱼金价监控\n\n数据时间 " + data_time + "\n\n" + "\n\n".join(md)
        if diff_txt:
            body_md += "\n\n溢价(vs上金所)：" + diff_txt
        if any("收复" in a for a in alerts):
            title = f"✅ 金价收复{THRESHOLD:.0f}"
        elif all("日内最低" in a for a in alerts):
            title = f"⚠️ 金价盘中触及{THRESHOLD:.0f}下方"
        else:
            title = f"⚠️ 金价跌破{THRESHOLD:.0f}提醒"
        serverchan(title, body_md)
    if (bj.tm_hour, bj.tm_min) >= EVENING_REPORT and not is_weekend \
            and state.get("evening_date") != today:
        # 每日21:30档：当日各银行最高价/最低价（带时间）
        lines_e = ["### 每日金价回顾（当日高低）", ""]
        try:
            daily = call_api("/api/miniprogram/statistics/daily").get("data", {})
            summ = (daily.get("today") or {}).get("summary") or {}
            for code, cname, spec in SHOW:
                if not cname.startswith(BANK_PREFIX):
                    continue
                info = (summ.get(code) or {}).get(spec) or {}
                hi, lo = info.get("high"), info.get("low")
                if hi is None or lo is None:
                    continue
                try:
                    hi, lo = float(hi), float(lo)
                except (TypeError, ValueError):
                    continue
                ht = (info.get("high_time") or "")[11:16]
                lt = (info.get("low_time") or "")[11:16]
                lines_e.append(f"**{cname} {spec}**：最高 {hi:.2f}（{ht}）／ 最低 {lo:.2f}（{lt}）")
            # 补充银行（每日定盘价，无日内高低，报当日牌价+更新时间）；web 复用主流程抓取结果
            try:
                if web:
                    lines_e.append("**其他银行牌价（每日定盘）**")
                    for short, (v, vt) in web.items():
                        lines_e.append(f"{short}：{v:.2f}（牌价{vt}）")
                    lines_e.append("")
                else:
                    lines_e.append("（其他银行牌价暂缺）")
            except Exception as e:
                lines_e.append("（其他银行牌价暂缺）")
                print("晚报补充行情失败（跳过该节）:", e)
            lines_e.append(f"数据时间 {data_time}")
            if serverchan(f"📊 每日金价回顾 {today[5:]}", "\n\n".join(lines_e)):
                state["evening_date"] = today  # 推送成功才标记，失败下小时重试
            else:
                print("日报推送失败，下小时重试")
        except Exception as e:
            print("晚间日报生成失败（下小时重试）:", e)
    if not alerts and (state.get("evening_date") == today or is_weekend or (bj.tm_hour, bj.tm_min) < EVENING_REPORT):
        print("非事件提醒，跳过推送（仅跌破/收复765时提醒）")
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
        for h in reversed(history[-48:]))
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
<div class="time">数据时间：{data_time} ｜ 每15分钟检测；跌破/收复765微信提醒，每日21:30晚报</div>
{alert_banner}
<table><tr><th>品种</th><th>价格</th><th>变化</th></tr>{trs}</table>
<h1 style="font-size:16px">近期走势（最近48次采集）</h1>
<div class="scroll"><table><tr><th>时间</th>{head}</tr>{hist_rows}</table></div>
</body></html>"""
    open(HTML, "w").write(html)

if __name__ == "__main__":
    main()
