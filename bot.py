"""Politics news alert bot (EN + FR) -> Telegram. Personal use."""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

# ---------------------------------------------------------------- config
TIMEZONE = ZoneInfo("Asia/Phnom_Penh")   # timezone shown in the alert timestamp
MAX_AGE_HOURS = 6        # ignore items older than this
MAX_SENDS_PER_RUN = 15   # safety cap so a bad run can't flood the chat
KEEP_DAYS = 7            # how long to remember seen items
RECENT_HOURS = 36        # how long sent alerts are kept for the digest
DIGEST_HOURS = 24        # digest covers the last N hours
DIGEST_PER_SOURCE = 4    # headlines per source in the digest
STATE_FILE = "seen.json"
UA = "Mozilla/5.0 (compatible; PoliticsNewsBot/1.0)"

GNEWS_EN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

FR_POLITICS = (
    r"politique|gouvernement|ministre|pr[ée]sident|[ée]lection|scrutin|"
    r"assembl[ée]e|s[ée]nat|parlement|d[ée]put[ée]|s[ée]nateur|premier ministre|"
    r"[ée]lys[ée]e|matignon|coalition|motion de censure|r[ée]forme|diplomat"
)
EN_POLITICS = (
    r"politic|government|minister|president|election|vote|parliament|congress|"
    r"senate|white house|campaign|coalition|diplomat|sanction|treaty"
)

SOURCES = [
    # ---- English
    dict(name="BBC", lang="en", enabled=True, gnews=False,
         url="https://feeds.bbci.co.uk/news/politics/rss.xml"),
    # CNN's own RSS (rss.cnn.com) stopped updating in 2023 -> Google News workaround
    dict(name="CNN", lang="en", enabled=True, gnews=True,
         url=GNEWS_EN.format(q="when:1d+site:cnn.com+politics")),
    # Reuters has had no official RSS since 2020 -> Google News workaround
    dict(name="Reuters", lang="en", enabled=True, gnews=True,
         url=GNEWS_EN.format(q="when:1d+site:reuters.com+politics")),
    # Optional: Le Monde English front page (not politics-only, so it is filtered)
    dict(name="Le Monde (EN)", lang="en", enabled=False, gnews=False,
         url="https://www.lemonde.fr/en/rss/une.xml", filter=EN_POLITICS),
    # ---- French
    dict(name="Le Monde Politique", lang="fr", enabled=True, gnews=False,
         url="https://www.lemonde.fr/politique/rss_full.xml"),
    # General France 24 FR feed, filtered down to politics
    dict(name="France 24", lang="fr", enabled=True, gnews=False,
         url="https://www.france24.com/fr/rss", filter=FR_POLITICS),
]

FLAGS = {"en": "🇬🇧", "fr": "🇫🇷"}
LABELS = {"en": "Politics", "fr": "Politique"}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DEFAULT_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
CHATS = {
    "en": os.environ.get("CHAT_ID_EN") or DEFAULT_CHAT,
    "fr": os.environ.get("CHAT_ID_FR") or DEFAULT_CHAT,
}
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("seen", {})
        s.setdefault("initialized", [])
        s.setdefault("recent", [])
        return s
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "initialized": [], "recent": []}


def save_state(state):
    cutoff = time.time() - KEEP_DAYS * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    rc = time.time() - RECENT_HOURS * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > rc]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


# ---------------------------------------------------------------- helpers
def norm_title(t):
    return re.sub(r"\W+", " ", t.lower()).strip()


def clean_title(title, gnews):
    title = html.unescape(title).strip()
    if gnews and " - " in title:          # Google News appends " - Publisher"
        title = title.rsplit(" - ", 1)[0]
    return title


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(e, key, None) or e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch(src):
    r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content).entries


def time_ago(when, lang):
    secs = max(0, int((datetime.now(timezone.utc) - when).total_seconds()))
    mins = secs // 60
    if mins < 1:
        return "just now" if lang == "en" else "à l'instant"
    if mins < 60:
        n, unit_en, unit_fr = mins, "min", "min"
    else:
        hours = mins // 60
        n = hours
        unit_en = "hour" if hours == 1 else "hours"
        unit_fr = "heure" if hours == 1 else "heures"
    return f"{n} {unit_en} ago" if lang == "en" else f"il y a {n} {unit_fr}"


def send(lang, src, title, link, when):
    chat = CHATS[lang]
    ts = when.astimezone(TIMEZONE).strftime("%d %b %Y, %H:%M")
    ago = time_ago(when, lang)
    text = (
        f"{FLAGS[lang]} <b>{html.escape(src['name'])}</b> · {LABELS[lang]}\n"
        f"<a href=\"{html.escape(link, quote=True)}\">{html.escape(title)}</a>\n"
        f"🕒 {ts} · {ago}"
    )
    return tg_post(chat, text)


def tg_post(chat, text, preview=True):
    if DRY_RUN:
        print(text, "\n")
        return True
    for _ in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": not preview},
            timeout=20,
        )
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if r.ok:
            time.sleep(1.1)   # stay under Telegram's per-chat rate limit
            return True
        print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
        return False
    return False


# ---------------------------------------------------------------- digest
DAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
}
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"],
}
DIGEST_TEXT = {
    "en": dict(title="Morning digest", empty="No political stories were alerted in the last {h} hours."),
    "fr": dict(title="Résumé du matin", empty="Aucune actualité politique signalée ces {h} dernières heures."),
}


def digest_date(lang):
    d = datetime.now(TIMEZONE)
    day, month = DAYS[lang][d.weekday()], MONTHS[lang][d.month - 1]
    return f"{day} {d.day} {month}" if lang == "fr" else f"{day}, {month} {d.day}"


def build_digest(lang, items):
    """Return a list of message strings (split to fit Telegram's 4096 limit)."""
    t = DIGEST_TEXT[lang]
    header = f"☀️ {FLAGS[lang]} <b>{t['title']}</b> · {digest_date(lang)}"
    if not items:
        return [f"{header}\n\n{t['empty'].format(h=DIGEST_HOURS)}"]

    blocks = []
    for src in [s_ for s_ in SOURCES if s_["lang"] == lang]:
        mine = sorted((i for i in items if i["src"] == src["name"]),
                      key=lambda i: i["ts"], reverse=True)[:DIGEST_PER_SOURCE]
        if not mine:
            continue
        lines = [f"<b>{html.escape(src['name'])}</b>"]
        for i in mine:
            link = html.escape(i["link"], quote=True)
            lines.append(f"• <a href=\"{link}\">{html.escape(i['title'])}</a>")
        blocks.append("\n".join(lines))

    messages, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3800:
            messages.append(cur)
            cur = header
        cur += "\n\n" + b
    messages.append(cur)
    return messages


def run_digest():
    if not DRY_RUN and (not TOKEN or not any(CHATS.values())):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / chat id")
    state = load_state()
    cutoff = time.time() - DIGEST_HOURS * 3600
    for lang in ("en", "fr"):
        items, seen_titles = [], set()
        for r in sorted(state["recent"], key=lambda r: r["ts"], reverse=True):
            if r["lang"] != lang or r["ts"] < cutoff:
                continue
            key = norm_title(r["title"])
            if key in seen_titles:
                continue
            seen_titles.add(key)
            items.append(r)
        for msg in build_digest(lang, items):
            tg_post(CHATS[lang], msg, preview=False)
        print(f"Digest {lang}: {len(items)} stories")


# ---------------------------------------------------------------- main
def main():
    if not DRY_RUN and (not TOKEN or not any(CHATS.values())):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / chat id")

    state = load_state()
    seen = state["seen"]
    now = time.time()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    queue = []
    seen_titles = set()

    for src in SOURCES:
        if not src["enabled"]:
            continue
        first_run = src["name"] not in state["initialized"]
        try:
            entries = fetch(src)
        except Exception as exc:
            print(f"[{src['name']}] fetch failed: {exc}", file=sys.stderr)
            continue

        pattern = re.compile(src["filter"], re.I) if src.get("filter") else None
        st = dict(fetched=len(entries), known=0, new=0, filtered=0, old=0, dupe=0, queued=0)
        for e in entries:
            uid = f"{src['name']}|{e.get('id') or e.get('link')}"
            if uid in seen:
                st["known"] += 1
                continue
            title = clean_title(e.get("title", ""), src["gnews"])
            link = e.get("link", "")
            if not title or not link:
                continue
            seen[uid] = now                     # mark seen even if filtered out
            st["new"] += 1
            if first_run:                       # seed silently, don't flood
                continue
            if pattern and not pattern.search(f"{title} {e.get('summary', '')}"):
                st["filtered"] += 1
                continue
            when = entry_time(e) or datetime.now(timezone.utc)
            if when < cutoff:
                st["old"] += 1
                continue
            key = (src["lang"], norm_title(title))
            if key in seen_titles:              # same headline from two feeds
                st["dupe"] += 1
                continue
            seen_titles.add(key)
            queue.append((when, src, title, link))
            st["queued"] += 1

        print(f"[{src['name']}] " + ", ".join(f"{k}={v}" for k, v in st.items())
              + (" (first run: seeded)" if first_run else ""))

        if src["name"] not in state["initialized"]:
            state["initialized"].append(src["name"])

    queue.sort(key=lambda x: x[0])              # oldest first
    sent = 0
    for when, src, title, link in queue[-MAX_SENDS_PER_RUN:]:
        if send(src["lang"], src, title, link, when):
            sent += 1
            state["recent"].append({"lang": src["lang"], "src": src["name"],
                                    "title": title, "link": link,
                                    "ts": when.timestamp()})
    print(f"Queued {len(queue)}, sent {sent}")
    if not DRY_RUN:
        save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "digest":
        run_digest()
    else:
        main()