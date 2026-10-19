"""
Facebook-groep kamer-monitor
----------------------------
Checkt een Facebook-groep op nieuwe posts, filtert op "kamer aangeboden" + "geschikt voor een man",
en stuurt een pushmelding naar je telefoon via ntfy.sh (gratis, geen account nodig).

Eerste keer:   python monitor.py --login     (log handmatig in bij Facebook, sluit daarna het venster)
Daarna:        python monitor.py             (checkt elke paar minuten, laat draaien)
Eenmalig test: python monitor.py --once --show

Op GitHub Actions: zet de secrets FB_COOKIES en NTFY_TOPIC, de workflow draait "--once".
"""

import argparse
import hashlib
import json
import os
import sys
import random
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ================== INSTELLINGEN ==================
GROUP_URL = "https://www.facebook.com/groups/244190669097392/?sorting_setting=CHRONOLOGICAL"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "kamer-ewoud-VERANDER-DIT-123")  # kies iets unieks, iedereen met deze naam kan meelezen
CHECK_EVERY_MIN = (8, 15)                      # willekeurig tussen 8 en 15 minuten, minder robot-achtig
NOTIFY_WHEN_UNSURE = True                      # bij twijfel toch melden (beter een melding te veel)
# ==================================================

BASE = Path(__file__).parent
PROFILE_DIR = BASE / "fb_profile"
SEEN_FILE = BASE / "seen.json"

# --- Filterwoorden (lowercase). Pas gerust aan. ---

# Iemand ZOEKT een kamer -> weg
SEEKING = [
    r"\bik zoek\b", r"\bik ben op zoek naar een? (kamer|woning|studio|plek)",
    r"\bwij zoeken een (kamer|woning)", r"\bzoek(e|t)? (een )?(kamer|woonruimte|studio)",
    r"\blooking for (a |an )?(room|place|studio|accommodation|apartment)",
    r"\bi'?m looking for\b", r"\bsearching for (a )?room", r"\bin need of a room",
    r"\bkamer gezocht\b", r"\broom wanted\b", r"\bwho has a room\b", r"\bwie heeft (er )?een kamer\b",
]

# Iemand BIEDT een kamer aan -> goed
OFFERING = [
    r"\bkamer (vrij|beschikbaar|aangeboden|te huur)", r"\b(vrij|beschikbaar) per\b",
    r"\bte huur\b", r"\bhuurder gezocht\b", r"\bnieuwe huisgeno(ot|ten)\b",
    r"\b(op zoek naar|zoeken) (een )?(nieuwe )?huisgeno(ot|ten)\b", r"\bonderhuur(der)?\b",
    r"\broom (available|for rent|offered)", r"\bavailable (from|per|as of)\b",
    r"\b(looking for|seeking) (a )?(new )?(housemate|roommate|flatmate|tenant)",
    r"\bsublet\b", r"\bfor rent\b", r"\bhospiteeravond\b", r"\bhuurprijs\b", r"\bper maand\b",
    r"€\s?\d{3}", r"\b\d{3}\s?(euro|eur)\b", r"\bm2\b|\bm²\b",
]

# Alleen voor vrouwen -> weg
FEMALE_ONLY = [
    r"\bhuisgenote\b", r"\balleen (voor )?(vrouwen|meiden|dames)", r"\b(vrouwen|meiden|dames)huis\b",
    r"\bvrouwelijke (huisgenoot|huurder|student)", r"\bwe zoeken een (meid|meisje|vrouw|dame)\b",
    r"\bfemale only\b", r"\bfemales? only\b", r"\bonly (for )?(girls|women|females)\b",
    r"\bgirls only\b", r"\bfemale (housemate|roommate|tenant|flatmate)\b",
    r"\b(ladies|girls) house\b", r"\bgeen jongens\b", r"\bno (boys|guys|men)\b",
]


def match_any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def classify(text: str) -> str:
    """Geeft 'relevant', 'irrelevant' of 'twijfel'."""
    t = text.lower()
    if match_any(FEMALE_ONLY, t):
        return "irrelevant"
    seeking = match_any(SEEKING, t)
    offering = match_any(OFFERING, t)
    if offering and not seeking:
        return "relevant"
    if seeking and not offering:
        return "irrelevant"
    return "twijfel"   # beide of geen van beide


# --- Opslag van al geziene posts ---

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:]))


# --- Melding ---

def notify(text, link, label):
    title = "Kamer gevonden!" if label == "relevant" else "Misschien een kamer?"
    preview = text.strip().replace("\n", " ")[:300]
    headers = {"Title": title, "Tags": "house", "Priority": "high" if label == "relevant" else "default"}
    if link:
        headers["Click"] = link
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=preview.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print("Melding mislukt:", e)


# --- Facebook uitlezen ---

def scrape_posts(page):
    page.goto(GROUP_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    if any(w in page.url for w in ("login", "checkpoint")):
        raise LoginExpired(page.url)
    # Een beetje scrollen zodat de nieuwste ~10 posts laden
    for _ in range(4):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1500 + random.randint(0, 1000))

    # "Meer weergeven" / "See more" openklikken zodat je de hele tekst hebt
    for label in ["Meer weergeven", "See more"]:
        for btn in page.get_by_role("button", name=label).all()[:15]:
            try:
                btn.click(timeout=1000)
            except Exception:
                pass

    posts = []
    for art in page.locator('div[role="feed"] div[role="article"]').all():
        try:
            text = art.inner_text(timeout=2000)
        except Exception:
            continue
        if len(text) < 40:
            continue
        link = None
        for a in art.locator("a[href*='/posts/'], a[href*='/permalink/']").all()[:1]:
            href = a.get_attribute("href") or ""
            link = href.split("?")[0]
        key = link or hashlib.sha1(text[:500].encode()).hexdigest()
        posts.append({"key": key, "text": text, "link": link})
    return posts


class LoginExpired(Exception):
    pass


SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


def parse_cookies(raw: str):
    """Accepteert een JSON-export (bijv. van de Cookie-Editor extensie) of 'c_user=..; xs=..'."""
    raw = raw.strip()
    cookies = []
    if raw.startswith("["):
        for c in json.loads(raw):
            ck = {"name": c["name"], "value": c["value"],
                  "domain": c.get("domain", ".facebook.com"), "path": c.get("path", "/"),
                  "secure": c.get("secure", True), "httpOnly": c.get("httpOnly", False)}
            ss = SAMESITE.get(str(c.get("sameSite", "")).lower())
            if ss:
                ck["sameSite"] = ss
            cookies.append(ck)
    else:
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies.append({"name": k, "value": v, "domain": ".facebook.com",
                                "path": "/", "secure": True})
    return cookies


def open_context(p, show):
    cookies = os.environ.get("FB_COOKIES")
    if cookies:  # GitHub Actions / server: geen bewaard profiel, wel cookies
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1200, "height": 900}, locale="nl-NL", timezone_id="Europe/Amsterdam",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
        ctx.add_cookies(parse_cookies(cookies))
        return ctx
    return p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=not show,
                                                viewport={"width": 1200, "height": 900})


def run(once=False, show=False):
    seen = load_seen()
    first_run = len(seen) == 0
    with sync_playwright() as p:
        ctx = open_context(p, show)
        page = ctx.new_page()
        while True:
            try:
                posts = scrape_posts(page)
                new = [x for x in posts if x["key"] not in seen]
                print(f"[{time.strftime('%H:%M')}] {len(posts)} posts gezien, {len(new)} nieuw")
                for post in new:
                    seen.add(post["key"])
                    label = classify(post["text"])
                    print(f"   - {label}: {post['text'][:80].replace(chr(10), ' ')}")
                    if first_run:
                        continue  # bij de eerste run niet spammen met oude posts
                    if label == "relevant" or (label == "twijfel" and NOTIFY_WHEN_UNSURE):
                        notify(post["text"], post["link"], label)
                save_seen(seen)
                first_run = False
            except LoginExpired:
                print("Niet (meer) ingelogd bij Facebook.")
                requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", timeout=15,
                              data="Facebook-login is verlopen, zet nieuwe cookies in GitHub (FB_COOKIES).".encode(),
                              headers={"Title": "Kamer-monitor staat stil", "Tags": "warning"})
                ctx.close()
                sys.exit(1)
            except Exception as e:
                print("Fout:", e)
            if once:
                break
            time.sleep(random.randint(*CHECK_EVERY_MIN) * 60)
        ctx.close()


def login():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = ctx.new_page()
        page.goto("https://www.facebook.com/login")
        input("Log in bij Facebook in het venster, druk daarna hier op Enter...")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="eenmalig inloggen")
    ap.add_argument("--once", action="store_true", help="één keer checken en stoppen")
    ap.add_argument("--show", action="store_true", help="browservenster laten zien")
    ap.add_argument("--test", action="store_true", help="filter testen met voorbeeldteksten")
    args = ap.parse_args()
    if args.login:
        login()
    elif args.test:
        for s in [
            "Kamer vrij per 1 december in Utrecht Oost, 18m2, €550 incl.",
            "Hoi! Ik ben op zoek naar een kamer in Utrecht, 24 jaar, student.",
            "Wij zoeken een nieuwe huisgenote voor ons gezellige meidenhuis!",
            "Looking for a room in Utrecht from January",
            "Room available from November, female only",
            "Nieuwe huisgenoot gezocht! Hospiteeravond 3 nov.",
        ]:
            print(f"{classify(s):10} | {s}")
    else:
        run(once=args.once, show=args.show)
