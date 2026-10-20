"""Job Radar: checks remote job feeds, keeps fresh GTM and marketing roles open
to Nigeria, skips jobs already sent, and alerts Telegram. Runs on GitHub Actions.

Modes:
  python radar.py jobs    -> check feeds and micro1, send new matches
  python radar.py report  -> send the daily application count
  python radar.py test    -> send one test message to Telegram
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

FEEDS = [
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/remote-jobs.rss",
    "https://remotive.com/remote-jobs/feed/marketing",
    "https://remoteok.com/remote-jobs.rss",
]
# Jobicy lists jobs with a clear location field; "anywhere" means open worldwide.
# Their rules allow checking at most once an hour, so it runs only at the top of the hour.
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs?count=50&geo=anywhere&industry=marketing"
MICRO1_SITEMAP = "https://jobs.micro1.ai/sitemap.xml"
MAX_AGE_MINUTES = 180          # only jobs posted in the last 3 hours
MICRO1_FLOOD_LIMIT = 30        # first run marks old micro1 posts as seen without alerting
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
SEEN_KEEP_DAYS = 30            # forget links after 30 days to keep the file small

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEET_CSV_URL = os.environ.get("DAILY_COUNT_CSV_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

UA = {"User-Agent": "Mozilla/5.0 (JobRadar; personal job alerts)"}

# ---------------- helpers ----------------

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def send_telegram(text):
    if DRY_RUN or not TOKEN or not CHAT_ID:
        print("--- would send ---\n" + text)
        return
    data = json.dumps({"chat_id": CHAT_ID, "text": text,
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data,
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        reason = e.read().decode("utf-8", errors="replace")
        hint = {401: "the bot token is wrong", 400: "the chat ID is wrong, or you have not pressed Start in the bot chat",
                403: "the bot is blocked or cannot message this chat", 404: "the bot token is wrong or incomplete"}.get(e.code, "")
        print(f"::error::Telegram refused the message ({e.code}): {hint}. Details: {reason}")
        raise
    time.sleep(1)  # stay well under Telegram's rate limit


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    cutoff = time.time() - SEEN_KEEP_DAYS * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=0, sort_keys=True)


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_rss(xml_text):
    """Return a list of dicts with title, link, body, creator, published."""
    root = ET.fromstring(xml_text)
    items = []
    for el in root.iter():
        if strip_ns(el.tag) not in ("item", "entry"):
            continue
        d = {}
        for child in el:
            name = strip_ns(child.tag)
            val = (child.text or "").strip()
            if name == "link" and not val:
                val = child.attrib.get("href", "")
            d.setdefault(name, val)
        items.append({
            "title": unescape(d.get("title", "")),
            "link": d.get("link") or d.get("guid", ""),
            "body": re.sub(r"<[^>]+>", " ", unescape(d.get("description") or d.get("summary") or d.get("content") or "")),
            "creator": d.get("creator", ""),
            "location": d.get("region") or d.get("location") or "",
            "published": d.get("pubDate") or d.get("published") or d.get("updated") or d.get("date", ""),
        })
    return items


def fetch_jobicy():
    data = json.loads(fetch(JOBICY_URL))
    out = []
    for j in data.get("jobs", []):
        out.append({
            "title": unescape(j.get("jobTitle", "")),
            "link": j.get("url", ""),
            "body": unescape(j.get("jobExcerpt", "")),
            "creator": j.get("companyName", ""),
            "location": j.get("jobGeo", "") or "Anywhere",
            "published": j.get("pubDate", ""),
        })
    return out


def age_minutes(published):
    if not published:
        return 99999
    try:
        dt = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            return 99999
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 60)

# ---------------- scoring (same rules as the n8n version) ----------------

ROLE_RE = re.compile(r"(gtm|go-to-market|go to market|revops|revenue operations|growth|marketing|demand gen|lead gen|outbound|clay|brand|content)")
ENTRY_RE = re.compile(r"(intern|junior|entry|associate|apprentice|trainee|graduate)")
GTM_RE = re.compile(r"(gtm|go-to-market|go to market|revops|revenue operations|clay)")
C = (r"(philippines|filipino|india|pakistan|bangladesh|sri lanka|indonesia|vietnam|thailand|malaysia|singapore|japan|korea|china|taiwan|australia|new zealand|brazil|mexico|argentina|colombia|chile|peru|latam|latin america|canada|usa|u\.s\.|us|united states|north america|uk|united kingdom|ireland|eu|europe|emea|apac|germany|france|spain|portugal|italy|netherlands|poland|romania|ukraine|turkey|israel|uae|egypt|kenya|south africa)")
RESTRICTED_RE = re.compile(
    r"(\b" + C + r"[ -]?(only|based|residents?|citizens?|timezones?|time zones?))"
    r"|(must (be|reside|live|work|be located|be based) (in|from) (the )?" + C + r")"
    r"|(located in (the )?" + C + r"\b)"
    r"|(\bremote[ ,(-]*(in )?" + C + r"\b)"
    r"|(authori[sz]ed to work in (the )?" + C + r")"
    r"|(\bus citizens?)"
    r"|(hiring (in|from) (the )?" + C + r")"
    r"|(open to candidates (in|from) (the )?" + C + r")")
NIGERIA_RE = re.compile(r"(nigeria|(?<!south )africa)")
WORLDWIDE_RE = re.compile(r"(worldwide|anywhere in the world|work from anywhere|global(ly)? remote|any location|all countries)")


def score(job):
    title = job["title"]
    t = title.lower()
    text = (title + " " + job["body"]).lower()
    if GTM_RE.search(t) and ENTRY_RE.search(t):
        priority = "A - entry level GTM"
    elif GTM_RE.search(t):
        priority = "B - GTM role"
    else:
        priority = "C - marketing or growth role"
    loc = job.get("location", "").lower().strip()
    if loc:
        # trust the board's own location field when it has one
        if NIGERIA_RE.search(loc) or WORLDWIDE_RE.search(loc) or "anywhere" in loc:
            elig = "open to Nigeria or worldwide"
        else:
            elig = "restricted"
    elif NIGERIA_RE.search(text):
        elig = "open to Nigeria or worldwide"
    elif RESTRICTED_RE.search(text):
        elig = "restricted"
    elif WORLDWIDE_RE.search(text):
        elig = "open to Nigeria or worldwide"
    else:
        elig = "check location"
    company = job["creator"] or (title.split(":")[0] if ":" in title else "")
    return {
        "title": title, "company": company, "link": job["link"],
        "age": age_minutes(job["published"]), "priority": priority,
        "eligibility": elig, "matches_role": bool(ROLE_RE.search(t)),
    }


def keep(j):
    # strict: only roles that clearly say Nigeria, Africa, worldwide or anywhere
    return (j["matches_role"] and j["eligibility"] == "open to Nigeria or worldwide"
            and j["age"] <= MAX_AGE_MINUTES and bool(j["link"]))

# ---------------- micro1 ----------------

M1_ROLE_RE = re.compile(r"(marketing|brand|creative|gtm|go-to-market|growth|sales|content|communications|community|partnership|presentation|strategy|revenue|demand|outbound|clay)")
M1_RESTRICTED_RE = re.compile(r"(\b(us|u\.s\.|usa|united states|uk|canada|europe|india|philippines)[ -]?(only|based|residents?|citizens?))|(must (be|reside|live|work) (in|from) (the )?(us|u\.s\.|united states|uk|canada|europe))|(\bus citizens?)")


def check_micro1(seen, now):
    try:
        xml = fetch(MICRO1_SITEMAP)
    except Exception as e:
        print("micro1 sitemap failed:", e)
        return
    links = [u.strip() for u in re.findall(r"<loc>([^<]+/post/[^<]+)</loc>", xml)]
    new = [u for u in links if u not in seen]
    for u in new:
        seen[u] = now
    if len(new) > MICRO1_FLOOD_LIMIT:
        print(f"micro1: {len(new)} new posts, marked seen without alerting (first run)")
        return
    for u in new:
        try:
            html = fetch(u)
        except Exception as e:
            print("micro1 post failed:", u, e)
            continue
        m = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.I)
             or re.search(r"<title>([^<]+)</title>", html, re.I))
        title = unescape(m.group(1)).strip() if m else ""
        if not title or not M1_ROLE_RE.search(title.lower()):
            continue
        d = re.search(r'<meta[^>]+(?:name|property)="(?:og:)?description"[^>]+content="([^"]+)"', html, re.I)
        if M1_RESTRICTED_RE.search((title + " " + (d.group(1) if d else "")).lower()):
            continue
        send_telegram(f"New micro1 role (contract, AI-training work)\n{title}\n{u}\n"
                      "Check pay and country on the page before applying.")

# ---------------- modes ----------------

def run_jobs():
    seen = load_seen()
    now = time.time()
    jobs = []
    for url in FEEDS:
        try:
            jobs += parse_rss(fetch(url))
        except Exception as e:
            print("feed failed:", url, e)
    if datetime.now(timezone.utc).minute < 15:
        try:
            jobs += fetch_jobicy()
        except Exception as e:
            print("Jobicy failed:", e)
    sent = 0
    for job in jobs:
        j = score(job)
        if not keep(j) or j["link"] in seen:
            continue
        seen[j["link"]] = now
        send_telegram(f"New job, posted {j['age']} min ago\n{j['priority']}\n{j['title']}\n"
                      f"Company: {j['company']}\nEligibility: {j['eligibility']}\n{j['link']}")
        sent += 1
    check_micro1(seen, now)
    save_seen(seen)
    print(f"checked {len(jobs)} feed items, sent {sent} job alerts")


def run_report():
    if not SHEET_CSV_URL:
        print("DAILY_COUNT_CSV_URL not set, skipping daily report")
        return
    import csv
    import io
    rows = list(csv.reader(io.StringIO(fetch(SHEET_CSV_URL))))
    lines = [f"{r[0]}: {r[1]}" for r in rows[1:] if len(r) >= 2 and r[0]]
    send_telegram("Daily job report\n" + "\n".join(lines))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "jobs"
    if mode == "test":
        send_telegram("Job Radar is connected. Job alerts will arrive here.")
    elif mode == "report":
        run_report()
    else:
        run_jobs()
