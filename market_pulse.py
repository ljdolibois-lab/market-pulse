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
            for k, v in json.load(f).items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k] = {**cfg[k], **v}
                else:
                    cfg[k] = v
    if os.environ.get("NTFY_TOPIC"):  # GitHub secret in the cloud
        cfg.setdefault("phone_push", {})["topic"] = os.environ["NTFY_TOPIC"]
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


# --------------------------------------------------------------------------- sector holdings

SSGA = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{}.xlsx"
XNS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx_rows(data):
    """Minimal .xlsx reader (first sheet) using only the standard library."""
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(XNS + "si"):
            strings.append("".join(t.text or "" for t in si.iter(XNS + "t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iter(XNS + "row"):
        vals = []
        for c in row.iter(XNS + "c"):
            v = c.find(XNS + "v")
            if v is None:
                vals.append("")
            elif c.get("t") == "s":
                vals.append(strings[int(v.text)])
            else:
                vals.append(v.text or "")
        rows.append(vals)
    return rows


def fetch_holdings(etf, top_n):
    """Top holdings + weights for a SPDR sector ETF, from State Street's daily file."""
    try:
        rows = read_xlsx_rows(http_get(SSGA.format(etf.lower()), timeout=20))
    except Exception:
        return []
    out, cols = [], None
    for r in rows:
        if cols is None:
            if "Ticker" in r and "Weight" in r and "Name" in r:
                cols = (r.index("Name"), r.index("Ticker"), r.index("Weight"))
            continue
        try:
            name, tick, w = r[cols[0]], r[cols[1]], float(r[cols[2]])
        except (IndexError, ValueError):
            continue
        if not tick or not re.match(r"^[A-Z]+(\.[A-Z])?$", tick):
            continue
        out.append({"ticker": tick.replace(".", "-"), "name": name.title(), "weight": w})
    out.sort(key=lambda h: h["weight"], reverse=True)
    return out[:top_n]


def fetch_spark(symbols):
    """Batch prices (20 per request) from Yahoo's spark endpoint."""
    res = {}
    for i in range(0, len(symbols), 20):
        chunk = symbols[i:i + 20]
        url = ("https://query1.finance.yahoo.com/v7/finance/spark?range=1d&interval=5m&symbols="
               + urllib.parse.quote(",".join(chunk)))
        data = []
        for attempt in range(3):
            try:
                data = json.loads(http_get(url))["spark"]["result"]
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        for r in data or []:
            try:
                m = r["response"][0]["meta"]
            except (KeyError, IndexError, TypeError):
                continue
            price, prev = m.get("regularMarketPrice"), m.get("previousClose") or m.get("chartPreviousClose")
            if price is not None and prev:
                res[r["symbol"]] = {"price": price, "pct": (price / prev - 1) * 100,
                                    "name": m.get("shortName") or m.get("longName")}
    return res


def clean_name(n):
    n = re.sub(r"\s+", " ", n or "").strip()
    n = re.sub(r",? (Inc\.?|Corporation|Corp\.?|Co\.?|Company|Ltd\.?|plc|PLC|Incorporated|Holdings|Group)$", "", n)
    return n.strip(" ,")


def fetch_sector_movers(cfg):
    """For each sector ETF: its biggest companies, today's move, and how many points of the
    sector's move each one is responsible for (weight x move)."""
    top_n = cfg.get("holdings_per_sector", 12)
    sectors = cfg.get("sectors", [])
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        holdings = dict(zip([s for s, _ in sectors], ex.map(lambda s: fetch_holdings(s[0], top_n), sectors)))
    tickers = sorted({h["ticker"] for hs in holdings.values() for h in hs})
    prices = fetch_spark(tickers) if tickers else {}
    out = {}
    for etf, hs in holdings.items():
        rows = []
        for h in hs:
            p = prices.get(h["ticker"])
            if not p:
                continue
            rows.append({"ticker": h["ticker"], "name": clean_name(p["name"] or h["name"]),
                         "weight": h["weight"], "price": p["price"], "pct": p["pct"],
                         "impact": h["weight"] / 100 * p["pct"]})
        rows.sort(key=lambda r: abs(r["impact"]), reverse=True)
        out[etf] = rows
    return out


# --------------------------------------------------------------------------- the written brief

def by_sym(quotes):
    return {q["symbol"]: q for g in quotes.values() for q in g}


def fmt_pct(p, digits=2):
    if p is None:
        return "n/a"
    if round(abs(p), digits) == 0:
        return f"{0:.{digits}f}%"
    sign = "+" if p > 0 else "−"
    return f"{sign}{abs(p):.{digits}f}%"


def headline(spx_pct, secs, vix, futures_pct=None):
    if futures_pct is not None:
        return f"Futures point {'higher' if futures_pct > 0 else 'lower'} ahead of the open"
    a = abs(spx_pct)
    if a < 0.15:
        h = "Stocks hold steady"
    elif spx_pct > 0:
        h = "Stocks edge higher" if a < 0.5 else ("Stocks rise" if a < 1.5 else "Stocks rally")
    else:
        h = "Stocks edge lower" if a < 0.5 else ("Stocks fall" if a < 1.5 else "Stocks sell off")
    if secs:
        up = sum(1 for q in secs if q["pct"] > 0)
        best = max(secs, key=lambda q: q["pct"])
        worst = min(secs, key=lambda q: q["pct"])
        if up == 0:
            h += f" as all {len(secs)} sectors decline"
        elif up == len(secs):
            h += f" as all {len(secs)} sectors advance"
        elif spx_pct >= 0:
            h += f", led by {best['name']}"
        else:
            h += f", dragged by {worst['name']}"
    if vix and vix.get("pct") is not None and vix["price"] >= 20 and abs(vix["pct"]) >= 5:
        h += f"; VIX {'jumps' if vix['pct'] > 0 else 'eases'} to {vix['price']:.1f}"
    return h


def build_brief(quotes, news, state, holiday, movers=None):
    """Returns (headline, rows, top_stories). rows = [(label, text)], short and scannable."""
    s = by_sym(quotes)
    rows = []
    secs = [q for q in quotes.get("sectors", []) if q["pct"] is not None]
    spx, ndx, dji, rut, vix = (s.get(k) for k in ("^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX"))

    if state == "pre" and s.get("ES=F") and s["ES=F"]["pct"] is not None:
        parts = [f"{q['name']} {fmt_pct(q['pct'])}" for q in quotes.get("futures", []) if q["pct"] is not None]
        rows.append(("Futures", " · ".join(parts)))
        head = headline(0, [], None, futures_pct=s["ES=F"]["pct"])
    elif spx and spx["pct"] is not None:
        parts = [f"S&P 500 {fmt_pct(spx['pct'])} at {spx['price']:,.0f}"]
        for q, n in ((dji, "Dow"), (ndx, "Nasdaq"), (rut, "Russell 2000")):
            if q and q["pct"] is not None:
                parts.append(f"{n} {fmt_pct(q['pct'])}")
        rows.append(("Indices", " · ".join(parts)))
        head = headline(spx["pct"], secs, vix)
        if spx.get("pct_5d") is not None:
            t = [f"{fmt_pct(spx['pct_5d'])} this week"]
            if spx.get("pct_1m") is not None:
                t.append(f"{fmt_pct(spx['pct_1m'])} this month")
            if spx.get("off_high") is not None and spx["off_high"] < -0.5:
                t.append(f"{abs(spx['off_high']):.1f}% below 3-month high")
            rows.append(("Trend", "S&P " + " · ".join(t)))
    else:
        head = "Market data unavailable right now"

    if secs:
        up = sum(1 for q in secs if q["pct"] > 0)
        best = max(secs, key=lambda q: q["pct"])
        worst = min(secs, key=lambda q: q["pct"])
        t = f"{up} of {len(secs)} up · Best: {best['name']} {fmt_pct(best['pct'])} · " \
            f"Worst: {worst['name']} {fmt_pct(worst['pct'])}"
        sp = {q["symbol"]: q["pct"] for q in secs}
        dfn = [sp[k] for k in ("XLP", "XLU", "XLV") if k in sp]
        grw = [sp[k] for k in ("XLK", "XLY", "XLC") if k in sp]
        if dfn and grw:
            spread = sum(dfn) / len(dfn) - sum(grw) / len(grw)
            if spread > 0.75:
                t += " · Risk-off: defensives beating growth"
            elif spread < -0.75:
                t += " · Risk-on: growth beating defensives"
        rows.append(("Sectors", t))

    allm = {r["ticker"]: r for rs in (movers or {}).values() for r in rs}
    if len(allm) >= 4:
        ranked = sorted(allm.values(), key=lambda r: r["pct"])
        ups = [r for r in reversed(ranked) if r["pct"] > 0][:2]
        dns = [r for r in ranked if r["pct"] < 0][:2]
        rows.append(("Big movers", " · ".join(f"{r['name']} {fmt_pct(r['pct'])}" for r in ups + dns)))
    else:
        wl = sorted((q for q in quotes.get("watchlist", []) if q["pct"] is not None), key=lambda q: q["pct"])
        if wl:
            rows.append(("Big movers", f"{wl[-1]['name']} {fmt_pct(wl[-1]['pct'])} · "
                                       f"{wl[0]['name']} {fmt_pct(wl[0]['pct'])}"))

    if vix and vix["price"] is not None:
        lvl = "high stress" if vix["price"] >= 30 else ("elevated" if vix["price"] >= 20 else "calm")
        rows.append(("Volatility", f"VIX {vix['price']:.1f} ({lvl}) · {fmt_pct(vix['pct'], 1)} today"))

    tnx, dxy = s.get("^TNX"), s.get("DX-Y.NYB")
    parts = []
    if tnx and tnx["price"] is not None and tnx["change"] is not None:
        bps = tnx["change"] * 100
        parts.append(f"10-yr yield {tnx['price']:.2f}% ({'+' if bps >= 0 else '−'}{abs(bps):.0f} bps)")
    if dxy and dxy["pct"] is not None:
        parts.append(f"Dollar {fmt_pct(dxy['pct'])}")
    if parts:
        rows.append(("Rates & dollar", " · ".join(parts)))

    oil, gold, btc = s.get("CL=F"), s.get("GC=F"), s.get("BTC-USD")
    parts = []
    if oil and oil["pct"] is not None:
        parts.append(f"Oil ${oil['price']:.2f} ({fmt_pct(oil['pct'])})")
    if gold and gold["pct"] is not None:
        parts.append(f"Gold {fmt_pct(gold['pct'])}")
    if btc and btc["pct"] is not None:
        parts.append(f"Bitcoin {fmt_pct(btc['pct'])}")
    if parts:
        rows.append(("Commodities", " · ".join(parts)))

    top = [(f["source"], f["items"][0]) for f in news if f["items"]][:3]
    return head, rows, top


def brief_lines(head, rows):
    """Plain-text version for the terminal, notifications and phone pushes."""
    return [head] + [f"{l}: {t}" for l, t in rows]


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


def color_nums(text):
    """Color +x% / −x% / bps figures green or red inside already-escaped text."""
    def sub(m):
        c = "up" if m.group(0).startswith("+") else "down"
        return f'<span class="{c}">{m.group(0)}</span>'
    return re.sub(r"[+−]\d[\d,]*(?:\.\d+)?(?:%| bps)", sub, text)


def yf_link(ticker, name):
    """Ticker + company name as one link to its Yahoo Finance quote page."""
    url = "https://finance.yahoo.com/quote/" + urllib.parse.quote(ticker)
    return (f'<a class="co" href="{esc(url)}" target="_blank" rel="noopener" title="{esc(name)} on Yahoo Finance">'
            f'<b>{esc(ticker)}</b> <span class="muted">{esc(name)}</span></a>')


def mover_rows(rows, show_impact):
    if not rows:
        return '<p class="muted">Holdings data unavailable right now.</p>'
    mx = max(abs(r["impact"] if show_impact else r["pct"]) for r in rows) or 1
    out = []
    for r in rows:
        v = r["impact"] if show_impact else r["pct"]
        w = abs(v) / mx * 50
        bar = (f'<span class="bar {cls(v)}" style="{"left" if v >= 0 else "right"}:50%;width:{w:.1f}%"></span>')
        right = (f'{"+" if r["impact"] > 0 else "−" if r["impact"] < 0 else ""}{abs(r["impact"]):.2f} pts'
                 if show_impact else esc(r.get("sector", "")))
        out.append(
            f'<tr><td class="mv-name">{yf_link(r["ticker"], r["name"])}'
            + (f'<span class="wt">{r["weight"]:.1f}% of sector</span>' if show_impact else "")
            + f'</td><td class="num chg {cls(r["pct"])}">{fmt_pct(r["pct"])}</td>'
            f'<td class="mv-bar"><span class="track">{bar}<span class="mid"></span></span></td>'
            f'<td class="num muted mv-imp">{right}</td></tr>')
    return "<table class=\"mv\">" + "".join(out) + "</table>"


def movers_panels(secs, movers):
    names = {q["symbol"]: q["name"] for q in secs}
    pct = {q["symbol"]: q["pct"] for q in secs}
    seen, allrows = set(), []
    for etf, rs in movers.items():
        for r in rs:
            if r["ticker"] not in seen:
                seen.add(r["ticker"])
                allrows.append({**r, "sector": names.get(etf, etf)})
    allrows.sort(key=lambda r: abs(r["pct"]), reverse=True)
    html_out = (f'<div class="mv-panel" data-for="all"><div class="mv-head"><b>Biggest moves among the largest companies</b>'
                f'<span class="muted">Tap a sector to see what is driving it</span></div>'
                f'{mover_rows(allrows[:10], False)}</div>')
    for etf, rs in movers.items():
        html_out += (f'<div class="mv-panel" data-for="{esc(etf)}" hidden><div class="mv-head">'
                     f'<b>{esc(names.get(etf, etf))} <span class="chg {cls(pct.get(etf))}">{fmt_pct(pct.get(etf))}</span></b>'
                     f'<button type="button" class="mv-back">Show all</button></div>'
                     f'<p class="mv-note muted">Top holdings ranked by impact: how many points of the sector\'s move each company accounts for (weight × move).</p>'
                     f'{mover_rows(rs[:10], True)}</div>')
    return html_out


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


def render(cfg, quotes, news, movers, now, state, holiday, label, brief, top, archive_links, is_snapshot=False):
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
        f'<button type="button" class="cell" data-sector="{esc(q["symbol"])}" style="{heat_style(q["pct"])}" '
        f'aria-pressed="false"><span class="c-name">{esc(q["name"])}</span>'
        f'<span class="c-sym">{esc(q["symbol"])}</span><span class="c-pct">{fmt_pct(q["pct"])}</span></button>'
        for q in secs)
    panels = movers_panels(secs, movers or {})

    wl = sorted(quotes["watchlist"], key=lambda q: q["pct"] if q["pct"] is not None else 0, reverse=True)
    rows = "".join(
        f'<tr><td>{yf_link(q["symbol"], q["name"])}</td>'
        f'<td class="num">{fmt_price(q)}</td>'
        f'<td class="num chg {cls(q["pct"])}">{fmt_pct(q["pct"])}</td>'
        f'<td class="sp">{spark(q["series"], q["prev"], 90, 22)}</td></tr>'
        for q in wl)

    head, brows = brief
    brief_html = f'<p class="headline">{esc(head)}</p><dl class="brief-rows">' + "".join(
        f"<dt>{esc(l)}</dt><dd>{color_nums(esc(t))}</dd>" for l, t in brows) + "</dl>"
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
        "{{MOVERS}}": panels,
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
    if shutil.which("osascript"):  # macOS only; skipped on GitHub's Linux servers
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
        remote = pub["remote"]
        token = os.environ.get("GITHUB_TOKEN")  # set inside GitHub Actions
        if token and remote.startswith("https://github.com/"):
            remote = remote.replace("https://", f"https://x-access-token:{token}@", 1)
        git("remote", "add", "origin", remote)
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
    body = brief[0] + "\n\n" + "\n".join(brief[1:])
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


def slot_mode(slot, now):
    """Cloud runs: GitHub's cron is in UTC and can't follow daylight saving, so the workflow
    fires at both possible UTC times and this decides, in Eastern time, whether to run."""
    if now.weekday() >= 5:
        return None
    t = now.hour * 60 + now.minute
    if slot == "am":
        return "briefing" if 9 * 60 + 30 <= t < 11 * 60 + 15 else None
    if slot == "pm":
        return "briefing" if 16 * 60 <= t < 17 * 60 + 45 else None
    return "intraday" if market_state(now) == "open" else None


def cmd_run(args):
    cfg = load_config()
    for d in (OUT, ARCHIVE, DATA, LOGS):
        os.makedirs(d, exist_ok=True)
    now = dt.datetime.now(NY)
    state = market_state(now)
    if args.slot:
        mode = slot_mode(args.slot, now)
        if not mode and not args.force:
            print(f"[{now:%Y-%m-%d %H:%M} ET] slot '{args.slot}' is outside its Eastern-time window; skipping.")
            return
        args.mode = mode or ("intraday" if args.slot == "intraday" else "briefing")

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        fq = ex.submit(fetch_all_quotes, cfg)
        fn = ex.submit(fetch_news, cfg)
        fm = ex.submit(fetch_sector_movers, cfg)
        quotes, news, movers = fq.result(), fn.result(), fm.result()

    spx = by_sym(quotes).get("^GSPC")
    holiday = False
    if state == "open" and spx and spx.get("time"):
        holiday = dt.datetime.fromtimestamp(spx["time"], NY).date() != now.date()

    label = session_label(now, state)
    head, rows, top = build_brief(quotes, news, state, holiday, movers)
    brief = (head, rows)
    lines = brief_lines(head, rows)
    is_briefing = args.mode == "briefing"

    links = archive_list()
    page = render(cfg, quotes, news, movers, now, state, holiday, label, brief, top, links)
    tmp = DASHBOARD + ".tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, DASHBOARD)

    stamp = now.strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(DATA, "latest.json"), "w") as f:
        json.dump({"generated": now.isoformat(), "label": label, "state": state,
                   "brief": lines, "movers": movers, "quotes": quotes, "news": news}, f, indent=1)

    slug = label.replace(" ", "-")
    if is_briefing and not args.force and os.path.isdir(ARCHIVE) and any(
            f.startswith(now.strftime("%Y-%m-%d")) and f.endswith(f"_{slug}.html") for f in os.listdir(ARCHIVE)):
        # Two triggers (cron-job.org and GitHub's backup schedule) can both fire; only the first sends.
        print(f"  {label} already sent today; refreshing the page without a second push")
        is_briefing = False
    if is_briefing:
        snap = render(cfg, quotes, news, movers, now, state, holiday, label, brief, top,
                      [(t, "../" + h if not h.startswith("archive/") else h.replace("archive/", ""))
                       for t, h in links], is_snapshot=True)
        with open(os.path.join(ARCHIVE, f"{stamp}_{slug}.html"), "w") as f:
            f.write(snap)
        shutil.copy(os.path.join(DATA, "latest.json"), os.path.join(DATA, f"{stamp}.json"))
        # re-render so the new snapshot appears in the dashboard's archive list
        page = render(cfg, quotes, news, movers, now, state, holiday, label, brief, top, archive_list())
        with open(DASHBOARD, "w") as f:
            f.write(page)

    n_q = sum(len(v) for v in quotes.values())
    n_n = sum(len(f["items"]) for f in news)
    print(f"[{now:%Y-%m-%d %H:%M} ET] {label}: {n_q} quotes, {n_n} headlines in {time.time() - t0:.1f}s")
    for l in lines:
        print("  " + l)

    if is_briefing and cfg.get("notify_on_briefing", True):
        notify("Market Pulse", label, head)
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
        push_phone(cfg, label, lines, top, force=args.push)
    if args.open or (is_briefing and cfg.get("open_dashboard_on_briefing", True)):
        if sys.platform == "darwin":
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
    r.add_argument("--slot", choices=["am", "pm", "intraday"], help="cloud schedule slot (GitHub Actions)")
    r.add_argument("--force", action="store_true", help="run a --slot even outside its time window")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    a = ap.parse_args()
    {"run": cmd_run, "install": cmd_install, "uninstall": cmd_uninstall,
     "status": cmd_status}.get(a.cmd or "run", cmd_run)(a if a.cmd else ap.parse_args(["run"]))


if __name__ == "__main__":
    main()
