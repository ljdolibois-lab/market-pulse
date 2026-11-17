# Market Pulse

A personal markets dashboard that briefs me twice every trading day.

- **Morning briefing, 9:45 AM ET** (8:45 AM Central), 15 minutes after the open: a Mac notification plus the dashboard opens
- **Intraday refresh** every 30 minutes while the market is open, with no notification
- **Closing briefing, 4:15 PM ET** (3:15 PM Central), after the close: a notification plus the dashboard
- Every briefing is saved to `output/archive/` so I can look back at how a week played out

**Live dashboard:** https://ljdolibois-lab.github.io/market-pulse/ (republished on every run)

## Phone push
The morning briefing is also sent to my phone through the free **ntfy** app. Tapping the notification opens the live dashboard. The ntfy topic and publishing settings live in `local_settings.json`, which is kept off GitHub. To get the closing briefing on your phone too, set `push_on` to `["morning", "closing"]`. To send a test push, run `python3 market_pulse.py run --push`.

## What's on it
- **The Brief**: a plain-English summary written automatically from the data. It covers index moves, how many sectors are up, sector leaders and laggards, risk-on vs. risk-off rotation, VIX, the 10-year yield in basis points, oil, gold, the dollar and bitcoin
- Major indices (S&P 500, Dow, Nasdaq, Russell 2000, VIX) with intraday charts against the prior close
- A heatmap of all 11 S&P sectors (SPDR ETFs)
- Rates, the dollar and commodities; index futures; global markets (Nikkei, Hang Seng, FTSE, DAX)
- Headlines from the **Wall Street Journal**, **Yahoo Finance**, **MarketWatch** and **CNBC**
- A watchlist of mega-caps and banks, sorted by move

## Commands
```bash
python3 market_pulse.py run --open      # refresh now and open it
python3 market_pulse.py install         # schedule it (Mon–Fri)
python3 market_pulse.py status          # check the schedule and last runs
python3 market_pulse.py uninstall       # remove the schedule
```
To change symbols, news sources or times, edit `config.json`. After changing times, run `install` again.

## How it works
- Python, standard library only
- Prices come from Yahoo Finance's chart endpoint (5-minute bars). News comes from RSS feeds. The fetches run in parallel and the whole run takes about 2 seconds
- A rule-based engine turns the numbers into the written brief
- The page is a static HTML file with no server. It reloads itself every 5 minutes, so an open tab stays current
- macOS `launchd` handles the schedule. If the Mac is asleep at a scheduled time, the run happens when it wakes

## Interview talking points
> "I built a small program that runs on my laptop every trading day. At 9:45 it pulls the indices, sectors, Treasury yields, oil and gold, plus the WSJ and Yahoo Finance headlines. It writes me a short brief on what's moving and why, and sends me a notification. It refreshes through the day and sends a closing recap after 4. It also archives every briefing, so on Friday I can look back at how the week developed."

Things to be ready to discuss from your own dashboard: what the 10-year yield did and why it matters for equities, which sectors led (growth vs. defensives), where the VIX sits, and one or two headlines you read.
