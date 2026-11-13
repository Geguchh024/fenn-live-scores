"""Live Scores: football (Georgia's national team and the Erovnuli Liga
included), UFC, rugby and the NBA, with crests, scorers and fight results.

Sources (no account, no key):
- ESPN's public JSON: site.api.espn.com/apis/site/v2/sports/... for team
  lists, each team's next match (team endpoint) and results (schedule), and
  the league scoreboard while a match is on (score, minute, goal scorers,
  red cards). UFC cards come from the mma/ufc scoreboard, fighters from the
  athlete endpoint and ESPN search.
- TheSportsDB's free test key for the Erovnuli Liga, which ESPN doesn't
  cover: next and last match per club, and the live soccer feed during a
  match.

Polling: every 30 s while a followed team or fighter is on (or about to
be), otherwise each team is refreshed every 3 hours. A goal, a kick-off, a
full time or a followed fighter's result pulses the badge and lights the
tile for 90 seconds, since Fenn has no notification message.

Crests, flags and headshots are downloaded once into FENN_DATA_DIR/logos."""

import datetime as dt
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import fenn

SITE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/{path}"
STANDINGS = "https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings"
SEARCH = "https://site.web.api.espn.com/apis/search/v2?query={q}&limit=25&type=player"
ATHLETE = "https://site.web.api.espn.com/apis/common/v3/sports/mma/athletes/{id}"
TSDB = "https://www.thesportsdb.com/api/v1/json/123/{path}"
HEADSHOT = "https://a.espncdn.com/i/headshots/mma/players/full/{id}.png"

EROVNULI = "4638"            # TheSportsDB league id
GEORGIA = "584"              # ESPN: Georgia's national football team
GEORGIA_RUGBY = "81"         # ESPN: Georgia's rugby team (the Lelos)
DINAMO_TBILISI = "134385"    # TheSportsDB

# Follow page: (button label, source, league, national teams)
FOOTBALL = [("Erovnuli Liga", "tsdb", EROVNULI, False), ("Nations", "espn", "uefa.nations", True),
            ("Premier League", "espn", "eng.1", False), ("LaLiga", "espn", "esp.1", False),
            ("Serie A", "espn", "ita.1", False), ("Bundesliga", "espn", "ger.1", False),
            ("Ligue 1", "espn", "fra.1", False), ("Champions L.", "espn", "uefa.champions", False),
            ("Europa L.", "espn", "uefa.europa", False), ("Conference L.", "espn", "uefa.europa.conf", False),
            ("Süper Lig", "espn", "tur.1", False), ("Portugal", "espn", "por.1", False),
            ("Eredivisie", "espn", "ned.1", False), ("Saudi Pro", "espn", "ksa.1", False),
            ("MLS", "espn", "usa.1", False)]
RUGBY = [("Tests", "espn", "289234", True), ("Six Nations", "espn", "180659", True),
         ("World Cup", "espn", "164205", True), ("Top 14", "espn", "270559", False),
         ("Premiership", "espn", "267979", False), ("URC", "espn", "270557", False),
         ("Champions Cup", "espn", "271937", False)]
NBA = [("NBA", "espn", "nba", False)]
SPORTS = [("Football", "soccer", FOOTBALL), ("UFC", "mma", None), ("Rugby", "rugby", RUGBY),
          ("NBA", "basketball", NBA)]
# National football teams play in several competitions; their results are
# merged from these schedules.
INTL = ["uefa.nations", "fifa.friendly", "uefa.euroq", "fifa.worldq.uefa", "uefa.euro", "fifa.world"]
LEAGUE_NAMES = {"uefa.nations": "Nations League", "fifa.friendly": "Friendly", "uefa.euroq": "Euro qualifier",
                "fifa.worldq.uefa": "World Cup qualifier", "uefa.euro": "Euro", "fifa.world": "World Cup",
                "eng.1": "Premier League", "esp.1": "LaLiga", "ita.1": "Serie A", "ger.1": "Bundesliga",
                "fra.1": "Ligue 1", "uefa.champions": "Champions League", "uefa.europa": "Europa League",
                "uefa.europa.conf": "Conference League", "tur.1": "Süper Lig", "por.1": "Primeira Liga",
                "ned.1": "Eredivisie", "ksa.1": "Saudi Pro League", "usa.1": "MLS", "nba": "NBA",
                "289234": "Test match", "180659": "Six Nations", "164205": "Rugby World Cup",
                "270559": "Top 14", "267979": "Premiership", "270557": "URC", "271937": "Champions Cup",
                EROVNULI: "Erovnuli Liga", "ufc": "UFC"}
SPORT_ICON = {"soccer": "soccer-ball", "mma": "boxing-glove", "rugby": "football",
              "basketball": "basketball"}
METHODS = {"kotko": "KO/TKO", "ko": "KO", "tko": "TKO", "submission": "Submission",
           "decision": "Decision", "dq": "DQ", "disqualification": "DQ"}
TSDB_DONE = {"FT", "AET", "PEN", "AOT", "AP", "Match Finished", "FT_PEN"}
TSDB_PRE = {"NS", "TBD", "", "Not Started", "PST", "CANC", "Postponed", "Cancelled"}

NO_TABLE = {"289234", "164205", "fifa.friendly"}   # tests, cups and friendlies have no table
LIVE_EVERY, INFO_EVERY, UFC_EVERY = 30, 3 * 3600, 3 * 3600
SOON = 10 * 60          # start polling this long before kick-off
FLASH_SECONDS = 90

app = fenn.Plugin()
LOGOS = os.path.join(app.data_dir, "logos")
os.makedirs(LOGOS, exist_ok=True)
settings = {"pinned": None, "popup": False, "goals": True, "bookends": True, "georgians": True}
settings.update(app.load("settings", {}))


def migrate():
    """Follows from version 1 (teams.json): football and NBA teams carry
    over; cricket, NFL, MLB and NHL are gone."""
    out = []
    for t in app.load("teams", []) or []:
        if t.get("sport") in ("soccer", "basketball"):
            out.append({"kind": "team", "src": "espn", "sport": t["sport"], "league": t["league"],
                        "id": str(t["id"]), "name": t.get("name", ""), "abbr": t.get("abbr", ""),
                        "logo": "", "national": False})
    return out


follows = app.load("follows", None)
if follows is None:
    follows = migrate()
    app.save("follows", follows)
cache = app.load("cache", {})   # key -> {"next", "last", "standing", "logo", "record", ...} (kept across runs)
info_at = {}                    # key -> time of the last info fetch (this run)
busy = set()                    # request keys in flight
live = {}                       # key -> event being polled live
prev = {}                       # event id -> {"score": (h, a), "state"} for alerts
boards_at = {}                  # (sport, league, date) -> last poll
ufc = {"cards": app.load("ufc", {}), "at": 0.0}   # event id -> {"name","date","bouts"}
lists = {}                      # (src, sport, league) -> [teams] | "loading" | "error"
tables = {}                     # (src, sport, league) -> {"groups": [...]} | "loading" | "error"
search = {"q": "", "results": None}
ui = {"view": 0, "sport": 0, "league": 0, "filter": "", "flash": None, "flash_until": 0.0,
      "flash_key": None, "error": "", "updated": None, "table": 0, "card": None, "fq": 0,
      "page_w": 568, "expanded": set()}
last_sent = {"tile": None, "page": None}


def post(msg):
    app._inbox.put(msg)


def fkey(f):
    return f"{f['src']}:{f['sport']}:{f['id']}"


def spawn(tag, fn, *args):
    """Runs fn(*args) in a thread unless the same tag is already running."""
    if tag in busy:
        return
    busy.add(tag)

    def work():
        try:
            fn(*args)
        except Exception as e:  # network, bad JSON, a changed feed
            print(f"{tag}: {type(e).__name__}: {e}", file=sys.stderr)
            post({"type": "x-failed", "tag": tag, "error": f"{type(e).__name__}: {e}"})
        finally:
            post({"type": "x-done", "tag": tag})
    threading.Thread(target=work, daemon=True).start()


# Fetching (worker threads) ------------------------------------------------------------

def get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Fenn live-scores)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def iso(s):
    """ESPN '2026-11-14T17:00Z', TheSportsDB '2026-10-31T15:00:00' -> aware datetime."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?$", s):
        s += "+00:00"
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def espn_side(c, sport):
    team = c.get("team") or {}
    score = c.get("score")
    if isinstance(score, dict):
        score = score.get("displayValue")
    logo = team.get("logo") or ((team.get("logos") or [{}])[0]).get("href", "")
    return {"id": str(team.get("id") or c.get("id") or ""), "name": team.get("displayName") or "",
            "short": team.get("shortDisplayName") or team.get("displayName") or "",
            "abbr": team.get("abbreviation") or "", "score": "" if score is None else str(score),
            "home": c.get("homeAway") == "home", "winner": c.get("winner"), "logo": logo}


def espn_event(e, sport, league, league_name=""):
    comp = (e.get("competitions") or [{}])[0]
    st = comp.get("status") or e.get("status") or {}
    stype = st.get("type") or {}
    sides = [espn_side(c, sport) for c in comp.get("competitors") or []]
    home = next((s for s in sides if s["home"]), sides[0] if sides else None)
    away = next((s for s in sides if s is not home), None)
    lg = e.get("league") if isinstance(e.get("league"), dict) else {}
    slug = lg.get("slug") or league
    details = []
    for d in comp.get("details") or []:
        if not (d.get("scoringPlay") or d.get("redCard")) or d.get("shootout"):
            continue
        who = (d.get("athletesInvolved") or [{}])[0]
        kind = "red" if d.get("redCard") else "og" if d.get("ownGoal") else "pen" if d.get("penaltyKick") else "goal"
        details.append({"team": str((d.get("team") or {}).get("id", "")), "kind": kind,
                        "player": who.get("shortName") or who.get("displayName") or "",
                        "minute": (d.get("clock") or {}).get("displayValue") or ""})
    return {"id": str(e.get("id")), "src": "espn", "sport": sport, "league": slug,
            "league_name": LEAGUE_NAMES.get(slug) or league_name or lg.get("abbreviation") or "",
            "date": e.get("date") or comp.get("date"), "state": stype.get("state") or "pre",
            "detail": stype.get("shortDetail") or "", "clock": st.get("displayClock") or "",
            "period": st.get("period") or 0, "home": home, "away": away, "details": details,
            "venue": (comp.get("venue") or {}).get("fullName") or ""}


def tsdb_abbr(name, short=""):
    if short:
        return short.upper()[:3]
    words = [w for w in re.split(r"[\s\-]+", name) if w and not w.isdigit() and w.upper() not in ("FC", "FK", "SC")]
    if len(words) >= 2 and words[0] in ("Dinamo", "Torpedo", "Dila"):
        return (words[0][0] + words[1][:2]).upper()
    return (words[0][:3] if words else name[:3]).upper()


def tsdb_event(e, progress=None):
    status = (e.get("strStatus") or "").strip()
    state = "post" if status in TSDB_DONE else "pre" if status in TSDB_PRE else "in"
    hs, as_ = e.get("intHomeScore"), e.get("intAwayScore")
    sides = []
    for k, sc, home in (("Home", hs, True), ("Away", as_, False)):
        badge = e.get(f"str{k}TeamBadge") or ""
        sides.append({"id": str(e.get(f"id{k}Team") or ""), "name": e.get(f"str{k}Team") or "",
                      "short": e.get(f"str{k}Team") or "", "abbr": tsdb_abbr(e.get(f"str{k}Team") or ""),
                      "score": "" if sc in (None, "") else str(sc), "home": home, "winner": None,
                      "logo": (badge + "/small") if badge and not badge.endswith(("/small", "/tiny")) else badge})
    if state == "post" and sides[0]["score"] and sides[1]["score"]:
        h, a = int(sides[0]["score"]), int(sides[1]["score"])
        sides[0]["winner"], sides[1]["winner"] = h > a, a > h
    clock = f"{progress}'" if progress and str(progress).isdigit() else ""
    detail = {"HT": "HT", "PST": "Postponed", "CANC": "Cancelled"}.get(status, status)
    return {"id": str(e.get("idEvent")), "src": "tsdb", "sport": "soccer", "league": str(e.get("idLeague") or EROVNULI),
            "league_name": "Erovnuli Liga" if str(e.get("idLeague")) == EROVNULI else (e.get("strLeague") or ""),
            "date": e.get("strTimestamp") or f"{e.get('dateEvent')}T{e.get('strTime') or '00:00:00'}",
            "state": state, "detail": detail, "clock": clock, "period": 0, "home": sides[0], "away": sides[1],
            "details": [], "venue": e.get("strVenue") or ""}


def fetch_info(f):
    """A team's next match, last result and table position."""
    out = {"type": "x-info", "key": fkey(f), "next": None, "last": None}
    if f["src"] == "tsdb":
        nxt = (get_json(TSDB.format(path=f"eventsnext.php?id={f['id']}")) or {}).get("events") or []
        lst = get_json(TSDB.format(path=f"eventslast.php?id={f['id']}")) or {}
        lst = lst.get("results") or lst.get("events") or []
        out["next"] = tsdb_event(nxt[0]) if nxt else None
        out["last"] = tsdb_event(lst[0]) if lst else None
        post(out)
        return
    sport, league = f["sport"], f["league"]
    team = (get_json(SITE.format(sport=sport, league=league, path=f"teams/{f['id']}")) or {}).get("team") or {}
    out["standing"] = team.get("standingSummary") or ""
    out["logo"] = ((team.get("logos") or [{}])[0]).get("href", "")
    nxt = [espn_event(e, sport, league) for e in team.get("nextEvent") or []]
    nxt = [e for e in nxt if e["state"] != "post"]
    out["next"] = min(nxt, key=lambda e: e["date"] or "") if nxt else None
    if sport == "soccer" and f.get("national"):
        paths = [(lg, f"teams/{f['id']}/schedule") for lg in INTL]
    elif sport == "soccer":
        paths = [("all", f"teams/{f['id']}/schedule")]
    elif sport == "basketball":
        paths = [(league, f"teams/{f['id']}/schedule")]
    else:
        paths = []   # ESPN has no rugby schedules: results are the ones seen live
    done = []
    for lg, path in paths:
        try:
            data = get_json(SITE.format(sport=sport, league=lg, path=path))
        except Exception as e:  # some competitions have no schedule for a team
            print(f"schedule {lg} {f['id']}: {e}", file=sys.stderr)
            continue
        for e in data.get("events") or []:
            ev = espn_event(e, sport, lg if lg != "all" else league)
            if ev["state"] == "post":
                done.append(ev)
    if done:
        out["last"] = max(done, key=lambda e: e["date"] or "")
    post(out)


def fetch_board(sport, league, days, tag):
    """A league's scoreboard for the given days: scores, minute, scorers."""
    evs = []
    for day in days:
        data = get_json(SITE.format(sport=sport, league=league, path=f"scoreboard?dates={day}"))
        name = ((data.get("leagues") or [{}])[0]).get("name") or ""
        evs += [espn_event(e, sport, league, name) for e in data.get("events") or []]
    post({"type": "x-board", "tag": tag, "events": evs})


def fetch_tsdb_live(tag):
    data = get_json(TSDB.format(path="livescore.php?s=Soccer")) or {}
    rows = data.get("livescore") or []
    post({"type": "x-board", "tag": tag, "events": [tsdb_event(r, r.get("strProgress")) for r in rows
                                                     if str(r.get("idLeague")) == EROVNULI]})


def fetch_fighter(f):
    a = (get_json(ATHLETE.format(id=f["id"])) or {})
    ath = a.get("athlete") or {}
    rec = next((s.get("displayValue") for s in (ath.get("statsSummary") or {}).get("statistics") or []
                if s.get("name") == "wins-losses-draws"), "")
    last = None
    emap = a.get("eventsMap") or {}
    now = dt.datetime.now(dt.timezone.utc)
    for uid in a.get("events") or []:
        e = emap.get(uid) or {}
        when = iso((e.get("gameDate") or "").replace(".000+00:00", "Z"))
        if e.get("gameResult") and when and when < now:
            opp = e.get("opponent") or {}
            last = {"name": e.get("shortName") or e.get("name") or "", "date": e.get("gameDate"),
                    "result": e.get("gameResult"), "opponent": opp.get("displayName") or ""}
            break
    post({"type": "x-fighter", "key": fkey(f), "record": rec,
          "country": (ath.get("flag") or {}).get("alt") or ath.get("citizenship") or "",
          "flag": (ath.get("flag") or {}).get("href") or "",
          "weight": (ath.get("weightClass") or {}).get("text") or "", "last_fight": last})


def bouts(e):
    comps = e.get("competitions") or []
    out = []
    for i, c in enumerate(comps):
        st = c.get("status") or {}
        stype = st.get("type") or {}
        sides = []
        for x in sorted(c.get("competitors") or [], key=lambda x: x.get("order", 0)):
            a = x.get("athlete") or {}
            flag = a.get("flag") or {}
            name = a.get("displayName") or ""
            sides.append({"id": str(x.get("id")), "name": name, "short": a.get("shortName") or name,
                          "abbr": name.split()[-1] if name else "", "score": "", "home": not sides,
                          "winner": x.get("winner"), "logo": HEADSHOT.format(id=x.get("id")),
                          "flag": flag.get("href") or "", "country": flag.get("alt") or "",
                          "record": ((x.get("records") or [{}])[0]).get("summary", "")})
        if len(sides) < 2:
            continue
        method = ""
        for d in c.get("details") or []:
            t = ((d.get("type") or {}).get("text") or "")
            if t.startswith("Unofficial Winner"):
                m = t[len("Unofficial Winner"):].strip()
                method = METHODS.get(m.lower(), m.title())
        out.append({"id": str(c.get("id")), "src": "espn", "sport": "mma", "league": "ufc",
                    "league_name": e.get("shortName") or e.get("name") or "UFC",
                    "event_id": str(e.get("id")), "event_name": e.get("name") or "",
                    "date": c.get("date") or e.get("date"), "state": stype.get("state") or "pre",
                    "detail": stype.get("shortDetail") or "", "clock": st.get("displayClock") or "",
                    "period": st.get("period") or 0, "weight": (c.get("type") or {}).get("abbreviation") or "",
                    "rounds": ((c.get("format") or {}).get("regulation") or {}).get("periods") or 3,
                    "home": sides[0], "away": sides[1], "method": method, "main": i == len(comps) - 1,
                    "order": i, "details": [], "venue": (c.get("venue") or {}).get("fullName") or ""})
    return out


def fetch_ufc(days):
    """UFC cards: the default scoreboard (this week's event) plus the given
    days (YYYYMMDD); with days=None, the last event and the next four from
    the season calendar."""
    full = days is None
    first = get_json(SITE.format(sport="mma", league="ufc", path="scoreboard"))
    cards = {}
    cal = []
    for item in ((first.get("leagues") or [{}])[0]).get("calendar") or []:
        ref = ((item.get("event") or {}).get("$ref") or "")
        m = re.search(r"/events/(\d+)", ref)
        cal.append({"id": m.group(1) if m else "", "name": item.get("label") or "",
                    "date": item.get("startDate") or ""})

    def add(data):
        for e in data.get("events") or []:
            cards[str(e["id"])] = {"name": e.get("name") or "", "short": e.get("shortName") or "",
                                   "date": e.get("date") or "", "bouts": bouts(e)}
    add(first)
    if days is None:   # a full refresh: the last event and the next four
        now = dt.datetime.now(dt.timezone.utc)
        real = [c for c in cal if "Contender Series" not in c["name"] and iso(c["date"])]
        past = [c for c in real if iso(c["date"]) < now - dt.timedelta(hours=10)][-1:]
        future = [c for c in real if iso(c["date"]) >= now - dt.timedelta(hours=10)][:4]
        days = sorted({d for c in past + future for d in espn_days(iso(c["date"]))})
    for d in days:
        try:
            add(get_json(SITE.format(sport="mma", league="ufc", path=f"scoreboard?dates={d}")))
        except Exception as e:
            print(f"ufc {d}: {e}", file=sys.stderr)
    post({"type": "x-ufc", "cards": cards, "full": full})


def fetch_teams(src, sport, league):
    out = []
    if src == "tsdb":
        name = urllib.parse.quote("Georgian Erovnuli Liga")
        for t in (get_json(TSDB.format(path=f"search_all_teams.php?l={name}")) or {}).get("teams") or []:
            badge = t.get("strBadge") or ""
            out.append({"id": str(t["idTeam"]), "name": t.get("strTeam") or "",
                        "abbr": tsdb_abbr(t.get("strTeam") or ""),
                        "logo": badge + "/small" if badge else ""})
    else:
        data = get_json(SITE.format(sport=sport, league=league, path="teams"))
        for s in data.get("sports") or []:
            for lg in s.get("leagues") or []:
                for item in lg.get("teams") or []:
                    tm = item.get("team") or {}
                    out.append({"id": str(tm.get("id")), "name": tm.get("displayName") or "",
                                "abbr": tm.get("abbreviation") or "",
                                "logo": ((tm.get("logos") or [{}])[0]).get("href", "")})
    post({"type": "x-teams", "key": (src, sport, league), "teams": sorted(out, key=lambda x: x["name"])})


def fetch_search(q):
    data = get_json(SEARCH.format(q=urllib.parse.quote(q)))
    found = []
    for r in data.get("results") or []:
        for c in r.get("contents") or []:
            uid = c.get("uid") or ""
            if c.get("sport") == "mma" and "~a:" in uid:
                found.append({"id": uid.split("~a:")[1], "name": c.get("displayName") or ""})
    post({"type": "x-search", "q": q, "results": found[:12]})


def fetch_table(src, sport, league):
    groups = []
    if src == "tsdb":
        season = str(dt.date.today().year)
        rows = (get_json(TSDB.format(path=f"lookuptable.php?l={league}&s={season}")) or {}).get("table") or []
        groups.append({"name": "Erovnuli Liga", "rows": [
            {"id": str(r.get("idTeam")), "rank": int(r.get("intRank") or 0), "name": r.get("strTeam") or "",
             "logo": (r.get("strBadge") or "").replace("/tiny", "/small"),
             "p": r.get("intPlayed"), "w": r.get("intWin"), "d": r.get("intDraw"), "l": r.get("intLoss"),
             "gd": r.get("intGoalDifference"), "pts": r.get("intPoints")} for r in rows], "partial": True})
    else:
        data = get_json(STANDINGS.format(sport=sport, league=league))
        for ch in data.get("children") or [data]:
            entries = ((ch.get("standings") or {}).get("entries")) or []
            rows = []
            for e in entries:
                st = {s.get("name"): s.get("displayValue") for s in e.get("stats") or []}
                tm = e.get("team") or {}
                rows.append({"id": str(tm.get("id")), "name": tm.get("shortDisplayName") or tm.get("displayName") or "",
                             "logo": ((tm.get("logos") or [{}])[0]).get("href", ""),
                             "rank": int(re.sub(r"\D", "", st.get("rank") or st.get("playoffSeed") or "0") or 0),
                             "p": st.get("gamesPlayed"), "w": st.get("wins"), "d": st.get("ties"),
                             "l": st.get("losses"), "gd": st.get("pointDifferential"), "pts": st.get("points"),
                             "pct": st.get("winPercent"), "gb": st.get("gamesBehind")})
            if rows:
                if not any(r["rank"] for r in rows):
                    for i, r in enumerate(rows):
                        r["rank"] = i + 1
                groups.append({"name": ch.get("name") or "", "rows": sorted(rows, key=lambda r: r["rank"])})
    post({"type": "x-table", "key": (src, sport, league), "groups": groups})


# Logos ------------------------------------------------------------------------------------

logo_queue = queue.Queue()
logo_state = {}   # url -> path | "pending" | ("failed", time)


def sized(url, kind):
    if url.startswith("https://a.espncdn.com/i/"):
        w, h = (96, 70) if kind == "head" else (64, 64)
        return f"https://a.espncdn.com/combiner/i?img={url[len('https://a.espncdn.com'):]}&w={w}&h={h}"
    return url


def logo(url, kind="crest"):
    """Local path of a cached image, or None while it downloads."""
    if not url:
        return None
    st = logo_state.get(url)
    if isinstance(st, str) and st != "pending":
        return st
    if st is None:
        path = os.path.join(LOGOS, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            logo_state[url] = path
            return path
        logo_state[url] = "pending"
        logo_queue.put((url, kind, path))
    elif isinstance(st, tuple) and time.time() - st[1] > 3600:
        del logo_state[url]
    return None


def logo_worker():
    while True:
        url, kind, path = logo_queue.get()
        try:
            req = urllib.request.Request(sized(url, kind), headers={"User-Agent": "Mozilla/5.0 (Fenn live-scores)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if not data.startswith((b"\x89PNG", b"\xff\xd8")):
                raise ValueError("not an image")
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
            post({"type": "x-logo", "url": url, "path": path})
        except Exception as e:
            print(f"logo {url}: {e}", file=sys.stderr)
            post({"type": "x-logo", "url": url, "path": None})


for _ in range(3):
    threading.Thread(target=logo_worker, daemon=True).start()


# Handling results (main thread) -----------------------------------------------------------

def find(key):
    return next((f for f in follows if fkey(f) == key), None)


def num(score):
    m = re.match(r"\s*(\d+)", score or "")
    return int(m.group(1)) if m else None


def save_cache():
    app.save("cache", cache)


@app.on_message("x-done")
def on_done(msg):
    busy.discard(msg["tag"])


@app.on_message("x-failed")
def on_failed(msg):
    tag = msg["tag"]
    if tag.startswith("teams:"):
        lists[tuple(tag[6:].split("|"))] = "error"
    elif tag.startswith("table:"):
        tables[tuple(tag[6:].split("|"))] = "error"
    elif tag.startswith("search:"):
        search["results"] = "error"
    elif tag.startswith("info:"):
        info_at[tag[5:]] = time.time() - INFO_EVERY + 300   # try again in 5 minutes
    ui["error"] = "Can't reach the scores feed right now." if "URLError" in msg["error"] else ""
    draw()


@app.on_message("x-info")
def on_info(msg):
    key = msg["key"]
    c = cache.setdefault(key, {})
    if key not in live:
        c["next"] = msg["next"]
    if msg["last"] and (not c.get("last") or (msg["last"]["date"] or "") >= (c["last"]["date"] or "")):
        c["last"] = msg["last"]
    for k in ("standing", "logo"):
        if msg.get(k) is not None:
            c[k] = msg[k]
    f = find(key)
    if f and msg.get("logo") and not f.get("logo"):
        f["logo"] = msg["logo"]
        app.save("follows", follows)
    ui["updated"] = time.strftime("%H:%M")
    ui["error"] = ""
    save_cache()
    draw()


@app.on_message("x-fighter")
def on_fighter(msg):
    c = cache.setdefault(msg["key"], {})
    c.update({k: msg[k] for k in ("record", "country", "flag", "weight", "last_fight")})
    save_cache()
    draw()


def alert(text, key, short):
    ui["flash"], ui["flash_until"], ui["flash_key"] = text, time.monotonic() + FLASH_SECONDS, key
    ui["flash_short"] = short
    app.badge_pulse()
    app.active(True)
    if settings["popup"]:
        app.show()


def mine(key, ev):
    """(my side, their side) of an event for a followed team."""
    f = find(key)
    me_id = f["id"] if f else ""
    if ev["away"] and ev["away"]["id"] == me_id:
        return ev["away"], ev["home"]
    return ev["home"], ev["away"]


def watch(key, ev):
    """Compares a fresh event with what was seen before; alerts on changes."""
    old = prev.get(ev["id"])
    score = (num(ev["home"]["score"]) if ev["home"] else None, num(ev["away"]["score"]) if ev["away"] else None)
    prev[ev["id"]] = {"score": score, "state": ev["state"]}
    if not old:
        return
    me, them = mine(key, ev)
    line = f"{ev['home']['abbr']} {ev['home']['score']}–{ev['away']['score']} {ev['away']['abbr']}"
    if old["state"] == "pre" and ev["state"] == "in" and settings["bookends"]:
        alert(f"Kick-off · {me['abbr']} vs {them['abbr']}", key, "Kick-off")
    if ev["sport"] == "soccer" and settings["goals"] and None not in score and None not in old["score"]:
        for i, side in enumerate((ev["home"], ev["away"])):
            if score[i] > old["score"][i]:
                scorer = next((d for d in reversed(ev["details"]) if d["team"] == side["id"]
                               and d["kind"] != "red"), None)
                who = f" · {scorer['player']} {scorer['minute']}" if scorer and scorer["player"] else ""
                alert(f"GOAL! {line}{who}", key, "GOAL!")
    if old["state"] != "post" and ev["state"] == "post" and settings["bookends"]:
        alert(f"Full time · {line}", key, "Full time")


@app.on_message("x-board")
def on_board(msg):
    by_id = {e["id"]: e for e in msg["events"]}
    boards_at[msg["tag"]] = time.monotonic()
    for f in follows:
        if f["kind"] != "team":
            continue
        key = fkey(f)
        c = cache.setdefault(key, {})
        cur = live.get(key) or c.get("next")
        if not cur or cur["id"] not in by_id:
            continue
        ev = by_id[cur["id"]]
        if not ev.get("league_name"):
            ev["league_name"] = cur.get("league_name", "")
        watch(key, ev)
        if ev["state"] == "post":
            live.pop(key, None)
            c["last"], c["next"] = ev, None
            info_at[key] = time.time() - INFO_EVERY + 120   # fetch the next fixture shortly
        else:
            live[key] = ev
            c["next"] = ev
    ui["updated"] = time.strftime("%H:%M")
    save_cache()
    draw()


@app.on_message("x-ufc")
def on_ufc(msg):
    if msg.get("full"):
        old, ufc["cards"] = ufc["cards"], msg["cards"]
        for eid, card in old.items():   # a card that is on keeps being shown
            if any(b["state"] == "in" for b in card["bouts"]):
                ufc["cards"].setdefault(eid, card)
    else:
        ufc["cards"].update(msg["cards"])
    keep = sorted(ufc["cards"].items(), key=lambda kv: kv[1]["date"] or "")[-8:]
    ufc["cards"] = dict(keep)
    for card in ufc["cards"].values():
        for b in card["bouts"]:
            key = watched_bout(b)
            if key:
                old = prev.get(b["id"])
                prev[b["id"]] = {"state": b["state"], "score": (None, None)}
                if old and old["state"] != "post" and b["state"] == "post" and settings["bookends"]:
                    me, them = fighter_side(b, key)
                    alert(bout_result_text(b, key), key,
                          "Won!" if me.get("winner") else "Lost" if them.get("winner") else "Final")
                elif old and old["state"] == "pre" and b["state"] == "in" and settings["bookends"]:
                    alert(f"Fight on · {b['home']['abbr']} vs {b['away']['abbr']}", key, "Fight on")
    app.save("ufc", ufc["cards"])
    ui["updated"] = time.strftime("%H:%M")
    draw()


@app.on_message("x-teams")
def on_teams(msg):
    lists[tuple(msg["key"])] = msg["teams"]
    draw()


@app.on_message("x-table")
def on_table(msg):
    tables[tuple(msg["key"])] = msg
    draw()


@app.on_message("x-search")
def on_search(msg):
    if msg["q"] == search["q"]:
        search["results"] = msg["results"]
    draw()


@app.on_message("x-logo")
def on_logo(msg):
    logo_state[msg["url"]] = msg["path"] or ("failed", time.time())
    if msg["path"]:
        draw()


# Scheduling ------------------------------------------------------------------------------

def espn_days(when):
    """ESPN files scoreboards under the US date: an evening kick-off in UTC
    can sit on the day before, so early UTC hours ask for both days."""
    u = when.astimezone(dt.timezone.utc)
    days = [u.strftime("%Y%m%d")]
    if u.hour < 8:
        days.insert(0, (u - dt.timedelta(days=1)).strftime("%Y%m%d"))
    return days


def due_live(ev, now):
    """Whether to poll a match every 30 s: it's on, or starts within 10
    minutes. A match still 'on' 4 hours after kick-off is stale."""
    if not ev or ev["state"] == "post":
        return False
    w = iso(ev["date"])
    if not w or (now - w).total_seconds() > 4 * 3600:
        return False
    return ev["state"] == "in" or (w - now).total_seconds() <= SOON


def stale(ev, now):
    """Kicked off over 2¼ hours ago and still not final: the feed dropped
    it, so ask the team endpoint again soon."""
    w = iso(ev["date"]) if ev else None
    return bool(w) and ev["state"] != "post" and (now - w).total_seconds() > 8100


def poll(force=False):
    now = dt.datetime.now(dt.timezone.utc)
    mono = time.monotonic()
    boards = {}
    for f in follows:
        key = fkey(f)
        ev = live.get(key) or (cache.get(key) or {}).get("next")
        every = 600 if f["kind"] == "team" and stale(ev, now) else INFO_EVERY
        if force or time.time() - info_at.get(key, 0) > every:
            info_at[key] = time.time()
            if f["kind"] == "fighter":
                spawn(f"info:{key}", fetch_fighter, dict(f))
            else:
                spawn(f"info:{key}", fetch_info, dict(f))
        if f["kind"] != "team" or not due_live(ev, now):
            continue
        if ev["src"] == "tsdb":
            boards["tsdb-live"] = (fetch_tsdb_live,)
        else:
            days = espn_days(iso(ev["date"]))
            tag = f"board:{ev['sport']}|{ev['league']}|{days[-1]}"
            boards[tag] = (fetch_board, ev["sport"], ev["league"], days)
    for tag, (fn, *args) in boards.items():
        if force or mono - boards_at.get(tag, -1e9) >= LIVE_EVERY - 1:
            boards_at[tag] = mono
            spawn(tag, fn, *args, tag)
    if wants_ufc():
        card_live = [c for c in ufc["cards"].values() if card_due(c, now)]
        if force or time.time() - ufc["at"] > UFC_EVERY:
            ufc["at"] = time.time()
            spawn("ufc", fetch_ufc, None)
        elif card_live and mono - boards_at.get("ufc-live", -1e9) >= LIVE_EVERY - 1:
            boards_at["ufc-live"] = mono
            spawn("ufc-live", fetch_ufc, sorted({d for c in card_live for d in espn_days(iso(c["date"]))}))


def card_due(card, now):
    states = {b["state"] for b in card["bouts"]}
    if "in" in states:
        return True
    w = iso(card["date"])
    return bool(w) and "pre" in states and (w - now).total_seconds() <= SOON and \
        (now - w).total_seconds() < 9 * 3600


def wants_ufc():
    return settings["georgians"] or any(f["kind"] == "fighter" for f in follows) or ui["view"] == 1


def tick():
    if ui["flash"] and time.monotonic() > ui["flash_until"]:
        clear_flash()
    poll()
    draw()


# UFC helpers ------------------------------------------------------------------------------

def watched_bout(b):
    """The follow key that makes a bout interesting, or None."""
    ids = {b["home"]["id"], b["away"]["id"]}
    for f in follows:
        if f["kind"] == "fighter" and f["id"] in ids:
            return fkey(f)
    if settings["georgians"] and "Georgia" in (b["home"]["country"], b["away"]["country"]):
        side = b["home"] if b["home"]["country"] == "Georgia" else b["away"]
        return f"espn:mma:{side['id']}"
    return None


def fighter_side(b, key):
    fid = key.split(":")[-1]
    return (b["home"], b["away"]) if b["home"]["id"] == fid else (b["away"], b["home"])


def bout_result_text(b, key):
    me, them = fighter_side(b, key)
    how = bout_how(b)
    if me.get("winner"):
        return f"{me['abbr']} wins · {how}"
    if them.get("winner"):
        return f"{me['abbr']} loses to {them['abbr']} · {how}"
    return f"{me['abbr']} vs {them['abbr']} · {b['detail']}"


def bout_how(b):
    if b["method"] == "Decision":
        return "Decision"
    if b["method"]:
        return f"{b['method']} R{b['period']} {b['clock']}".strip()
    return b["detail"]


def fighter_bouts(key):
    """Bouts on known cards for a fighter (or a Georgian fighter's key)."""
    fid = key.split(":")[-1]
    out = []
    for card in ufc["cards"].values():
        for b in card["bouts"]:
            if fid in (b["home"]["id"], b["away"]["id"]):
                out.append(b)
    return sorted(out, key=lambda b: b["date"] or "")


def georgian_bouts():
    """Bouts with a Georgian fighter that are coming up or ended in the last 20 hours."""
    out = []
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)
    for card in ufc["cards"].values():
        for b in card["bouts"]:
            w = iso(b["date"])
            if "Georgia" in (b["home"]["country"], b["away"]["country"]) and w and \
                    (b["state"] != "post" or w > since):
                out.append(b)
    return sorted(out, key=lambda b: b["date"] or "")


# What to show ------------------------------------------------------------------------------

def current(key):
    """The event to show for a follow: live, a result from the last 14 h,
    the next one, or the last one."""
    f = find(key)
    now = dt.datetime.now(dt.timezone.utc)
    if f and f["kind"] == "fighter":
        bs = fighter_bouts(key)
        for b in bs:
            if b["state"] == "in":
                return b
        recent = [b for b in bs if b["state"] == "post" and iso(b["date"]) and now - iso(b["date"]) < dt.timedelta(hours=20)]
        upcoming = [b for b in bs if b["state"] == "pre"]
        return (recent[-1] if recent else None) or (upcoming[0] if upcoming else None) or (bs[-1] if bs else None)
    c = cache.get(key) or {}
    if key in live:
        return live[key]
    last, nxt = c.get("last"), c.get("next")
    lw = iso(last["date"]) if last else None
    if last and lw and now - lw < dt.timedelta(hours=14):
        return last
    return nxt or last


def is_live(ev):
    return bool(ev) and ev["state"] == "in"


def entries():
    """Follows plus Georgian bouts, ordered: live, then soonest."""
    out = [(fkey(f), f) for f in follows]
    known = {k for k, _ in out}
    if settings["georgians"]:
        for b in georgian_bouts():
            side = b["home"] if b["home"]["country"] == "Georgia" else b["away"]
            k = f"espn:mma:{side['id']}"
            if k not in known:
                known.add(k)
                out.append((k, {"kind": "fighter", "src": "espn", "sport": "mma", "league": "ufc",
                                "id": side["id"], "name": side["name"], "abbr": side["abbr"],
                                "logo": side["logo"], "auto": True}))
    now = dt.datetime.now(dt.timezone.utc)

    def rank(kf):
        ev = current(kf[0]) if find(kf[0]) else auto_current(kf[0])
        if is_live(ev):
            return (0, "")
        if ev and ev["state"] == "pre":
            return (1, ev["date"] or "")
        if ev and ev["state"] == "post" and iso(ev["date"]) and now - iso(ev["date"]) < dt.timedelta(hours=14):
            return (0.5, "")
        return (2, "")
    return sorted(out, key=rank)


def auto_current(key):
    bs = fighter_bouts(key)
    for b in bs:
        if b["state"] == "in":
            return b
    upcoming = [b for b in bs if b["state"] == "pre"]
    return upcoming[0] if upcoming else (bs[-1] if bs else None)


def show_ev(key):
    return current(key) if find(key) else auto_current(key)


def primary():
    ents = entries()
    if not ents:
        return None, None
    pinned = next(((k, f) for k, f in ents if k == settings["pinned"]), None)
    lives = [(k, f) for k, f in ents if is_live(show_ev(k))]
    if ui["flash"] and ui["flash_until"] > time.monotonic():
        hit = next(((k, f) for k, f in ents if k == ui["flash_key"]), None)
        if hit:
            return hit
    if lives and not (pinned and pinned in lives):
        return lives[0]
    return pinned or ents[0]


# Formatting ---------------------------------------------------------------------------------

def when_text(date, short=False):
    w = iso(date)
    if not w:
        return ""
    local = w.astimezone()
    today = dt.date.today()
    hm = local.strftime("%H:%M")
    if local.date() == today:
        return hm if short else f"Today {hm}"
    if local.date() == today + dt.timedelta(days=1):
        return f"Tmrw {hm}" if short else f"Tomorrow {hm}"
    if local.date() == today - dt.timedelta(days=1):
        return "Yesterday"
    if 0 < (local.date() - today).days < 7:
        return local.strftime("%a ") + hm
    return local.strftime("%d %b").lstrip("0") if short else local.strftime("%a %d %b ").replace(" 0", " ") + hm


def countdown(date):
    """'in 40 min', 'in 5 h', 'in 3 days'."""
    w = iso(date)
    if not w:
        return ""
    secs = (w - dt.datetime.now(dt.timezone.utc)).total_seconds()
    if secs < 60:
        return "Starting"
    if secs < 3600:
        return f"in {int(secs // 60)} min"
    if secs < 36 * 3600:
        return f"in {int(round(secs / 3600))} h"
    return f"in {int(round(secs / 86400))} days"


def status_text(ev, short=False):
    """'67'', 'HT', 'FT', 'Sat 21:00', 'R2 3:12'."""
    if ev["state"] == "pre":
        if ev["detail"] in ("Postponed", "Cancelled", "Canceled"):
            return ev["detail"]
        return when_text(ev["date"], short)
    if ev["sport"] == "mma":
        if ev["state"] == "in":
            return f"R{ev['period']} {ev['clock']}".strip() if ev["period"] else "Live"
        return bout_how(ev)
    if ev["state"] == "in":
        if ev["sport"] == "soccer":
            if ev["detail"] in ("HT", "Halftime"):
                return "HT"
            return ev["clock"] if ev["clock"] and ev["clock"] not in ("0'", "") else (ev["detail"] or "Live")
        return ev["detail"] or "Live"
    if ev["detail"] in ("", "Final", "Full Time"):
        return "FT"
    return ev["detail"]


def outcome(me, them):
    if me.get("winner"):
        return "W"
    if them and them.get("winner"):
        return "L"
    if me.get("score") and them and me["score"] == them["score"]:
        return "D"
    return ""


def line_for(key, f):
    """(title, subtitle, live) for a follow, as plain text."""
    ev = show_ev(key)
    if f["kind"] == "fighter":
        if not ev:
            c = cache.get(key) or {}
            lf = c.get("last_fight")
            sub = f"Last: {lf['result']} vs {lf['opponent']}" if lf else "No fight booked"
            return f["name"], sub, False
        me, them = fighter_side(ev, key)
        if ev["state"] == "post":
            return f"{me['abbr']} vs {them['abbr']}", bout_result_text(ev, key), False
        return f"{me['abbr']} vs {them['abbr']}", f"{status_text(ev)} · {ev['league_name']}", is_live(ev)
    if not ev or not ev.get("home"):
        return f["name"], "No match scheduled", False
    me, them = mine(key, ev)
    if ev["state"] == "pre":
        return f"{me['abbr'] or me['name']} vs {them['abbr'] if them else ''}", status_text(ev), False
    score = f"{me['abbr']} {me['score']}–{them['score'] if them else ''} {them['abbr'] if them else ''}"
    sub = status_text(ev)
    if ev["state"] == "post":
        o = outcome(me, them)
        sub = f"{o} · {sub}" if o else sub
    return score, sub, is_live(ev)


def side_logo(side, sport, size, fighter=False):
    """A crest, flag or headshot image, or the sport's icon until it loads."""
    if fighter:
        p = logo(side.get("logo"), "head")
        if p:
            return fenn.image(path=p, width=round(size * 96 / 70), height=size)
    else:
        p = logo(side.get("logo"))
        if p:
            return fenn.image(path=p, width=size, height=size)
    return fenn.icon(SPORT_ICON.get(sport, "trophy"), size=size * 0.75, color="faint")


def flag(side, size=14):
    p = logo(side.get("flag"))
    return fenn.image(path=p, width=size, height=size) if p else fenn.spacer(size)


# Tile -------------------------------------------------------------------------------------------

def tile_view():
    key, f = primary()
    size = app.tile_size or "wide"
    w = app.tile_width or (104 if size != "small" else 44)
    if not f:
        kids = [fenn.icon("trophy", size=22, color="dim")]
        if size != "small":
            kids.append(fenn.text("Pick your teams", size="small", color="dim"))
        return fenn.column(kids, gap=2, align="center", justify="center", tooltip="Click to choose teams")
    ev = show_ev(key)
    flash = ui["flash"] if ui["flash_until"] > time.monotonic() and ui["flash_key"] == key else None
    tip = "\n".join(" · ".join(line_for(k, x)[:2]) for k, x in entries()[:8])
    fighter = f["kind"] == "fighter"
    lv = is_live(ev)
    if not ev or not ev.get("home"):
        img = side_logo({"logo": f.get("logo")}, f["sport"], 22 if size == "small" else 26, fighter)
        if size == "small":
            return fenn.column([img], align="center", justify="center", tooltip=tip)
        return fenn.row([img, fenn.column([fenn.text(f["abbr"] or f["name"], bold=True),
                                           fenn.text(line_for(key, f)[1], size="small", color="dim")], gap=0)],
                        gap=8, align="center", tooltip=tip)
    me, them = fighter_side(ev, key) if fighter else mine(key, ev)
    st = status_text(ev, short=True)
    st_color = "red" if lv else "dim"
    if fighter:
        big = f"vs {them['abbr']}" if ev["state"] != "post" else ("Won" if me.get("winner") else "Lost" if them.get("winner") else "Final")
        small = st
    elif ev["state"] == "pre":
        big, small = f"vs {them['abbr']}" if them else "", st
    else:
        big = f"{me['score']}–{them['score']}" if them else me["score"]
        small = f"{st} · {them['abbr']}" if them and size != "large" else st
        if ev["state"] == "post":
            o = outcome(me, them)
            small = f"{o} · {small}" if o else small
    if flash:
        small, st_color = ui.get("flash_short") or "Alert", "accent"
    if size == "small":
        return fenn.column([side_logo(me, ev["sport"], 22, fighter),
                            fenn.text(big if ev["state"] != "pre" or fighter else st, size="small", bold=True,
                                      color="accent" if flash else ("red" if lv else "text"))],
                           gap=1, align="center", justify="center", tooltip=tip)
    if size == "large":
        hw = 30
        home, away = (ev["home"], ev["away"])
        mid = f"{home['score']}–{away['score']}" if ev["state"] != "pre" and not fighter else "vs"
        rows = [fenn.row([side_logo(home, ev["sport"], hw, fighter), fenn.spacer(),
                          fenn.text(mid, size="title", color="accent" if flash else "text"), fenn.spacer(),
                          side_logo(away, ev["sport"], hw, fighter)], gap=2, align="center"),
                fenn.row([fenn.text(home["abbr"], size="small", color="dim"), fenn.spacer(),
                          fenn.text(small if flash else st, size="small", bold=True,
                                    color="accent" if flash else st_color), fenn.spacer(),
                          fenn.text(away["abbr"], size="small", color="dim")], gap=2, align="center")]
        others = [(k, x) for k, x in entries() if k != key][:2]
        for k, x in others:
            t, s, l = line_for(k, x)
            rows += [fenn.divider(), fenn.row([fenn.text(t, size="small", bold=True), fenn.spacer(),
                                               fenn.text(s.split(" · ")[0], size="small",
                                                         color="red" if l else "dim")], gap=4)]
        return fenn.column(rows, gap=3, justify="center", tooltip=tip)
    if w >= 150 and not fighter:
        home, away = ev["home"], ev["away"]
        mid = f"{home['score']}–{away['score']}" if ev["state"] != "pre" else "vs"
        return fenn.column([
            fenn.row([side_logo(home, ev["sport"], 20), fenn.text(home["abbr"], bold=True), fenn.spacer(),
                      fenn.text(mid, size="title", color="accent" if flash else "text"), fenn.spacer(),
                      fenn.text(away["abbr"], bold=True), side_logo(away, ev["sport"], 20)], gap=4, align="center"),
            fenn.row([fenn.spacer(), fenn.text(small if flash else st, size="small", bold=lv or bool(flash),
                                               color="accent" if flash else st_color), fenn.spacer()]),
        ], gap=1, justify="center", tooltip=tip)
    return fenn.row([
        side_logo(me, ev["sport"], 26, fighter),
        fenn.column([fenn.text(big, size="title" if not fighter and ev["state"] != "pre" else "body", bold=True,
                               color="accent" if flash else "text"),
                     fenn.row(([fenn.icon("circle", size=7, color="red", fill=True)] if lv and not flash else []) +
                              [fenn.text(small, size="small", color="accent" if flash else st_color, bold=lv)],
                              gap=3, align="center")], gap=0),
    ], gap=7, align="center", tooltip=tip)


# Page ---------------------------------------------------------------------------------------------

VIEWS = ["Matches", "UFC", "Table", "Follow", "Settings"]


def match_card(key, f):
    ev = show_ev(key)
    c = cache.get(key) or {}
    k = key
    pinned = settings["pinned"] == k
    fighter = f["kind"] == "fighter"
    tools = [fenn.button(f"pin:{k}", icon="push-pin", primary=pinned,
                         tooltip="On the tile" if pinned else "Keep on the tile")]
    if not f.get("auto"):
        tools.append(fenn.button(f"del:{k}", icon="x", tooltip="Unfollow"))
    else:
        tools.append(fenn.button(f"follow:{k}", icon="star", tooltip="Follow this fighter"))
    if not ev or not ev.get("home"):
        sub = c.get("standing") or ""
        if fighter:
            lf = c.get("last_fight")
            sub = " · ".join(x for x in (c.get("record"), c.get("weight"),
                                         f"last: {lf['result']} vs {lf['opponent']}" if lf else "") if x)
        if not sub and key not in cache and f"info:{key}" in busy:
            sub = "Loading…"
        return fenn.card([fenn.row([side_logo({"logo": f.get("logo")}, f["sport"], 28, fighter),
                                    fenn.column([fenn.text(f["name"], bold=True),
                                                 fenn.text(sub or ("No fight booked" if fighter else "No match scheduled"),
                                                           size="small", color="dim")], gap=0),
                                    fenn.spacer(), *tools], gap=10, align="center")], padding=10)
    lv = is_live(ev)
    st = countdown(ev["date"]) if ev["state"] == "pre" else status_text(ev)
    league = ev.get("league_name") or LEAGUE_NAMES.get(ev["league"], "")
    if fighter:
        league = " · ".join(x for x in (ev.get("league_name"), ev.get("weight"),
                                        "Main event" if ev.get("main") else "") if x)
    head = fenn.row([
        fenn.icon(SPORT_ICON.get(ev["sport"], "trophy"), size=14, color="red" if lv else "dim"),
        fenn.text(league, size="small", color="dim"),
        fenn.spacer(),
        *([fenn.icon("circle", size=8, color="red", fill=True)] if lv else []),
        fenn.text(st, size="small", bold=True, color="red" if lv else "dim"),
        *tools,
    ], gap=6, align="center")
    home, away = ev["home"], ev["away"]

    def side(s, right):
        name_col = [fenn.text(s["short"] if len(s["name"]) > 18 else s["name"],
                              bold=bool(s.get("winner")) or not ev["state"] == "post")]
        if fighter:
            name_col.append(fenn.row([flag(s, 12), fenn.text(s.get("record", ""), size="small", color="dim")],
                                     gap=4, align="center"))
        col = fenn.column(name_col, gap=0, align="end" if right else "start")
        img = side_logo(s, ev["sport"], 34 if fighter else 30, fighter)
        return fenn.row([fenn.spacer(), col, img] if right else [img, col], gap=8, align="center")
    if fighter:
        mid = [fenn.text("vs" if ev["state"] != "post" else
                         ("def." if home.get("winner") else "lost to" if away.get("winner") else "vs"),
                         size="title", color="dim")]
        if ev["state"] == "post" and ev.get("method"):
            mid.append(fenn.text(bout_how(ev), size="small", color="dim"))
        elif ev["state"] == "pre":
            mid.append(fenn.text(f"{ev.get('rounds', 3)} rounds", size="small", color="faint"))
    elif ev["state"] == "pre":
        w = iso(ev["date"])
        day = when_text(ev["date"]).rsplit(" ", 1)[0] if w else ""
        mid = [fenn.text(w.astimezone().strftime("%H:%M") if w else "vs", size="title"),
               fenn.text(status_text(ev) if ev["detail"] in ("Postponed", "Cancelled") else day,
                         size="small", color="dim")]
    else:
        mid = [fenn.text(f"{home['score']}  –  {away['score']}", size="big", color="red" if lv else "text")]
    body = [head, fenn.grid(3, [side(home, False), fenn.column(mid, gap=0, align="center", justify="center"),
                                side(away, True)], gap=4)]
    # Goal scorers and red cards
    if ev["details"] and expanded(k, ev):
        cols = []
        for s in (home, away):
            lines = [f"{'🟥' if d['kind'] == 'red' else '⚽'} {d['player']} {d['minute']}"
                     + (" (pen)" if d["kind"] == "pen" else " (og)" if d["kind"] == "og" else "")
                     for d in ev["details"] if d["team"] == s["id"]]
            cols.append(fenn.column([fenn.text(x, size="small", color="dim") for x in lines], gap=1,
                                    align="end" if s is away else "start"))
        body.append(fenn.grid(2, cols, gap=8))
    left, right = "", []
    if not fighter:
        last, nxt = c.get("last"), c.get("next")
        if ev["state"] == "pre" and last and last.get("home") and last["id"] != ev["id"]:
            me, them = mine(key, last)
            o = outcome(me, them)
            right.append(f"Last: {o + ' ' if o else ''}{me['score']}–{them['score']} vs {them['short']}")
        elif ev["state"] == "post" and nxt and nxt.get("home") and nxt["id"] != ev["id"]:
            me, them = mine(key, nxt)
            right.append(f"Next: vs {them['short']} · {when_text(nxt['date'])}")
        if c.get("standing"):
            right.append(c["standing"])
        left = ev.get("venue") if ev["state"] == "pre" else ""
    else:
        left = ev.get("event_name") or ""
    foot = []
    if left:
        foot += [fenn.icon("map-pin" if not fighter else "calendar-blank", size=12, color="faint"),
                 fenn.text(left, size="small", color="faint")]
    if right:
        foot += [fenn.spacer(), fenn.text(" · ".join(right), size="small", color="faint")]
    if foot:
        body.append(fenn.row(foot, gap=4, align="center"))
    return fenn.card(body, padding=10, gap=6)


def expanded(k, ev):
    return ev["sport"] == "soccer"


def matches_view():
    ents = entries()
    if not ents:
        return [fenn.card([
            fenn.row([fenn.icon("soccer-ball", size=28, color="accent"),
                      fenn.column([fenn.text("Follow your teams", size="title"),
                                   fenn.text("Football (Georgia and the Erovnuli Liga too), UFC, rugby and the NBA.",
                                             size="small", color="dim", wrap=True)], gap=2)], gap=10, align="center"),
            fenn.row([fenn.button("quick:georgia", label="Georgia", icon="plus", primary=True),
                      fenn.button("quick:dinamo", label="Dinamo Tbilisi", icon="plus"),
                      fenn.button("quick:lelos", label="Georgia rugby", icon="plus"),
                      fenn.button("goadd", label="More", icon="magnifying-glass")], gap=6),
        ], padding=12, gap=10)]
    return [match_card(k, f) for k, f in ents]


def ufc_view():
    cards = sorted(ufc["cards"].items(), key=lambda kv: kv[1]["date"] or "")
    if not cards:
        if "ufc" not in busy:
            ufc["at"] = 0
        return [fenn.text("Loading UFC cards…", color="dim")]
    now = dt.datetime.now(dt.timezone.utc)
    if ui["card"] not in ufc["cards"]:
        # This week's (or the next) event
        upcoming = [eid for eid, cd in cards if iso(cd["date"]) and now - iso(cd["date"]) < dt.timedelta(hours=10)]
        ui["card"] = upcoming[0] if upcoming else cards[-1][0]
    ids = [eid for eid, _ in cards]
    labels = [("● " if any(b["state"] == "in" for b in cd["bouts"]) else "") +
              (iso(cd["date"]).astimezone().strftime("%d %b").lstrip("0") if iso(cd["date"]) else "?")
              for _, cd in cards]
    card = ufc["cards"][ui["card"]]
    out = [fenn.chips(f"card:{len(ids)}", labels, selected=ids.index(ui["card"]))]
    states = {b["state"] for b in card["bouts"]}
    st = "LIVE" if "in" in states else ("Final" if states == {"post"} else when_text(card["date"]))
    out.append(fenn.row([fenn.icon("boxing-glove", size=20, color="red" if st == "LIVE" else "accent"),
                         fenn.column([fenn.text(card["name"], bold=True),
                                      fenn.text(" · ".join(x for x in (st, (card["bouts"] or [{}])[-1].get("venue", "")) if x),
                                                size="small", color="red" if st == "LIVE" else "dim")], gap=0)],
                        gap=8, align="center"))
    followed = {f["id"] for f in follows if f["kind"] == "fighter"}
    rows = []
    for b in reversed(card["bouts"]):   # main event first
        mark = lambda s: s["id"] in followed or (settings["georgians"] and s["country"] == "Georgia")

        def name(s, right):
            col = "accent" if mark(s) else ("text" if b["state"] != "post" or s.get("winner") else "dim")
            parts = [flag(s, 14), fenn.text(s["name"], bold=bool(s.get("winner")) or mark(s), color=col)]
            if right:
                parts = parts[::-1]
            return fenn.column([fenn.row(([fenn.spacer()] if right else []) + parts, gap=5, align="center"),
                                fenn.text(s["record"], size="small", color="faint")], gap=0,
                               align="end" if right else "start")
        lv = b["state"] == "in"
        mid = [fenn.text(b["weight"] + (" · Main" if b["main"] else ""), size="small", color="dim")]
        if b["state"] == "post":
            mid.append(fenn.text(bout_how(b), size="small", bold=True))
        elif lv:
            mid.append(fenn.text(f"● R{b['period']} {b['clock']}", size="small", bold=True, color="red"))
        rows.append(fenn.grid(3, [name(b["home"], False), fenn.column(mid, gap=0, align="center"),
                                  name(b["away"], True)], gap=4))
    if rows:
        out.append(fenn.card([x for r in rows for x in (r, fenn.divider())][:-1], padding=10, gap=6))
    else:
        out.append(fenn.text("The card hasn't been announced yet.", color="dim"))
    out.append(fenn.text("Accent names are fighters you follow" +
                         (" and Georgian fighters." if settings["georgians"] else "."),
                         size="small", color="faint"))
    return out


def table_targets():
    seen, out = set(), []
    for f in follows:
        if f["kind"] != "team":
            continue
        lg = f["league"]
        if f["sport"] == "soccer" and f.get("national"):
            lg = "uefa.nations"
        if lg in NO_TABLE:
            continue
        t = (f["src"], f["sport"], lg)
        if t not in seen:
            seen.add(t)
            out.append((t, f))
    return out


def table_view():
    targets = table_targets()
    if not targets:
        return [fenn.text("Follow a team to see its league table.", color="dim")]
    i = min(ui["table"], len(targets) - 1)
    out = [fenn.chips(f"table:{len(targets)}", [LEAGUE_NAMES.get(t[2], t[2]) for t, _ in targets], selected=i)]
    t, f = targets[i]
    data = tables.get(t)
    if data is None:
        tables[t] = "loading"
        spawn("table:" + "|".join(t), fetch_table, *t)
        data = "loading"
    if data == "loading":
        return out + [fenn.text("Loading the table…", color="dim")]
    if data == "error" or not data.get("groups"):
        return out + [fenn.text("No table for this competition right now.", color="dim"),
                      fenn.button("retry-table", label="Try again", icon="arrow-clockwise")]
    mine_ids = {x["id"] for x in follows if x["kind"] == "team" and x["src"] == t[0]}
    group = next((g for g in data["groups"] if any(r["id"] in mine_ids for r in g["rows"])), data["groups"][0])
    basket = t[1] == "basketball"
    head = "  W   L    PCT   GB" if basket else " P   W  D  L   GD  Pts"
    rows = [fenn.row([fenn.text(group["name"], size="small", color="dim", bold=True), fenn.spacer(),
                      fenn.text(head, size="small", color="faint", mono=True)], gap=6)]
    for r in group["rows"]:
        me = r["id"] in mine_ids
        if basket:
            nums = f"{r['w'] or '':>3} {r['l'] or '':>3}  {r['pct'] or '':>5} {r['gb'] or '':>4}"
        else:
            nums = f"{r['p'] or '':>2} {r['w'] or '':>3} {r['d'] or '':>2} {r['l'] or '':>2} {r['gd'] or '':>4} {r['pts'] or '':>4}"
        p = logo(r["logo"])
        rows.append(fenn.row([
            fenn.text(f"{r['rank']:>2}", size="small", color="accent" if me else "dim", mono=True),
            fenn.image(path=p, width=16, height=16) if p else fenn.icon(SPORT_ICON.get(t[1], "trophy"), size=14, color="faint"),
            fenn.text(r["name"], bold=me, color="accent" if me else "text"),
            fenn.spacer(),
            fenn.text(nums, size="small", mono=True, bold=me, color="accent" if me else "text"),
        ], gap=8, align="center"))
    out.append(fenn.card(rows, padding=10, gap=4))
    if group.get("partial") or data["groups"][0].get("partial"):
        out.append(fenn.text("TheSportsDB's free feed shows the top 5 only.", size="small", color="faint"))
    return out


def follow_view():
    label, sport, leagues = SPORTS[ui["sport"]]
    out = [fenn.chips("sport", [s[0] for s in SPORTS], selected=ui["sport"])]
    following = {fkey(f) for f in follows}
    if sport == "mma":
        out.append(fenn.toggle("georgians", settings["georgians"],
                               label="Follow every Georgian fighter on UFC cards"))
        out.append(fenn.text_input(f"fsearch:{ui['fq']}", search["q"], hint="Search a fighter, e.g. Topuria, then Enter"))
        items = []
        if search["q"]:
            res = search["results"]
            if res is None:
                out.append(fenn.text("Searching…", color="dim"))
            elif res == "error":
                out.append(fenn.text("Search failed. Try again.", color="red"))
            elif not res:
                out.append(fenn.text("No UFC fighters found.", color="dim"))
            else:
                for r in res:
                    k = f"espn:mma:{r['id']}"
                    items.append(fenn.item(f"addf:{r['id']}:{r['name']}", r["name"],
                                           icon="check-circle" if k in following else "plus-circle",
                                           trailing="Following" if k in following else None,
                                           selected=k in following))
        else:
            seen = set()
            for card in sorted(ufc["cards"].values(), key=lambda c: c["date"] or ""):
                if card["bouts"] and all(b["state"] == "post" for b in card["bouts"]):
                    continue
                for b in reversed(card["bouts"]):
                    for s, o in ((b["home"], b["away"]), (b["away"], b["home"])):
                        if s["id"] in seen:
                            continue
                        seen.add(s["id"])
                        k = f"espn:mma:{s['id']}"
                        items.append(fenn.item(f"addf:{s['id']}:{s['name']}", s["name"],
                                               subtitle=f"vs {o['name']} · {card['short'] or card['name']} · {s['country']}",
                                               icon="check-circle" if k in following else "plus-circle",
                                               trailing="Following" if k in following else when_text(card["date"], True),
                                               selected=k in following))
            if items:
                out.append(fenn.text("On the next cards", size="small", color="dim", bold=True))
        if items:
            out.append(fenn.list_view(items[:60]))
        return out
    idx = min(ui["league"], len(leagues) - 1)
    if len(leagues) > 1:
        out.append(fenn.grid(5, [fenn.button(f"lg:{i}", label=l[0], primary=i == idx, width=0)
                                 for i, l in enumerate(leagues)], gap=4))
    _, src, league, national = leagues[idx]
    out.append(fenn.text_input("filter", ui["filter"], hint="Filter teams", live=True))
    lk = (src, sport, league)
    lst = lists.get(lk)
    if lst is None:
        lists[lk] = "loading"
        spawn("teams:" + "|".join(lk), fetch_teams, *lk)
        lst = "loading"
    if lst == "loading":
        return out + [fenn.text("Loading teams…", color="dim")]
    if lst == "error":
        return out + [fenn.text("Couldn't load teams.", color="red"),
                      fenn.button("retry", label="Try again", icon="arrow-clockwise")]
    q = ui["filter"].lower().strip()
    items = []
    star = {GEORGIA, GEORGIA_RUGBY, DINAMO_TBILISI}
    for x in sorted(lst, key=lambda x: (x["id"] not in star, x["name"])):
        if q and q not in x["name"].lower() and q not in x["abbr"].lower():
            continue
        k = f"{src}:{sport}:{x['id']}"
        items.append(fenn.item(f"add:{src}|{sport}|{league}|{int(national)}|{x['id']}", x["name"],
                               subtitle=x["abbr"] or None,
                               icon="check-circle" if k in following else "plus-circle",
                               trailing="Following" if k in following else None, selected=k in following))
    out.append(fenn.list_view(items[:100]) if items else fenn.text("No teams match.", color="dim"))
    return out


def settings_view():
    return [
        fenn.toggle("goals", settings["goals"], label="Alert on goals"),
        fenn.toggle("bookends", settings["bookends"], label="Alert on kick-off, full time and fight results"),
        fenn.toggle("popup", settings["popup"], label="Open the panel on an alert"),
        fenn.toggle("georgians", settings["georgians"], label="Follow every Georgian fighter on UFC cards"),
        fenn.divider(),
        fenn.text("Alerts pulse the pill and light the tile for 90 seconds. Scores poll every 30 s while one "
                  "of yours is on, otherwise every 3 hours.", size="small", color="faint", wrap=True),
        fenn.text("Data: ESPN's public feeds (football, UFC, rugby, NBA) and TheSportsDB's free feed for the "
                  "Erovnuli Liga, which has no live minute or scorers. No account or key.",
                  size="small", color="faint", wrap=True),
    ]


def page_view():
    body = [fenn.row([fenn.chips("view", VIEWS, selected=ui["view"]), fenn.spacer(),
                      fenn.text(f"Updated {ui['updated']}" if ui["updated"] else "", size="small", color="faint")],
                     align="center")]
    if ui["error"]:
        body.append(fenn.row([fenn.icon("wifi-slash", size=14, color="red"),
                              fenn.text(ui["error"], size="small", color="red")], gap=6, align="center"))
    if ui["flash"] and ui["flash_until"] > time.monotonic():
        body.append(fenn.card([fenn.row([fenn.icon("bell-ringing", color="accent", fill=True),
                                         fenn.text(ui["flash"], bold=True), fenn.spacer(),
                                         fenn.button("dismiss", icon="x", tooltip="Dismiss")],
                                        gap=8, align="center")], padding=8))
    body += (matches_view, ufc_view, table_view, follow_view, settings_view)[ui["view"]]()
    return fenn.column(body, gap=8, scroll=True, id=f"v{ui['view']}")


ACTIONS = [{"id": "refresh", "icon": "arrow-clockwise", "tooltip": "Refresh now"}]


def draw(force=False):
    tile, page = tile_view(), page_view()
    kt, kp = json.dumps(tile, sort_keys=True), json.dumps(page, sort_keys=True)
    if force or kt != last_sent["tile"]:
        app.tile(tile)
        last_sent["tile"] = kt
    if force or kp != last_sent["page"]:
        app.page(page, ACTIONS)
        last_sent["page"] = kp


# Events --------------------------------------------------------------------------------------------

def follow_team(src, sport, league, national, tid, name="", abbr="", logo_url=""):
    k = f"{src}:{sport}:{tid}"
    old = find(k)
    if old:
        follows.remove(old)
        cache.pop(k, None)
        live.pop(k, None)
        if settings["pinned"] == k:
            settings["pinned"] = None
    else:
        if not name:
            lst = lists.get((src, sport, league))
            x = next((x for x in lst if x["id"] == tid), None) if isinstance(lst, list) else None
            name, abbr, logo_url = (x["name"], x["abbr"], x["logo"]) if x else ("Team", "", "")
        follows.append({"kind": "team", "src": src, "sport": sport, "league": league, "id": tid,
                        "name": name, "abbr": abbr or name[:3].upper(), "logo": logo_url, "national": national})
        info_at.pop(k, None)
    app.save("follows", follows)
    app.save("settings", settings)
    poll()


def follow_fighter(fid, name):
    k = f"espn:mma:{fid}"
    old = find(k)
    if old:
        follows.remove(old)
        cache.pop(k, None)
    else:
        follows.append({"kind": "fighter", "src": "espn", "sport": "mma", "league": "ufc", "id": fid,
                        "name": name, "abbr": name.split()[-1] if name else "", "logo": HEADSHOT.format(id=fid)})
        info_at.pop(k, None)
    app.save("follows", follows)
    poll()


QUICK = {"georgia": ("espn", "soccer", "uefa.nations", True, GEORGIA, "Georgia", "GEO",
                     "https://a.espncdn.com/i/teamlogos/countries/500/geo.png"),
         "dinamo": ("tsdb", "soccer", EROVNULI, False, DINAMO_TBILISI, "Dinamo Tbilisi", "DTB",
                    "https://r2.thesportsdb.com/images/media/team/badge/mtef0m1781222095.png/small"),
         "lelos": ("espn", "rugby", "289234", True, GEORGIA_RUGBY, "Georgia", "GEO",
                   "https://a.espncdn.com/i/teamlogos/rugby/teams/500/81.png")}


@app.on_message("event")
def on_event(e):
    eid, value = e.get("id", ""), e.get("value")
    idx = value.get("index", 0) if isinstance(value, dict) else 0
    if eid == "view":
        ui["view"] = idx
        if idx == 1:
            poll()
    elif eid == "sport":
        ui["sport"], ui["league"], ui["filter"] = idx, 0, ""
    elif eid.startswith("lg:"):
        ui["league"], ui["filter"] = int(eid[3:]), ""
    elif eid.startswith("table:"):
        ui["table"] = idx
    elif eid.startswith("card:"):
        ids = [k for k, _ in sorted(ufc["cards"].items(), key=lambda kv: kv[1]["date"] or "")]
        ui["card"] = ids[idx] if idx < len(ids) else None
    elif eid == "filter":
        ui["filter"] = value or ""
    elif eid.startswith("fsearch:") and e.get("kind") == "submit":
        q = (value or "").strip()
        search["q"], search["results"] = q, None
        if q:
            spawn(f"search:{q}", fetch_search, q)
    elif eid == "goadd":
        ui["view"] = 3
    elif eid.startswith("quick:"):
        q = QUICK[eid[6:]]
        if not find(f"{q[0]}:{q[1]}:{q[4]}"):
            follow_team(*q)
    elif eid == "retry":
        label, sport, leagues = SPORTS[ui["sport"]]
        _, src, league, _ = leagues[min(ui["league"], len(leagues) - 1)]
        lists.pop((src, sport, league), None)
    elif eid == "retry-table":
        targets = table_targets()
        if targets:
            tables.pop(targets[min(ui["table"], len(targets) - 1)][0], None)
    elif eid == "refresh":
        ui["error"] = ""
        tables.clear()
        poll(force=True)
    elif eid == "dismiss":
        clear_flash()
    elif eid in ("popup", "goals", "bookends", "georgians"):
        settings[eid] = bool(value)
        app.save("settings", settings)
        if eid == "georgians" and value:
            poll()
    elif eid.startswith("add:"):
        src, sport, league, national, tid = eid[4:].split("|", 4)
        follow_team(src, sport, league, national == "1", tid)
    elif eid.startswith("addf:"):
        fid, name = eid[5:].split(":", 1)
        follow_fighter(fid, name)
    elif eid.startswith("follow:"):
        fid = eid.split(":")[-1]
        side = next((s for b in fighter_bouts(eid[7:]) for s in (b["home"], b["away"]) if s["id"] == fid), None)
        follow_fighter(fid, side["name"] if side else "Fighter")
    elif eid.startswith("pin:"):
        settings["pinned"] = None if settings["pinned"] == eid[4:] else eid[4:]
        app.save("settings", settings)
    elif eid.startswith("del:"):
        f = find(eid[4:])
        if f:
            follows.remove(f)
            cache.pop(eid[4:], None)
            live.pop(eid[4:], None)
            if settings["pinned"] == eid[4:]:
                settings["pinned"] = None
                app.save("settings", settings)
            app.save("follows", follows)
            save_cache()
    draw()


def clear_flash():
    ui["flash"], ui["flash_until"], ui["flash_key"] = None, 0.0, None
    app.clear_badge()
    app.active(False)


@app.on_message("page_open")
def on_page_open(_):
    if ui["flash"]:
        clear_flash()
    draw()


@app.on_message("layout")
def on_layout(msg):
    if msg.get("target") == "page" and msg.get("width"):
        ui["page_w"] = msg["width"]
    draw(force=msg.get("target") == "tile")


@app.on_message("theme")
def on_theme(_):
    draw(force=True)


@app.on_message("shutdown")
def on_shutdown(_):
    save_cache()


draw(force=True)
poll()
app.every(5, tick)
app.run()
