#!/usr/bin/env python3
"""Is this iCal feed alive? For the discovery skill's triage step.

`verify_ical_feed` only says a feed parses. A feed can parse and still be worth
nothing: Meetup's feed lists only upcoming events, so a dormant group returns a
valid, empty calendar; and one organizer often cross-lists the same online series
under several city-named groups. This prints, per feed, how many events are
coming up, the next two (date, title, location), and flags the patterns above.

    python3 .claude/skills/discovery/probe_feeds.py mywauug opensearch-project-washington-dc
    python3 .claude/skills/discovery/probe_feeds.py https://api.lu.ma/ics/get?entity=calendar&id=cal-XXXX

A bare word is treated as a Meetup group slug. Needs `icalendar` (a calgen
dependency, so it is present wherever calgen's tests run). Read-only; makes only
GET requests to the feeds you name.

Reading the output:
  [City, ST] OUT / CHECK
                  the group's home city, read from its Meetup page. OUT is a
                  state other than DC, VA or MD (a Georgia group listing
                  online events is not a DC group); CHECK is a VA or MD town
                  outside the DC metro list below, so look before proposing
                  (Frederick, Columbia, Baltimore). Blank for non-Meetup feeds.
  up=0            nothing scheduled: put it on the watchlist, don't propose it
  ERR HTTP 403    private or invite-only group: its feed can't be aggregated
  identical titles+dates across several slugs
                  one national organizer cross-listing an online series under
                  city names: the organizing group isn't DC-area, skip
  location blank  Meetup omits LOCATION for online events and often for
                  in-person ones too; the aggregator decides virtual vs physical
                  from each event page's JSON-LD, so blank here proves nothing
"""
import concurrent.futures as cf
import datetime
import re
import sys
import urllib.request

from icalendar import Calendar

USER_AGENT = "Mozilla/5.0 (dctech.events discovery)"


def feed_url(arg):
    if arg.startswith("http"):
        return arg
    return f"https://www.meetup.com/{arg}/events/ical/"


# DC metro, as in calendar-qc's out-of-area rule: DC; Northern Virginia; the
# Montgomery and Prince George's county suburbs.
METRO = {
    "washington", "arlington", "alexandria", "fairfax", "reston", "tysons",
    "tysons corner", "mclean", "vienna", "herndon", "ashburn", "sterling",
    "leesburg", "manassas", "falls church", "springfield", "chantilly",
    "centreville", "burke", "woodbridge", "dulles", "mclean", "oakton",
    "bethesda", "silver spring", "rockville", "gaithersburg", "germantown",
    "potomac", "chevy chase", "takoma park", "college park", "greenbelt",
    "hyattsville", "laurel", "bowie", "upper marlboro", "riverdale", "beltsville",
    "national harbor", "oxon hill", "clinton", "kensington", "wheaton",
}


def home(arg):
    """'City, ST' from a Meetup group page, with OUT / CHECK flags, or ''."""
    if arg.startswith("http") and "meetup.com" not in arg:
        return ""
    slug = arg if not arg.startswith("http") else arg.split("meetup.com/")[1].split("/")[0]
    try:
        req = urllib.request.Request(f"https://www.meetup.com/{slug}/",
                                     headers={"User-Agent": USER_AGENT})
        page = urllib.request.urlopen(req, timeout=25).read().decode("utf8", "replace")
    except Exception:  # noqa: BLE001 - the feed result matters more than this
        return ""
    m = re.search(r'"city":"([^"]+)","state":"([^"]*)"', page)
    if not m:
        return ""
    city, state = m.group(1), m.group(2).upper()
    flag = ""
    if state not in ("DC", "VA", "MD"):
        flag = " OUT"
    elif state != "DC" and city.lower() not in METRO:
        flag = " CHECK"
    return f"[{city}, {state}]{flag}"


def probe(arg):
    try:
        req = urllib.request.Request(feed_url(arg), headers={"User-Agent": USER_AGENT})
        cal = Calendar.from_ical(urllib.request.urlopen(req, timeout=25).read())
    except Exception as exc:  # noqa: BLE001 - report any failure per feed
        return arg, {"error": str(exc)[:70]}
    today = datetime.date.today()
    events = []
    for comp in cal.walk("VEVENT"):
        start = comp.get("DTSTART").dt
        start = start.date() if hasattr(start, "date") else start
        events.append((start, str(comp.get("SUMMARY", ""))[:60], str(comp.get("LOCATION", ""))[:60]))
    events.sort()
    upcoming = [e for e in events if e[0] >= today]
    return arg, {"total": len(events), "upcoming": upcoming, "home": home(arg)}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    with cf.ThreadPoolExecutor(8) as pool:
        results = dict(pool.map(probe, argv))
    signatures = {}
    for arg in argv:
        r = results[arg]
        if "error" in r:
            print(f"{arg:60} ERR {r['error']}")
            continue
        nxt = " | ".join(f"{d} {t}" + (f" @ {loc}" if loc else "") for d, t, loc in r["upcoming"][:2])
        print(f"{arg:60} {r['home']:28} up={len(r['upcoming']):<3} {nxt}")
        if r["upcoming"]:
            signatures.setdefault(tuple((d, t) for d, t, _ in r["upcoming"][:5]), []).append(arg)
    for sig, args in signatures.items():
        if len(args) > 1:
            print(f"\nNOTE identical upcoming events in: {', '.join(args)}"
                  " (likely one organizer cross-listing a series)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
