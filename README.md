# Live Scores

> Follow your football clubs, national teams, UFC fighters, rugby and NBA
> teams with live scores, crests, goal scorers, league tables and full UFC
> cards. **Georgia comes first.**

![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB)
![Version](https://img.shields.io/badge/version-2.0.0-7C5CFF)
![Plugin for](https://img.shields.io/badge/plugin%20for-Fenn-111)

A plugin for **Fenn**, the floating notch and taskbar for Windows. No account
or API key, and nothing to install.

| | |
|---|---|
| **Tile** | wide (small and large also supported) |
| **Page** | 600 × 440 |
| **Sports** | Football, UFC, Rugby, NBA |
| **Data** | ESPN and TheSportsDB public feeds |
| **Dependencies** | none |

## Highlights

- **Georgia first.** One click follows the national team, Dinamo Tbilisi or
  the Lelos. The whole **Erovnuli Liga** is covered, and every Georgian
  fighter on a UFC card is followed automatically.
- **Live tile** with your team's crest, score, minute and a red live dot.
- **Match cards** with goal scorers, red cards, venue, countdowns and table
  position.
- **Full UFC cards** with records, live rounds and results by method.
- **League tables** with your team highlighted.
- **Alerts** for goals, kick-off, full time and fight results.

## Install

Clone the repo into Fenn's plugins folder. The folder name must be `live-scores`:

```powershell
git clone https://github.com/Geguchh024/fenn-live-scores "$env:APPDATA\Fenn\plugins\live-scores"
```

You can also download the ZIP and extract it to the same place. Then open
the page and go to **Follow** to pick your teams.

## The tile

The tile shows your team's match with its crest: the live score and minute
with a red dot, the kick-off time, or the last result with W/D/L.

Which team gets the tile:

1. a team that is playing right now
2. otherwise, the team you pinned (the pin on its card)
3. otherwise, the next team to play

Wide tiles wider than about 150 points show both crests. A large tile shows
both crests and the score, plus the next two teams.

## The page

| View | What's there |
|---|---|
| **Matches** | A card per team or fighter, live ones first: crests, score or kick-off time with a countdown, goal scorers and red cards with the minute, the venue, the last result or next fixture, and the table position. |
| **UFC** | Full fight cards (main event first) for the last event and the next four: flags, records, weight class, the live round, and results with the method (KO/TKO R1 4:28, Submission, Decision). Followed and Georgian fighters are in the accent color. |
| **Table** | The league table (or Nations League group) of each team you follow, with your team highlighted. |
| **Follow** | Pick a sport and a league, filter, and click a team to follow it (click again to unfollow). |
| **Settings** | Alerts on goals; on kick-off, full time and fight results; open the panel on an alert; auto-follow Georgian fighters. |

### What you can follow

| Sport | Competitions |
|---|---|
| **Football** | Erovnuli Liga, UEFA national teams (Nations), Premier League, LaLiga, Serie A, Bundesliga, Ligue 1, the three European cups, Süper Lig, Portugal, Eredivisie, Saudi Pro League, MLS |
| **UFC** | Search any fighter, or pick from the next cards |
| **Rugby** | Test matches, Six Nations, World Cup, Top 14, Premiership, URC, Champions Cup |
| **NBA** | All teams |

## Alerts

Fenn has no notification messages, so alerts happen in the notch itself. A
goal, kick-off, full time or a followed fighter's result pulses the collapsed
pill and lights the tile. The page shows the alert, for example
"GOAL! GEO 1–0 HUN · Kvaratskhelia 23'", for 90 seconds. Opening the page
clears it.

## Data and polling

No account and no key are needed:

- **ESPN**'s public JSON (`site.api.espn.com/apis/site/v2/sports/...`): team
  lists, each team's next match and results, the league scoreboard during a
  match (score, minute, scorers, red cards), standings, UFC cards
  (`mma/ufc/scoreboard`), fighters (`common/v3/sports/mma/athletes`) and
  fighter search.
- **TheSportsDB**'s free test key for the Erovnuli Liga, which ESPN doesn't
  cover: each club's next and last match, and the live soccer feed during a
  match. That feed has the score and minute but no scorers, and its table
  shows the top 5 only.

How often it checks:

- every followed team: every 3 hours
- a match: every 30 seconds, from 10 minutes before kick-off until full time
- a UFC card: every 30 seconds while it's on

> **Limitations:** ESPN has no rugby schedules, so a rugby team's last result
> is the one seen live. These are unofficial feeds and can change without
> notice.

Crests, flags and headshots are downloaded once into
`%APPDATA%\Fenn\plugin-data\live-scores\logos`.

## Upgrading from version 1

Teams you followed in version 1 carry over, except cricket, NFL, MLB and NHL,
which were removed.

## Files

| File | What it is |
|---|---|
| `main.py` | Feeds, polling, alerts, the tile and all pages |
| `fenn.py` | The Fenn plugin helper |
| `plugin.toml` | Plugin manifest |

## Requirements

- Windows 10 or 11
- Python 3.8+ on `PATH` as `python`
