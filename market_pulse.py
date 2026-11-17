#!/usr/bin/env python3
"""
Market Pulse - a personal daily markets dashboard.

Pulls prices from Yahoo Finance and headlines from the Wall Street Journal,
Yahoo Finance, MarketWatch and CNBC, writes a written market brief, and renders
everything to output/dashboard.html.

Usage:
  python3 market_pulse.py run [--mode auto|briefing|intraday] [--open]
  python3 market_pulse.py install     # schedule it on this Mac (launchd)
  python3 market_pulse.py uninstall   # remove the schedule
  python3 market_pulse.py status      # show schedule + last run

Standard library only - no pip installs needed.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
ARCHIVE = os.path.join(OUT, "archive")
DATA = os.path.join(OUT, "data")
LOGS = os.path.join(OUT, "logs")
DASHBOARD = os.path.join(OUT, "dashboard.html")
NY = ZoneInfo("America/New_York")
UA = "MarketPulse/1.0 (personal dashboard)"
LABEL_BRIEF = "com.marketpulse.briefing"
LABEL_INTRA = "com.marketpulse.intraday"
AGENTS = os.path.expanduser("~/Library/LaunchAgents")


def load_config():
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    local = os.path.join(ROOT, "local_settings.json")  # private, git-ignored
    if os.path.exists(local):
        with open(local) as f:
            cfg.update(json.load(f))
    return cfg


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------- prices

def fetch_quote(symbol, history=False):
    """Intraday 5-minute series + last price + previous close from Yahoo's chart API."""
    sym = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=5m"
    for attempt in range(3):
        try:
            res = json.loads(http_get(url))["chart"]["result"][0]
            break
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    meta = res.get("meta", {})
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    closes = []
    try:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    except (KeyError, IndexError, TypeError):
        pass
    q = {
        "symbol": symbol,
        "price": price,
        "prev": prev,
        "change": (price - prev) if price is not None and prev else None,
        "pct": ((price / prev - 1) * 100) if price is not None and prev else None,
        "series": closes[-90:],
        "time": meta.get("regularMarketTime"),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
    }
    if history:
        try:
            h = json.loads(http_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d"))
            hc = [c for c in h["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
            if price and len(hc) > 6:
                q["pct_5d"] = (price / hc[-6] - 1) * 100
            if price and len(hc) > 22:
                q["pct_1m"] = (price / hc[-22] - 1) * 100
            if price and hc:
                q["hi_3m"] = max(hc + [price])
                q["off_high"] = (price / q["hi_3m"] - 1) * 100
        except Exception:
            pass
    return q


def fetch_all_quotes(cfg):
    groups = ["indices", "futures", "macro", "sectors", "global", "watchlist"]
    jobs = []
    for g in groups:
        for sym, name in cfg.get(g, []):
            jobs.append((g, sym, name))
    out = {g: [] for g in groups}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_quote, sym, g == "indices"): (g, sym, name) for g, sym, name in jobs}
        results = {}
        for f in cf.as_completed(futs):
            results[futs[f]] = f.result()
    for g, sym, name in jobs:
        q = results.get((g, sym, name))
        if q:
            q["name"] = name
            out[g].append(q)
    return out


# --------------------------------------------------------------------------- news

def _text(el, tag):
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def fetch_feed(source, url, limit):
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        return {"source": source, "items": [], "error": str(e)[:120]}
    items = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        link = _text(it, "link")
        if not title or not link:
            continue
        ts = None
        pub = _text(it, "pubDate")
        if pub:
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except Exception:
                pass
        desc = re.sub(r"<[^>]+>", "", html.unescape(_text(it, "description")))
        items.append({"title": title, "link": link, "ts": ts, "desc": desc[:220]})
    items.sort(key=lambda i: i["ts"] or 0, reverse=True)
    return {"source": source, "items": items[:limit], "error": None}


def fetch_news(cfg):
    limit = cfg.get("headlines_per_source", 8)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        feeds = list(ex.map(lambda s: fetch_feed(s[0], s[1], limit), cfg["news_feeds"]))
    seen = set()
    for f in feeds:  # drop cross-source duplicates
        keep = []
        for i in f["items"]:
            k = re.sub(r"\W+", "", i["title"].lower())[:60]
            if k not in seen:
                seen.add(k)
                keep.append(i)
        f["items"] = keep
    return feeds


# --------------------------------------------------------------------------- market clock

def market_state(now):
    if now.weekday() >= 5:
        return "closed"
    t = now.hour * 60 + now.minute
    if t < 4 * 60:
        return "closed"
    if t < 9 * 60 + 30:
        return "pre"
    if t < 16 * 60:
        return "open"
    if t < 20 * 60:
        return "after"
    return "closed"


def session_label(now, state):
    if state == "pre":
        return "Pre-Market Briefing"
    if state == "open":
        t = now.hour * 60 + now.minute
        return "Morning Briefing" if t < 11 * 60 else "Midday Update"
    if state == "after":
        return "Closing Briefing"
    return "Weekend Recap" if now.weekday() >= 5 else "Overnight Recap"


# --------------------------------------------------------------------------- the written brief

def by_sym(quotes):
    return {q["symbol"]: q for g in quotes.values() for q in g}


def fmt_pct(p, digits=2):
    if p is None:
        return "n/a"
    sign = "+" if p > 0 else ("−" if p < 0 else "")
    return f"{sign}{abs(p):.{digits}f}%"


def move_phrase(p, live):
    a = abs(p)
    if a < 0.15:
        return "is roughly flat" if live else "finished roughly flat"
    if live:
        word = "up" if p > 0 else "down"
        strength = "sharply " if a >= 1.5 else ""
        return f"is {strength}{word} {a:.2f}%"
    if p > 0:
        verb = "rallied" if a >= 1.5 else ("gained" if a >= 0.5 else "edged up")
    else:
        verb = "sold off" if a >= 1.5 else ("fell" if a >= 0.5 else "slipped")
    return f"{verb} {a:.2f}%"


def build_brief(quotes, news, state, holiday):
    s = by_sym(quotes)
    live = state == "open" and not holiday
    lines = []

    spx, ndx, dji, rut = s.get("^GSPC"), s.get("^IXIC"), s.get("^DJI"), s.get("^RUT")
    if state == "pre" and s.get("ES=F") and s["ES=F"]["pct"] is not None:
        es = s["ES=F"]
        nq = s.get("NQ=F")
        d = "higher" if es["pct"] > 0 else "lower"
        txt = f"Before the bell, S&P 500 futures are pointing {d} ({fmt_pct(es['pct'])})"
        if nq and nq["pct"] is not None:
            txt += f" and Nasdaq futures are {fmt_pct(nq['pct'])}"
        lines.append(txt + ".")
    elif spx and spx["pct"] is not None:
        txt = f"The S&P 500 {move_phrase(spx['pct'], live)} to {spx['price']:,.2f}"
        parts = []
        if dji and dji["pct"] is not None:
            parts.append(f"the Dow {fmt_pct(dji['pct'])}")
        if ndx and ndx["pct"] is not None:
            parts.append(f"the Nasdaq {fmt_pct(ndx['pct'])}")
        if parts:
            txt += ", with " + " and ".join(parts)
        txt += "."
        if rut and rut["pct"] is not None and spx["pct"] is not None:
            gap = rut["pct"] - spx["pct"]
            if abs(gap) >= 0.75:
                txt += (" Small caps are outperforming" if gap > 0 else " Small caps are lagging") + \
                       f" (Russell 2000 {fmt_pct(rut['pct'])})."
        lines.append(txt)
        if spx.get("pct_5d") is not None:
            ctx = f"Over the past week the S&P is {fmt_pct(spx['pct_5d'])}"
            if spx.get("pct_1m") is not None:
                ctx += f" and {fmt_pct(spx['pct_1m'])} over the past month"
            if spx.get("off_high") is not None and spx["off_high"] < -0.5:
                ctx += f"; it sits {abs(spx['off_high']):.1f}% below its 3-month high"
            lines.append(ctx + ".")

    secs = [q for q in quotes.get("sectors", []) if q["pct"] is not None]
    if secs:
        up = sum(1 for q in secs if q["pct"] > 0)
        best = max(secs, key=lambda q: q["pct"])
        worst = min(secs, key=lambda q: q["pct"])
        n = len(secs)
        if up == 0:
            txt = f"All {n} sectors are lower; {best['name']} is holding up best ({fmt_pct(best['pct'])}) " \
                  f"while {worst['name']} is the weakest ({fmt_pct(worst['pct'])})."
        elif up == n:
            txt = f"All {n} sectors are higher, led by {best['name']} ({fmt_pct(best['pct'])}); " \
                  f"{worst['name']} is the laggard ({fmt_pct(worst['pct'])})."
        else:
            txt = f"{up} of {n} sectors are higher; {best['name']} leads ({fmt_pct(best['pct'])}) " \
                  f"and {worst['name']} trails ({fmt_pct(worst['pct'])})."
        sp = {q["symbol"]: q["pct"] for q in secs}
        defensive = [sp[k] for k in ("XLP", "XLU", "XLV") if k in sp]
        growth = [sp[k] for k in ("XLK", "XLY", "XLC") if k in sp]
        if defensive and growth:
            spread = sum(defensive) / len(defensive) - sum(growth) / len(growth)
            if spread > 0.75:
                txt += " Defensive sectors are beating growth — a risk-off rotation."
            elif spread < -0.75:
                txt += " Growth sectors are beating defensives — a risk-on tone."
        lines.append(txt)

    macro = []
    vix = s.get("^VIX")
    if vix and vix["price"] is not None:
        lvl = "elevated" if vix["price"] >= 20 else "calm"
        if vix["price"] >= 30:
            lvl = "high-stress"
        macro.append(f"the VIX is {vix['price']:.1f} ({lvl}, {fmt_pct(vix['pct'], 1)})")
    tnx = s.get("^TNX")
    if tnx and tnx["price"] is not None and tnx["change"] is not None:
        bps = tnx["change"] * 100
        macro.append(f"the 10-year yield is {tnx['price']:.2f}% ({'+' if bps >= 0 else '−'}{abs(bps):.0f} bps)")
    oil, gold, btc, dxy = s.get("CL=F"), s.get("GC=F"), s.get("BTC-USD"), s.get("DX-Y.NYB")
    if oil and oil["pct"] is not None:
        macro.append(f"WTI crude is ${oil['price']:.2f} ({fmt_pct(oil['pct'])})")
    if gold and gold["pct"] is not None:
        macro.append(f"gold is {fmt_pct(gold['pct'])}")
    if dxy and dxy["pct"] is not None:
        macro.append(f"the dollar is {fmt_pct(dxy['pct'])}")
    if btc and btc["pct"] is not None:
        macro.append(f"bitcoin is {fmt_pct(btc['pct'])}")
    if macro:
        m = "; ".join(macro)
        lines.append(m[0].upper() + m[1:] + ".")

    wl = [q for q in quotes.get("watchlist", []) if q["pct"] is not None]
    if wl:
        wl.sort(key=lambda q: q["pct"])
        lo, hi = wl[0], wl[-1]
        lines.append(f"Biggest watchlist movers: {hi['name']} {fmt_pct(hi['pct'])}, "
                     f"{lo['name']} {fmt_pct(lo['pct'])}.")

    top = []
    for f in news:
        if f["items"]:
            top.append((f["source"], f["items"][0]))
    return lines, top[:3]


# --------------------------------------------------------------------------- rendering

def esc(x):
    return html.escape(str(x), quote=True)


def cls(p):
    if p is None or abs(p) < 0.005:
        return "flat"
    return "up" if p > 0 else "down"


def spark(series, prev, w=120, h=34):
    if not series or len(series) < 2:
        return ""
    vals = series + ([prev] if prev else [])
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(series)
    pts = " ".join(f"{i * w / (n - 1):.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}" for i, v in enumerate(series))
    base = ""
    if prev:
        y = h - 2 - (prev - lo) / rng * (h - 4)
        base = f'<line x1="0" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" class="base"/>'
    c = "up" if series[-1] >= (prev or series[0]) else "down"
    return (f'<svg class="spark {c}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'{base}<polyline points="{pts}"/></svg>')


def fmt_price(q):
    p = q["price"]
    if p is None:
        return "—"
    if q["symbol"] in ("^TNX", "^TYX"):
        return f"{p:.3f}%"
    if p >= 1000:
        return f"{p:,.2f}"
    return f"{p:,.2f}"


def fmt_change(q):
    if q["symbol"] in ("^TNX", "^TYX") and q["change"] is not None:
        bps = q["change"] * 100
        return f"{'+' if bps > 0 else ('−' if bps < 0 else '')}{abs(bps):.1f} bps"
    return fmt_pct(q["pct"])


def tile(q, big=False):
    p = q["pct"]
    return f"""
    <div class="tile {'big' if big else ''}">
      <div class="t-name">{esc(q['name'])}<span class="t-sym">{esc(q['symbol'])}</span></div>
      <div class="t-row">
        <div class="t-price">{fmt_price(q)}</div>
        <div class="chg {cls(p)}">{arrow(p)}{fmt_change(q)}</div>
      </div>
      {spark(q['series'], q['prev'], 160 if big else 120, 40 if big else 30)}
    </div>"""


def arrow(p):
    if p is None or abs(p) < 0.005:
        return ""
    return "▲ " if p > 0 else "▼ "


def heat_style(p):
    if p is None:
        return ""
    a = min(abs(p) / 2.5, 1)
    hue = "var(--up-rgb)" if p > 0 else "var(--down-rgb)"
    return f"background: rgba({hue}, {0.12 + a * 0.6:.2f});"


def ago(ts, now_ts):
    if not ts:
        return ""
    m = int((now_ts - ts) / 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m}m ago"
    if m < 60 * 24:
        return f"{m // 60}h ago"
    return f"{m // 1440}d ago"


def render(cfg, quotes, news, now, state, holiday, label, brief, top, archive_links, is_snapshot=False):
    now_ts = now.timestamp()
    state_text = {"pre": "Pre-market", "open": "Market open", "after": "After hours", "closed": "Market closed"}[state]
    if holiday:
        state_text = "Market closed (holiday)"
    local = now.astimezone()
    updated = f"{local.strftime('%-I:%M %p')} {local.strftime('%Z')}"
    if local.utcoffset() != now.utcoffset():
        updated += f" · {now.strftime('%-I:%M %p')} ET"

    idx = "".join(tile(q, True) for q in quotes["indices"])
    fut = "".join(tile(q) for q in quotes["futures"])
    mac = "".join(tile(q) for q in quotes["macro"])
    glo = "".join(tile(q) for q in quotes["global"])

    secs = sorted(quotes["sectors"], key=lambda q: q["pct"] if q["pct"] is not None else -99, reverse=True)
    heat = "".join(
        f'<div class="cell" style="{heat_style(q["pct"])}"><span class="c-name">{esc(q["name"])}</span>'
        f'<span class="c-sym">{esc(q["symbol"])}</span><span class="c-pct">{fmt_pct(q["pct"])}</span></div>'
        for q in secs)

    wl = sorted(quotes["watchlist"], key=lambda q: q["pct"] if q["pct"] is not None else 0, reverse=True)
    rows = "".join(
        f'<tr><td><b>{esc(q["symbol"])}</b> <span class="muted">{esc(q["name"])}</span></td>'
        f'<td class="num">{fmt_price(q)}</td>'
        f'<td class="num chg {cls(q["pct"])}">{fmt_pct(q["pct"])}</td>'
        f'<td class="sp">{spark(q["series"], q["prev"], 90, 22)}</td></tr>'
        for q in wl)

    brief_html = "".join(f"<p>{esc(l)}</p>" for l in brief)
    top_html = "".join(
        f'<li><span class="src">{esc(src)}</span><a href="{esc(i["link"])}" target="_blank" rel="noopener">{esc(i["title"])}</a></li>'
        for src, i in top)

    news_cols = ""
    for f in news:
        items = "".join(
            f'<li><a href="{esc(i["link"])}" target="_blank" rel="noopener">{esc(i["title"])}</a>'
            f'<span class="when">{ago(i["ts"], now_ts)}</span></li>' for i in f["items"])
        if not items:
            items = f'<li class="muted">Feed unavailable right now{(": " + esc(f["error"])) if f.get("error") else ""}</li>'
        news_cols += f'<section class="feed"><h3>{esc(f["source"])}</h3><ol>{items}</ol></section>'

    arch = "".join(f'<a href="{esc(href)}">{esc(text)}</a>' for text, href in archive_links)
    refresh = "" if is_snapshot else '<meta http-equiv="refresh" content="300">'
    show_futures = state in ("pre", "closed", "after") or holiday

    with open(os.path.join(ROOT, "template.html")) as f:
        tpl = f.read()
    repl = {
        "{{REFRESH}}": refresh,
        "{{LABEL}}": esc(label),
        "{{DATE}}": now.strftime("%A, %B %-d, %Y"),
        "{{STATE}}": esc(state_text),
        "{{STATE_CLASS}}": "live" if state == "open" and not holiday else "idle",
        "{{UPDATED}}": esc(updated),
        "{{BRIEF}}": brief_html,
        "{{TOP}}": top_html,
        "{{INDICES}}": idx,
        "{{FUTURES}}": fut,
        "{{FUTURES_FIRST}}": "first" if show_futures else "",
        "{{MACRO}}": mac,
        "{{GLOBAL}}": glo,
        "{{HEAT}}": heat,
        "{{WATCH}}": rows,
        "{{NEWS}}": news_cols,
        "{{ARCHIVE}}": arch or '<span class="muted">Briefings you receive will be saved here.</span>',
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl


# --------------------------------------------------------------------------- run

def notify(title, subtitle, message):
    def q(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{q(message)}" with title "{q(title)}" subtitle "{q(subtitle)}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


SITE = os.path.join(ROOT, ".site")


def git(*a):
    return subprocess.run(["git", "-C", SITE, *a], capture_output=True, text=True)


def publish_site(cfg, stamp):
    """Upload the dashboard + archive to GitHub Pages. Returns True once the new page is live."""
    pub = cfg.get("publish") or {}
    if not pub.get("enabled"):
        return False
    if not os.path.isdir(os.path.join(SITE, ".git")):
        os.makedirs(SITE, exist_ok=True)
        git("init", "-q")
        git("remote", "add", "origin", pub["remote"])
        git("config", "user.name", "Market Pulse")
        git("config", "user.email", pub.get("email", "marketpulse@users.noreply.github.com"))
    for name in os.listdir(SITE):
        if name != ".git":
            p = os.path.join(SITE, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    shutil.copy(DASHBOARD, os.path.join(SITE, "index.html"))
    shutil.copytree(ARCHIVE, os.path.join(SITE, "archive"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    git("add", "-A")
    git("checkout", "-q", "--orphan", "publish-tmp")  # keep the published branch to one commit
    c = git("commit", "-q", "-m", f"Dashboard {stamp}")
    if c.returncode != 0 and "nothing to commit" not in c.stdout + c.stderr:
        print(f"  publish: commit failed: {c.stderr.strip()[:200]}")
        return False
    p = git("push", "-q", "-f", "origin", f"HEAD:{pub.get('branch', 'gh-pages')}")
    git("branch", "-M", "publish-tmp", "main")
    if p.returncode != 0:
        print(f"  publish: push failed: {p.stderr.strip()[:200]}")
        return False
    print(f"  published to {pub['url']}")
    return True


def wait_until_live(url, stamp, timeout=150):
    """GitHub Pages takes ~30-60s to deploy; wait so the push opens the fresh page."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if stamp.encode() in http_get(url + f"?t={int(time.time())}", timeout=10):
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


def push_phone(cfg, label, brief, top, force=False):
    """Send the brief to a phone via ntfy (free app, no account needed)."""
    pp = cfg.get("phone_push") or {}
    if not pp.get("enabled") or not pp.get("topic"):
        return
    kind = "closing" if "Closing" in label else "morning"
    if not force and kind not in pp.get("push_on", ["morning"]):
        return
    body = "\n\n".join(brief[:4])
    if top:
        body += "\n\nTop story (" + top[0][0] + "): " + top[0][1]["title"]
    headers = {"Title": f"Market Pulse - {label}", "Tags": "chart_with_upwards_trend",
               "Priority": "default", "User-Agent": UA}
    site = (cfg.get("publish") or {}).get("url") if (cfg.get("publish") or {}).get("enabled") else None
    if site:
        headers["Click"] = site
        headers["Actions"] = f"view, Open dashboard, {site}"
    elif top:
        headers["Click"] = top[0][1]["link"]
    url = pp.get("server", "https://ntfy.sh").rstrip("/") + "/" + pp["topic"]
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        print("  pushed to phone")
    except Exception as e:
        print(f"  phone push failed: {e}")


def archive_list():
    if not os.path.isdir(ARCHIVE):
        return []
    files = sorted((f for f in os.listdir(ARCHIVE) if f.endswith(".html")), reverse=True)[:14]
    out = []
    for f in files:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})_(.+)\.html", f)
        if m:
            d = dt.date(int(m[1]), int(m[2]), int(m[3]))
            out.append((f"{d.strftime('%a %b %-d')} · {m[6].replace('-', ' ')}", f"archive/{f}"))
    return out


def cmd_run(args):
    cfg = load_config()
    for d in (OUT, ARCHIVE, DATA, LOGS):
        os.makedirs(d, exist_ok=True)
    now = dt.datetime.now(NY)
    state = market_state(now)

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        fq = ex.submit(fetch_all_quotes, cfg)
        fn = ex.submit(fetch_news, cfg)
        quotes, news = fq.result(), fn.result()

    spx = by_sym(quotes).get("^GSPC")
    holiday = False
    if state == "open" and spx and spx.get("time"):
        holiday = dt.datetime.fromtimestamp(spx["time"], NY).date() != now.date()

    label = session_label(now, state)
    brief, top = build_brief(quotes, news, state, holiday)
    is_briefing = args.mode == "briefing"

    links = archive_list()
    page = render(cfg, quotes, news, now, state, holiday, label, brief, top, links)
    tmp = DASHBOARD + ".tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, DASHBOARD)

    stamp = now.strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(DATA, "latest.json"), "w") as f:
        json.dump({"generated": now.isoformat(), "label": label, "state": state,
                   "brief": brief, "quotes": quotes, "news": news}, f, indent=1)

    if is_briefing:
        slug = label.replace(" ", "-")
        snap = render(cfg, quotes, news, now, state, holiday, label, brief, top,
                      [(t, "../" + h if not h.startswith("archive/") else h.replace("archive/", ""))
                       for t, h in links], is_snapshot=True)
        with open(os.path.join(ARCHIVE, f"{stamp}_{slug}.html"), "w") as f:
            f.write(snap)
        shutil.copy(os.path.join(DATA, "latest.json"), os.path.join(DATA, f"{stamp}.json"))
        # re-render so the new snapshot appears in the dashboard's archive list
        page = render(cfg, quotes, news, now, state, holiday, label, brief, top, archive_list())
        with open(DASHBOARD, "w") as f:
            f.write(page)

    n_q = sum(len(v) for v in quotes.values())
    n_n = sum(len(f["items"]) for f in news)
    print(f"[{now:%Y-%m-%d %H:%M} ET] {label}: {n_q} quotes, {n_n} headlines in {time.time() - t0:.1f}s")
    for l in brief:
        print("  " + l)

    if is_briefing and cfg.get("notify_on_briefing", True):
        notify("Market Pulse", label, brief[0] if brief else "Dashboard updated.")
    page_stamp = f'<meta name="mp-stamp" content="{stamp}-{int(time.time())}">'
    with open(DASHBOARD) as f:
        page = f.read()
    with open(DASHBOARD, "w") as f:
        f.write(page.replace("<head>", "<head>\n" + page_stamp, 1))
    live = publish_site(cfg, stamp)
    wants_push = is_briefing or args.push
    if wants_push:
        if live and not wait_until_live(cfg["publish"]["url"], page_stamp):
            print("  site still deploying; sending push anyway")
        push_phone(cfg, label, brief, top, force=args.push)
    if args.open or (is_briefing and cfg.get("open_dashboard_on_briefing", True)):
        subprocess.run(["open", DASHBOARD], check=False)


# --------------------------------------------------------------------------- schedule (launchd)

def et_to_local(hhmm):
    h, m = map(int, hhmm.split(":"))
    today = dt.date.today()
    et = dt.datetime(today.year, today.month, today.day, h, m, tzinfo=NY)
    loc = et.astimezone()
    return loc.hour, loc.minute


def plist(label, mode, times):
    entries = "".join(
        f"""
      <dict><key>Weekday</key><integer>{wd}</integer><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer></dict>"""
        for wd in range(1, 6) for h, m in times)
    log = os.path.join(LOGS, f"{mode}.log")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{esc(sys.executable)}</string>
    <string>{esc(os.path.join(ROOT, 'market_pulse.py'))}</string>
    <string>run</string>
    <string>--mode</string>
    <string>{mode}</string>
  </array>
  <key>WorkingDirectory</key><string>{esc(ROOT)}</string>
  <key>StartCalendarInterval</key>
  <array>{entries}
  </array>
  <key>StandardOutPath</key><string>{esc(log)}</string>
  <key>StandardErrorPath</key><string>{esc(log)}</string>
</dict>
</plist>
"""


def cmd_install(args):
    cfg = load_config()
    sch = cfg["schedule_et"]
    os.makedirs(AGENTS, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    brief_times = [et_to_local(sch["morning_briefing"]), et_to_local(sch["closing_briefing"])]
    step = int(sch.get("intraday_every_minutes", 30))
    intra = []
    t = 9 * 60 + 30 + step  # first intraday refresh after the open
    while t < 16 * 60:
        hhmm = f"{t // 60:02d}:{t % 60:02d}"
        if hhmm not in (sch["morning_briefing"], sch["closing_briefing"]):
            intra.append(et_to_local(hhmm))
        t += step
    uid = os.getuid()
    for label, mode, times in ((LABEL_BRIEF, "briefing", brief_times), (LABEL_INTRA, "intraday", intra)):
        path = os.path.join(AGENTS, label + ".plist")
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", path], capture_output=True)
        with open(path, "w") as f:
            f.write(plist(label, mode, times))
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", path], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Could not load {label}: {r.stderr.strip()}")
            sys.exit(1)
    fmt = lambda hm: dt.time(*hm).strftime("%-I:%M %p")
    tz = dt.datetime.now().astimezone().strftime("%Z")
    print("Scheduled (Mon–Fri, your local time, " + tz + "):")
    print(f"  Morning briefing   {fmt(brief_times[0])}  (notification + opens dashboard)")
    print(f"  Closing briefing   {fmt(brief_times[1])}  (notification + opens dashboard)")
    print(f"  Intraday refresh   every {step} min, {fmt(intra[0])}–{fmt(intra[-1])} (silent)")
    print(f"Dashboard: {DASHBOARD}")


def cmd_uninstall(args):
    uid = os.getuid()
    for label in (LABEL_BRIEF, LABEL_INTRA):
        path = os.path.join(AGENTS, label + ".plist")
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", path], capture_output=True)
        if os.path.exists(path):
            os.remove(path)
    print("Schedule removed.")


def cmd_status(args):
    for label in (LABEL_BRIEF, LABEL_INTRA):
        r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
        print(f"{label}: {'scheduled' if r.returncode == 0 else 'not scheduled'}")
    for mode in ("briefing", "intraday"):
        p = os.path.join(LOGS, f"{mode}.log")
        if os.path.exists(p):
            with open(p) as f:
                runs = [l for l in f if l.startswith("[")]
            if runs:
                print(f"last {mode}: {runs[-1].strip()}")


def main():
    ap = argparse.ArgumentParser(description="Market Pulse dashboard")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["auto", "briefing", "intraday"], default="auto")
    r.add_argument("--open", action="store_true", help="open the dashboard when done")
    r.add_argument("--push", action="store_true", help="also send a test push to your phone")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    a = ap.parse_args()
    {"run": cmd_run, "install": cmd_install, "uninstall": cmd_uninstall,
     "status": cmd_status}.get(a.cmd or "run", cmd_run)(a if a.cmd else ap.parse_args(["run"]))


if __name__ == "__main__":
    main()
