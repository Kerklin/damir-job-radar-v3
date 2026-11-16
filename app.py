"""Damir Job Radar v3: automatic vacancy search + explainable fit scoring.

Single-file Streamlit app. No API keys, no database, no paid services.

Automatic sources (public career-site endpoints, no login):
    UNDP and IOM  (Oracle Recruiting Cloud)
    WFP and UNHCR (Workday)
    UNICEF        (PageUp listing page, best effort)
    Arbeitnow     (free European job API)
You can add more career sites (Workday, Oracle, Greenhouse, Lever, Ashby, RSS) in the Sources tab.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st

# =====================================================================
# 1. SETTINGS YOU MAY EDIT
# =====================================================================

APP_TITLE = "Damir Job Radar"
CACHE_HOURS = 6                      # automatic re-search interval while the app is open
HTTP_TIMEOUT = 20
HTTP_RETRIES = 1
SOURCE_TIME_BUDGET = 90             # seconds per source, so one slow site cannot block the app
DETAIL_LIMIT_PER_SOURCE = 30         # full descriptions fetched for the most relevant titles
USER_AGENT = "Mozilla/5.0 (compatible; DamirJobRadar/3.0; personal job search)"

DEFAULT_KEYWORDS = [
    "construction", "engineer", "infrastructure", "rehabilitation", "WASH",
    "procurement", "project manager", "shelter", "architect", "heritage",
]

# Built-in sources. The URL is the public careers page; the app detects the platform.
DEFAULT_SOURCES = [
    {"name": "UNDP", "url": "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "enabled": True},
    {"name": "IOM", "url": "https://fa-evlj-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001", "enabled": True},
    {"name": "WFP", "url": "https://wfp.wd3.myworkdayjobs.com/job_openings", "enabled": True},
    {"name": "UNHCR", "url": "https://unhcr.wd3.myworkdayjobs.com/External", "enabled": True},
    {"name": "UNICEF", "url": "https://jobs.unicef.org/en-us/listing/", "enabled": True},
    {"name": "Arbeitnow (Europe)", "url": "https://www.arbeitnow.com/api/job-board-api", "enabled": True},
]

PROFILE = {
    "experience_start_year": 2001,
    "languages_confirmed": ["bosnian", "croatian", "serbian", "english"],
    "languages_uncertain": ["french", "italian"],   # spoken; level not configured
}
WEIGHTS = {"title": 20, "competencies": 25, "sector": 15, "experience": 15,
           "education": 10, "location": 5, "language": 5, "contract": 5}

PRIMARY_TITLES = [
    "construction", "civil engineer", "architectural engineer", "architect", "infrastructure",
    "rehabilitation", "site engineer", "site supervisor", "field engineer", "wash engineer",
    "wash specialist", "wash officer", "shelter", "facilities manager", "facility manager",
    "procurement", "contract manager", "contracts manager", "quantity surveyor",
    "project manager", "heritage", "engineer",
]
SECONDARY_TITLES = [
    "project coordinator", "programme officer", "program officer", "programme specialist",
    "technical adviser", "technical advisor", "technical specialist", "operations manager",
    "operations officer", "monitoring and evaluation", "supply", "consultant", "specialist",
]
NEGATIVE_TITLES = [
    "software engineer", "software developer", "data scientist", "data engineer", "devops",
    "frontend", "backend", "full stack", "machine learning", "nurse", "physician",
    "medical doctor", "pharmacist", "midwife", "sales representative", "account executive",
    "accountant", "intern", "internship", "graduate programme", "trainee", "driver",
    "chauffeur", "cleaner", "security guard",
]
COMPETENCIES = [
    "construction", "rehabilitation", "infrastructure", "site supervision", "supervision of works",
    "architecture", "architectural", "civil engineering", "wash", "water supply", "sanitation",
    "solar", "photovoltaic", "procurement", "tender", "bill of quantities", "bills of quantities",
    "boq", "cost estimate", "contract administration", "contract management", "fidic",
    "budget monitoring", "quality assurance", "quality control", "handover", "defects liability",
    "technical specifications", "design review", "stakeholder", "municipalit", "eu-funded",
    "european union", "ipa", "project management", "pmp", "heritage", "conservation",
    "post-conflict", "accessibility", "energy efficiency", "hvac", "schools", "preschool",
    "health facilit",
]
TARGET_ORGS = [
    "unicef", "undp", "unops", "wfp", "unhcr", "iom", "osce", "unesco", "world bank",
    "european union", "ebrd", "eib", "giz", "usaid", "save the children",
    "norwegian refugee council", "danish refugee council", "caritas", "strabag", "arup",
    "aecom", "jacobs", "stantec",
]
SECTOR_TERMS = ["humanitarian", "international development", "united nations", "ngo",
                "donor", "eu-funded", "reconstruction", "recovery"]
BIH_TERMS = ["bosnia", "herzegovina", "hercegovina", "sarajevo", "banja luka", "mostar",
             "tuzla", "zenica", "bihac", "bihać", "brcko", "brčko"]
GOOD_LOCATIONS = BIH_TERMS + ["western balkans", "balkans", "home-based", "home based",
                              "remote", "europe"]
KNOWN_LANGUAGES = ["english", "french", "italian", "spanish", "german", "arabic", "russian",
                   "portuguese", "ukrainian", "turkish", "chinese", "bosnian", "croatian",
                   "serbian", "albanian", "macedonian", "dutch", "polish"]

# =====================================================================
# 2. TEXT HELPERS
# =====================================================================

JOB_COLUMNS = [
    "job_id", "title", "organization", "location", "country", "grade", "contract_type",
    "posted_date", "closing_date", "url", "source", "description", "score", "category",
    "status", "days_left", "strengths", "gaps", "uncertain", "keywords", "breakdown",
]
IMPORT_COLUMNS = ["title", "organization", "location", "country", "contract_type",
                  "posted_date", "closing_date", "url", "description"]
TRACKER_STATES = ["", "Saved", "Applied", "Interview", "Rejected", "Not interested"]


class SourceError(Exception):
    """A whole source could not be read."""


def clean_text(value: Any) -> str:
    """Plain text without HTML or scripts."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    raw = html.unescape(html.unescape(str(value)))
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<\s*(br|/p|/li|/div|/h\d|/tr)\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[ \t\r\f\v\xa0]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def parse_date(value: Any) -> str:
    """Any date-like value (ISO text, epoch seconds/ms) -> YYYY-MM-DD, or ''."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        parsed = pd.to_datetime(str(value), errors="coerce", utc=True)
        return "" if pd.isna(parsed) else parsed.date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def parse_relative_posted(text: str, today: date | None = None) -> str:
    """Workday 'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / '30+ Days Ago'."""
    today = today or date.today()
    t = (text or "").lower()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else ""


def canonical_url(value: Any) -> str:
    """Only http(s) links; drop query string and fragment."""
    try:
        parts = urlsplit(clean_text(value))
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def safe_link(value: Any) -> str:
    """Original http(s) link (query kept, needed by some portals) or ''."""
    text = clean_text(value)
    return text if canonical_url(text) else ""


def fingerprint(job: dict[str, Any]) -> str:
    raw = "|".join(re.sub(r"[^a-z0-9]+", " ", clean_text(job.get(k)).lower()).strip()
                   for k in ("organization", "title", "location"))
    return "fp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def job_identifier(job: dict[str, Any]) -> str:
    return canonical_url(job.get("url")) or fingerprint(job)


def contains(term: str, source: str, whole: bool = False) -> bool:
    end = r"(?![a-z0-9])" if whole else ""
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + end, source.lower()) is not None


def matched(terms: list[str], source: str, whole: bool = False) -> list[str]:
    return [t for t in terms if contains(t, source, whole)]


def title_is_relevant(title: str) -> bool:
    t = (title or "").lower()
    if matched(NEGATIVE_TITLES, t, True):
        return False
    return bool(matched(PRIMARY_TITLES, t) or matched(SECONDARY_TITLES, t))


def extract_grade(text: str) -> str:
    m = re.search(r"\b(P-?[1-6]|D-?[12]|NO-?[1-4A-D]|NO[A-D]|G-?[1-7]|GS-?[1-7]|SC-?\d{1,2}|"
                  r"IPSA-?\d{1,2}|NPSA-?\d{1,2}|IICA-?\d|LICA-?\d{1,2}|CST\s?I{1,3})\b", text or "")
    return m.group(1).upper() if m else ""


def days_left(closing: str, today: date | None = None) -> int | None:
    if not closing:
        return None
    try:
        return (date.fromisoformat(closing[:10]) - (today or date.today())).days
    except ValueError:
        return None


# =====================================================================
# 3. SCORING (deterministic and explainable)
# =====================================================================

def score_job(raw: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Normalize one job and score it 0-100 against Damir's profile."""
    today = today or date.today()
    job = {
        "title": clean_text(raw.get("title")),
        "organization": clean_text(raw.get("organization")),
        "location": clean_text(raw.get("location")),
        "country": clean_text(raw.get("country")),
        "contract_type": clean_text(raw.get("contract_type")),
        "posted_date": parse_date(raw.get("posted_date")),
        "closing_date": parse_date(raw.get("closing_date")),
        "url": safe_link(raw.get("url")),
        "source": clean_text(raw.get("source")) or "Manual",
        "description": clean_text(raw.get("description")),
    }
    job["grade"] = clean_text(raw.get("grade")) or extract_grade(f"{raw.get('title', '')} {job['description'][:600]}")
    title = job["title"].lower()
    org = job["organization"].lower()
    loc = f"{job['location']} {job['country']}".lower()
    desc = job["description"].lower()
    full = f"{title}\n{org}\n{loc}\n{desc}"
    in_bih = bool(matched(BIH_TERMS, f"{loc} {title}"))

    strengths: list[str] = []
    gaps: list[str] = []
    uncertain: list[str] = []
    pts: dict[str, int] = {}
    status = "OK"

    # 1. Title (20)
    neg = matched(NEGATIVE_TITLES, title, True)
    prim = matched(PRIMARY_TITLES, title)
    sec = matched(SECONDARY_TITLES, title)
    if neg:
        pts["title"] = 0
        gaps.append("Role family not a fit: " + ", ".join(neg[:3]))
        status = "Disqualified"
    elif prim:
        pts["title"] = 20
        strengths.append("Primary target title: " + ", ".join(prim[:3]))
    elif sec:
        pts["title"] = 12
        strengths.append("Secondary target title: " + ", ".join(sec[:3]))
    else:
        pts["title"] = 0
        gaps.append("Title outside target role families")

    # 2. Competencies (25): distinct terms only, so repetition adds nothing
    comps = matched(COMPETENCIES, f"{title}\n{desc}")
    pts["competencies"] = round(25 * min(1.0, len(comps) / 8))
    if comps:
        strengths.append(f"{len(comps)} matching competencies")
    else:
        gaps.append("No target technical competencies found")

    # 3. Organization / sector (15)
    orgs = matched(TARGET_ORGS, f"{org}\n{title}", True)
    sectors = matched(SECTOR_TERMS, full)
    if orgs:
        pts["sector"] = 15
        strengths.append("Target organization: " + orgs[0].upper())
    elif sectors:
        pts["sector"] = 9
        strengths.append("Relevant sector: " + ", ".join(sectors[:3]))
    else:
        pts["sector"] = 4

    # 4. Experience (15)
    req_years = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:or more\s*)?years?", desc) if 0 < int(n) <= 40]
    have = today.year - PROFILE["experience_start_year"]
    if re.search(r"\b(entry[- ]level|junior|no experience required)\b", full):
        pts["experience"] = 0
        gaps.append("Appears below your seniority")
    elif req_years:
        need = max(req_years)
        pts["experience"] = 15 if need <= have else 6
        (strengths if need <= have else gaps).append(f"Requires {need}+ years; you have about {have}")
    else:
        pts["experience"] = 9
        uncertain.append("Required years of experience not stated")

    # 5. Education (10)
    edu = matched(["architecture", "architectural", "civil engineering", "engineering", "master",
                   "advanced university degree", "phd", "pmp", "construction management"], desc)
    pts["education"] = 10 if edu else 5
    if edu:
        strengths.append("Education aligned: " + ", ".join(edu[:3]))
    else:
        uncertain.append("Required degree not clearly stated")

    if re.search(r"licensed (professional )?engineer|chartered engineer|registered (professional )?engineer|"
                 r"professional engineer licen[cs]e", desc):
        uncertain.append("Engineering licence/registration mentioned: verify whether mandatory")
        if status == "OK":
            status = "Review required"

    # 6. Location and eligibility (5)
    nat = (re.search(r"nationals of ([a-z ]+?)(?: only|\)|\.|,|;|\n|$)", full)
           or re.search(r"(?:only|open|restricted) (?:open )?(?:to|for) ([a-z]+) nationals", full))
    explicit_national = re.search(r"nationals? only|national consultant|national individual consult|"
                                  r"locally recruited|open to nationals", full)
    local_grade = re.match(r"(NO|G|GS|SC|NPSA|LICA)", job["grade"] or "")
    if nat and not any(x in nat.group(1) for x in ("bosnia", "bih", "herzegovina", "all countries",
                                                    "all nationalities", "any country")):
        pts["location"] = 0
        gaps.append("Nationality restriction: " + nat.group(0).strip())
        status = "Disqualified"
    elif (explicit_national or local_grade) and not in_bih:
        pts["location"] = 0
        gaps.append("National/locally recruited post outside Bosnia and Herzegovina: usually requires local nationality")
        if explicit_national and loc.strip():
            status = "Disqualified"
        elif status == "OK":
            status = "Review required"
    elif (explicit_national or local_grade) and in_bih:
        pts["location"] = 5
        strengths.append("National post in Bosnia and Herzegovina")
    elif matched(GOOD_LOCATIONS, loc + " " + desc[:400]):
        pts["location"] = 5
        strengths.append("Location compatible")
    else:
        pts["location"] = 3
        uncertain.append("Relocation or duty-station conditions need review")

    # 7. Language (5)
    lang_ctx = " ".join(re.findall(r"(?:fluen\w*|proficien\w*|native|required|mandatory|working knowledge)"
                                   r"[^.\n]{0,100}", desc))
    req_langs = matched(KNOWN_LANGUAGES, lang_ctx, True)
    unknown = [l for l in req_langs if l not in PROFILE["languages_confirmed"] + PROFILE["languages_uncertain"]]
    level_unc = [l for l in req_langs if l in PROFILE["languages_uncertain"]]
    if unknown:
        pts["language"] = 0
        gaps.append("Language not in profile: " + ", ".join(unknown))
        if status == "OK":
            status = "Review required"
    elif level_unc:
        pts["language"] = 3
        uncertain.append("Confirm your level: " + ", ".join(l.title() for l in level_unc))
    else:
        pts["language"] = 5

    # 8. Contract (5)
    if re.search(r"\b(intern|internship|unpaid)\b", full):
        pts["contract"] = 0
        gaps.append("Internship or unpaid contract")
    else:
        pts["contract"] = 5

    if len(desc) < 250:
        uncertain.append("Short description: low confidence, open the vacancy")

    score = max(0, min(100, sum(pts.values())))
    remaining = days_left(job["closing_date"], today)
    if remaining is not None and remaining < 0:
        status = "Expired"

    if status in ("Disqualified", "Expired"):
        category = status
    elif status == "Review required" and score >= 65:
        category = "Review required"
    elif score >= 85:
        category = "Excellent fit"
    elif score >= 75:
        category = "Strong fit"
    elif score >= 65:
        category = "Good fit"
    elif score >= 50:
        category = "Possible fit"
    else:
        category = "Low priority"

    return {**job, "job_id": job_identifier(job), "score": score, "category": category,
            "status": status, "days_left": remaining, "strengths": " | ".join(strengths),
            "gaps": " | ".join(gaps), "uncertain": " | ".join(uncertain),
            "keywords": ", ".join(comps),
            "breakdown": "; ".join(f"{k} {v}/{WEIGHTS[k]}" for k, v in pts.items())}


def score_jobs(raw_jobs: list[dict[str, Any]], today: date | None = None) -> pd.DataFrame:
    """Score, de-duplicate (URL, then org/title/location) and drop expired jobs."""
    seen_ids: set[str] = set()
    seen_fp: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw in raw_jobs:
        try:
            job = score_job(raw, today)
        except Exception:  # one malformed record must never break the list
            continue
        fp = fingerprint(job) if not job["url"] else ""   # distinct URLs = distinct postings
        if job["status"] == "Expired" or not job["title"] or job["job_id"] in seen_ids or (fp and fp in seen_fp):
            continue
        seen_ids.add(job["job_id"])
        if fp:
            seen_fp.add(fp)
        rows.append(job)
    if not rows:
        return pd.DataFrame(columns=JOB_COLUMNS)
    df = pd.DataFrame(rows, columns=JOB_COLUMNS)
    df["_c"] = df["closing_date"].replace("", "9999-12-31")
    return df.sort_values(["score", "_c"], ascending=[False, True]).drop(columns="_c").reset_index(drop=True)


# =====================================================================
# 4. INTERNET SEARCH (source adapters)
# =====================================================================

def http(method: str, url: str, *, params: dict | None = None, body: dict | None = None,
         expect: str = "json") -> Any:
    """HTTP with timeout, retries on 429/5xx/network errors, and clear errors."""
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/json" if expect == "json" else "text/html,application/xml,*/*",
               "Accept-Language": "en-US,en;q=0.9"}
    last = "unknown error"
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.request(method, url, params=params, json=body, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(1 + attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(2 + 2 * attempt)
            continue
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code}")
        if expect == "json":
            try:
                return r.json()
            except ValueError as exc:
                raise SourceError("response was not JSON") from exc
        return r.text
    raise SourceError(last)


def safe_keyword(kw: str) -> str:
    return re.sub(r'["\';,]', " ", kw).strip()


def detect_source(url: str, name: str = "") -> dict[str, Any] | None:
    """Recognise the platform from a public careers URL."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    m = re.match(r"(https://[^/]+)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+)", url)
    if m:
        return {"type": "oracle", "host": m.group(1), "site": m.group(2), "name": name or urlsplit(url).netloc}
    m = re.match(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
    if m:
        return {"type": "workday", "host": f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com",
                "tenant": m.group(1), "site": m.group(3), "name": name or m.group(1).upper()}
    m = re.match(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/?#]+)", url)
    if m:
        return {"type": "workday", "host": f"{m.group(2)}.{m.group(1)}.myworkdayjobs.com",
                "tenant": m.group(2), "site": m.group(3), "name": name or m.group(2).upper()}
    m = re.match(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)", url)
    if m:
        return {"type": "greenhouse", "board": m.group(1), "name": name or m.group(1)}
    m = re.match(r"https?://jobs(\.eu)?\.lever\.co/([^/?#]+)", url)
    if m:
        return {"type": "lever", "board": m.group(2), "eu": bool(m.group(1)), "name": name or m.group(2)}
    m = re.match(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", url)
    if m:
        return {"type": "ashby", "board": m.group(1), "name": name or m.group(1)}
    if "arbeitnow.com/api/job-board-api" in url:
        return {"type": "arbeitnow", "url": url, "name": name or "Arbeitnow"}
    if "pageuppeople.com" in url or re.search(r"/listing/?$", url):
        return {"type": "pageup", "url": url, "name": name or urlsplit(url).netloc}
    return {"type": "rss", "url": url, "name": name or urlsplit(url).netloc}


def in_budget(src: dict) -> bool:
    return time.time() < src.get("deadline", float("inf"))


def _run_keywords(src: dict, keywords: list[str], fn: Callable[[str], None]) -> None:
    """One query per keyword. Stops early if the first two fail or the time budget is used."""
    errors, done = [], 0
    for kw in keywords:
        if not in_budget(src):
            break
        try:
            fn(kw)
        except SourceError as exc:
            errors.append(str(exc))
        done += 1
        if done == 2 and len(errors) == 2:
            break
    if done and len(errors) == done:
        raise SourceError(errors[-1])


def fetch_oracle(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    host, site, org = src["host"], src["site"], src["name"]
    api = f"{host}/hcmRestApi/resources/latest"
    found: dict[str, dict] = {}

    def query(kw: str) -> None:
        offset = 0
        while offset < max_per_kw:
            finder = (f'findReqs;siteNumber={site},keyword="{safe_keyword(kw)}",limit=25,'
                      f"offset={offset},sortBy=POSTING_DATES_DESC")
            data = http("GET", f"{api}/recruitingCEJobRequisitions",
                        params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                                "finder": finder})
            items = data.get("items") or [] if isinstance(data, dict) else []
            reqs = (items[0].get("requisitionList") or []) if items else []
            new = 0
            for r in reqs:
                rid = str(r.get("Id") or "")
                if rid and rid not in found:
                    found[rid] = r
                    new += 1
            offset += 25
            if len(reqs) < 25 or new == 0:
                break

    _run_keywords(src, keywords, query)

    def end_date(rec: dict) -> str:
        for k, v in rec.items():
            if "enddate" in k.lower() and v:
                return parse_date(v)
        return ""

    jobs, details = [], 0
    for rid, r in found.items():
        job = {"title": r.get("Title"), "organization": org, "location": r.get("PrimaryLocation"),
               "country": r.get("PrimaryLocationCountry"), "posted_date": r.get("PostedDate"),
               "closing_date": end_date(r), "contract_type": r.get("WorkplaceType") or "",
               "description": r.get("ShortDescriptionStr") or "",
               "url": f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}", "source": org}
        if deep and details < DETAIL_LIMIT_PER_SOURCE and in_budget(src) and title_is_relevant(clean_text(job["title"])):
            details += 1
            try:
                d = http("GET", f"{api}/recruitingCEJobRequisitionDetails",
                         params={"onlyData": "true", "expand": "all",
                                 "finder": f'ById;Id="{rid}",siteNumber={site}'})
                item = (d.get("items") or [{}])[0] if isinstance(d, dict) else {}
                parts = [str(v) for k, v in item.items()
                         if isinstance(v, str) and k.endswith("Str") and len(v) > 40]
                if parts:
                    job["description"] = "\n\n".join(parts)
                job["closing_date"] = end_date(item) or job["closing_date"]
            except SourceError:
                pass
        jobs.append(job)
    return jobs


def fetch_workday(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    host, tenant, site, org = src["host"], src["tenant"], src["site"], src["name"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    found: dict[str, dict] = {}

    def query(kw: str) -> None:
        offset, total = 0, None
        while offset < max_per_kw:
            data = http("POST", f"{api}/jobs", body={"appliedFacets": {}, "limit": 20,
                                                      "offset": offset, "searchText": kw})
            if not isinstance(data, dict):
                raise SourceError("unexpected Workday response")
            if total is None:
                total = int(data.get("total") or 0)     # only the first page reports total
            posts = data.get("jobPostings") or []
            new = 0
            for p in posts:
                path = p.get("externalPath")
                if path and path not in found:
                    found[path] = p
                    new += 1
            offset += 20
            if not posts or new == 0 or offset >= total:   # Workday loops past the end
                break

    _run_keywords(src, keywords, query)

    jobs, details = [], 0
    for path, p in found.items():
        job = {"title": p.get("title"), "organization": org, "location": p.get("locationsText"),
               "posted_date": parse_relative_posted(p.get("postedOn", "")), "description": "",
               "url": f"https://{host}/en-US/{site}{path}", "source": org}
        if deep and details < DETAIL_LIMIT_PER_SOURCE and in_budget(src) and title_is_relevant(clean_text(job["title"])):
            details += 1
            try:
                info = (http("GET", f"{api}{path}") or {}).get("jobPostingInfo") or {}
                job["description"] = info.get("jobDescription") or ""
                job["location"] = info.get("location") or job["location"]
                job["posted_date"] = info.get("startDate") or job["posted_date"]
                job["closing_date"] = info.get("endDate") or ""
                job["contract_type"] = info.get("timeType") or ""
                job["url"] = safe_link(info.get("externalUrl")) or job["url"]
            except (SourceError, AttributeError):
                pass
        jobs.append(job)
    return jobs


CLOSING_RE = re.compile(r"(?:clos\w*|apply before|deadline)[^0-9]{0,30}"
                        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})", re.I)


def fetch_pageup(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    """PageUp listing page (UNICEF). Best effort: reads job links from the public HTML."""
    page = http("GET", src["url"], expect="text")
    base = "{0.scheme}://{0.netloc}".format(urlsplit(src["url"]))
    found: dict[str, dict] = {}
    anchors = list(re.finditer(r'<a[^>]+href="([^"]*/job/(\d+)[^"]*)"[^>]*>(.*?)</a>', page, re.I | re.S))
    for i, m in enumerate(anchors):
        href, jid, label = m.group(1), m.group(2), clean_text(m.group(3))
        if not label or label.lower() in ("read more", "apply", "apply now", "view"):
            continue
        end = anchors[i + 1].start() if i + 1 < len(anchors) else m.end() + 3000
        block = clean_text(page[m.end():min(end, m.end() + 3000)])
        if jid not in found or len(label) > len(found[jid]["title"]):
            close = CLOSING_RE.search(block)
            found[jid] = {"title": label, "organization": src["name"], "description": block,
                          "closing_date": parse_date(close.group(1)) if close else "",
                          "url": href if href.startswith("http") else base + href, "source": src["name"]}
    if not found:
        raise SourceError("no vacancies found on the listing page (layout may have changed)")
    jobs, details = list(found.values()), 0
    for job in jobs:
        if deep and details < DETAIL_LIMIT_PER_SOURCE and in_budget(src) and title_is_relevant(job["title"]):
            details += 1
            try:
                text = clean_text(http("GET", job["url"], expect="text"))
                if len(text) > len(job["description"]):
                    job["description"] = text[:15000]
                close = CLOSING_RE.search(text)
                if close and not job["closing_date"]:
                    job["closing_date"] = parse_date(close.group(1))
            except SourceError:
                pass
    return jobs


def fetch_arbeitnow(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    jobs = []
    for page in (1, 2):
        data = http("GET", src["url"], params={"page": page})
        rows = data.get("data") or [] if isinstance(data, dict) else []
        for j in rows:
            title = clean_text(j.get("title"))
            if not title_is_relevant(title):      # broad feed: keep only target role families
                continue
            jobs.append({"title": title, "organization": j.get("company_name"),
                         "location": "Remote" if j.get("remote") else j.get("location"),
                         "posted_date": j.get("created_at"), "description": j.get("description"),
                         "contract_type": ", ".join(j.get("job_types") or []),
                         "url": j.get("url"), "source": "Arbeitnow"})
        if not rows:
            break
    return jobs


def fetch_greenhouse(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    data = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs", params={"content": "true"})
    return [{"title": j.get("title"), "organization": j.get("company_name") or src["name"],
             "location": (j.get("location") or {}).get("name"), "url": j.get("absolute_url"),
             "posted_date": j.get("first_published") or j.get("updated_at"),
             "description": j.get("content"), "source": src["name"]} for j in data.get("jobs") or []]


def fetch_lever(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    host = "api.eu.lever.co" if src.get("eu") else "api.lever.co"
    data = http("GET", f"https://{host}/v0/postings/{src['board']}", params={"mode": "json"})
    if not isinstance(data, list):
        raise SourceError("unexpected Lever response")
    return [{"title": j.get("text"), "organization": src["name"],
             "location": (j.get("categories") or {}).get("location"), "url": j.get("hostedUrl"),
             "contract_type": (j.get("categories") or {}).get("commitment"),
             "posted_date": j.get("createdAt"),
             "description": (j.get("descriptionPlain") or "") + " " +
                            " ".join(clean_text(x.get("content")) for x in j.get("lists") or []),
             "source": src["name"]} for j in data]


def fetch_ashby(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    data = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}")
    return [{"title": j.get("title"), "organization": src["name"], "location": j.get("location"),
             "url": j.get("jobUrl"), "posted_date": j.get("publishedAt"),
             "contract_type": j.get("employmentType"),
             "description": j.get("descriptionPlain") or j.get("descriptionHtml"), "source": src["name"]}
            for j in data.get("jobs") or [] if j.get("isListed", True)]


def fetch_rss(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    text = http("GET", src["url"], expect="text")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise SourceError("not a valid RSS/Atom feed") from exc
    jobs = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        children = {c.tag.split("}")[-1]: c for c in node}

        def txt(*names: str) -> str:
            for n in names:
                el = children.get(n)
                if el is not None:          # Element truthiness is unreliable: compare with None
                    return el.text or ""
            return ""

        link = children.get("link")
        href = (link.get("href") or link.text or "") if link is not None else ""
        jobs.append({"title": txt("title"), "organization": src["name"], "url": href,
                     "posted_date": txt("pubDate", "published", "updated"),
                     "description": txt("description", "summary", "content"), "source": src["name"]})
    return jobs


FETCHERS: dict[str, Callable[..., list[dict]]] = {
    "oracle": fetch_oracle, "workday": fetch_workday, "pageup": fetch_pageup,
    "arbeitnow": fetch_arbeitnow, "greenhouse": fetch_greenhouse, "lever": fetch_lever,
    "ashby": fetch_ashby, "rss": fetch_rss,
}


def fetch_one(source: dict, keywords: list[str], deep: bool, max_per_kw: int) -> tuple[list[dict], dict]:
    started = time.time()
    spec = detect_source(source["url"], source["name"])
    health = {"source": source["name"], "platform": spec["type"] if spec else "invalid URL",
              "status": "OK", "jobs": 0, "seconds": 0.0, "message": ""}
    try:
        if not spec:
            raise SourceError("URL not recognised")
        spec["deadline"] = started + SOURCE_TIME_BUDGET
        jobs = FETCHERS[spec["type"]](spec, keywords, deep, max_per_kw)
        health["jobs"] = len(jobs)
        if not jobs:
            health["message"] = "No matching vacancies right now"
    except SourceError as exc:
        jobs, health["status"], health["message"] = [], "Failed", str(exc)
    except Exception as exc:  # unexpected format change must not crash the app
        jobs, health["status"], health["message"] = [], "Failed", f"{type(exc).__name__}: {exc}"[:200]
    health["seconds"] = round(time.time() - started, 1)
    return jobs, health


@st.cache_data(ttl=CACHE_HOURS * 3600, show_spinner=False)
def run_search(sources_json: str, keywords: tuple[str, ...], deep: bool, max_per_kw: int) -> dict:
    """Search all enabled sources in parallel. Cached, so it re-runs automatically every CACHE_HOURS."""
    sources = [s for s in json.loads(sources_json) if s.get("enabled")]
    raw: list[dict] = []
    health: list[dict] = []
    if sources:
        with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
            for jobs, h in pool.map(lambda s: fetch_one(s, list(keywords), deep, max_per_kw), sources):
                raw.extend(jobs)
                health.append(h)
    return {"jobs": raw, "health": health,
            "searched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


# =====================================================================
# 5. EXPORTS, TRACKER, PROMPTS
# =====================================================================

def csv_bytes(df: pd.DataFrame) -> bytes:
    """CSV protected against spreadsheet formula injection."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].astype(str).map(lambda v: "'" + v if v[:1] in ("=", "+", "-", "@") else v)
    return out.to_csv(index=False).encode("utf-8")


def shortlist_md(df: pd.DataFrame) -> str:
    lines = [f"# Damir Job Radar shortlist ({date.today().isoformat()})", ""]
    for _, r in df.head(30).iterrows():
        lines += [f"## {r['title']} | {r['organization']}",
                  f"- Score: {r['score']} ({r['category']})", f"- Grade: {r['grade'] or 'not stated'}",
                  f"- Location: {r['location'] or 'not stated'}", f"- Closing: {r['closing_date'] or 'not stated'}",
                  f"- Strengths: {r['strengths'] or '-'}", f"- Gaps: {r['gaps'] or '-'}",
                  f"- Verify: {r['uncertain'] or '-'}", f"- Link: {r['url']}", ""]
    return "\n".join(lines)


def tailoring_prompt(r: pd.Series) -> str:
    return (
        "Act as an HR prescreener and ATS specialist. Evaluate my fit for this vacancy and give an "
        "apply / do-not-apply recommendation. List hard requirements, gaps, ATS keywords and likely "
        "screening questions. Then draft a tailored CV and cover letter. Broaden the framing, not the "
        "facts. Do not invent experience.\n\n"
        f"Title: {r['title']}\nOrganization: {r['organization']}\nGrade: {r['grade']}\n"
        f"Location: {r['location']}\nClosing date: {r['closing_date']}\nLink: {r['url']}\n\n"
        f"Vacancy text:\n{str(r['description'])[:7000]}"
    )


def tracker() -> pd.DataFrame:
    if "tracker" not in st.session_state:
        st.session_state.tracker = pd.DataFrame(columns=["job_id", "state", "note"])
    return st.session_state.tracker


def set_state_cb(job_id: str, widget_key: str) -> None:
    """Selectbox callback: update tracker without a rerun loop."""
    state = st.session_state.get(widget_key, "")
    t = tracker()
    t = t[t["job_id"] != job_id]
    if state:
        t = pd.concat([t, pd.DataFrame([{"job_id": job_id, "state": state, "note": ""}])], ignore_index=True)
    st.session_state.tracker = t.reset_index(drop=True)


BADGE = {"Excellent fit": "🟢", "Strong fit": "🟢", "Good fit": "🟡", "Possible fit": "🟠",
         "Review required": "🔵", "Low priority": "⚪", "Disqualified": "🔴"}


def job_card(r: pd.Series, tab: str, track: bool = True) -> None:
    dl = pd.to_numeric(pd.Series([r.get("days_left")]), errors="coerce").iloc[0]
    when = "no closing date" if pd.isna(dl) else ("closes today" if int(dl) == 0 else f"{int(dl)} days left")
    key = hashlib.md5(f"{tab}|{r['job_id']}".encode()).hexdigest()[:12]
    with st.expander(f"{int(r['score']):>3} · {r['title']} · {r['organization']} · {when}"):
        left, right = st.columns([4, 1])
        with left:
            st.markdown(f"{BADGE.get(r['category'], '⚪')} **{r['category']}** · **{int(r['score'])}/100**"
                        + (f" · Grade **{r['grade']}**" if r["grade"] else ""))
            st.caption(f"{r['location'] or 'Location not stated'} · Posted {r['posted_date'] or 'n/a'} · "
                       f"Closes {r['closing_date'] or 'not stated'} · Source: {r['source']}")
            for head, field in (("Strengths", "strengths"), ("Gaps", "gaps"), ("Verify", "uncertain")):
                if r[field]:
                    st.markdown(f"**{head}**\n" + "\n".join(f"- {x}" for x in str(r[field]).split(" | ")))
            st.caption("Score breakdown: " + str(r["breakdown"]))
        with right:
            if r["url"]:
                st.link_button("Open / Apply", r["url"], width="stretch")
            if track:
                cur_s = tracker().loc[tracker()["job_id"] == r["job_id"], "state"]
                cur = cur_s.iloc[0] if len(cur_s) and cur_s.iloc[0] in TRACKER_STATES else ""
                wkey = f"trk_{key}"
                st.session_state[wkey] = cur      # keep every copy of this job in sync with the tracker
                st.selectbox("Status", TRACKER_STATES, key=wkey, on_change=set_state_cb, args=(r["job_id"], wkey))
        c1, c2 = st.columns(2)
        with c1.popover("Vacancy text", width="stretch"):
            st.text(str(r["description"])[:8000] or "No description captured. Open the vacancy link.")
        with c2.popover("CV tailoring prompt", width="stretch"):
            st.code(tailoring_prompt(r), language=None)


def show_cards(df: pd.DataFrame, tab: str, track: bool = True, page_size: int = 25) -> None:
    if df.empty:
        st.info("No vacancies here under the current filters.")
        return
    pages = max(1, -(-len(df) // page_size))
    page = st.selectbox("Page", list(range(1, pages + 1)), key=f"page_{tab}_{pages}") if pages > 1 else 1
    for _, r in df.iloc[(page - 1) * page_size: page * page_size].iterrows():
        job_card(r, tab, track)


# =====================================================================
# 6. USER INTERFACE
# =====================================================================

st.set_page_config(page_title=APP_TITLE, page_icon="🎯", layout="wide")

ss = st.session_state
ss.setdefault("keywords", "\n".join(DEFAULT_KEYWORDS))
if "pending_keywords" in ss:              # applied before the widget exists
    ss.keywords = ss.pop("pending_keywords")
ss.setdefault("sources", [dict(s) for s in DEFAULT_SOURCES])
ss.setdefault("extra_jobs", [])          # imported CSV + analysed vacancies
ss.setdefault("analysis", None)
tracker()

st.title("🎯 Damir Job Radar")

# ---------------- Sidebar: search settings ----------------
with st.sidebar:
    st.header("Automatic search")
    kw_text = st.text_area("Search keywords (one per line)", key="keywords", height=180)
    keywords = tuple(dict.fromkeys(k.strip() for k in kw_text.splitlines() if k.strip()))[:20]
    deep = st.toggle("Read full vacancy texts (slower, more accurate)", value=True)
    depth = st.select_slider("Results per keyword per site", options=[20, 40, 60], value=20)
    if st.button("🔄 Search now", type="primary", width="stretch"):
        run_search.clear()
    st.caption(f"Runs automatically on open and refreshes every {CACHE_HOURS} hours.")

    st.header("Filters")
    q = st.text_input("Search in results")
    min_score = st.slider("Minimum score", 0, 100, 55, 5)
    hide_review = st.checkbox("Hide 'Review required'", value=False)
    loc_q = st.text_input("Location contains")
    window = st.selectbox("Closing within", ["Any", "7 days", "14 days", "30 days"])
    hide_rejected = st.checkbox("Hide rejected / not interested", value=True)

# ---------------- Search ----------------
with st.spinner("Searching UN agencies and job sites. First run can take up to a minute..."):
    result = run_search(json.dumps(ss.sources, sort_keys=True), keywords, deep, depth)

all_raw = result["jobs"] + ss.extra_jobs
jobs = score_jobs(all_raw)
health = pd.DataFrame(result["health"])
failed = health[health["status"] != "OK"] if not health.empty else health

st.caption(f"Last internet search: **{result['searched_at']}** · "
           f"{len(result['jobs'])} vacancies collected · {len(jobs)} active after de-duplication and expiry check")
if not health.empty and len(failed) == len(health):
    st.error("All sources failed. Check the Sources tab. You can still analyse vacancies or import a CSV.")
elif len(failed):
    st.warning("Some sources failed: " + ", ".join(failed["source"]) + ". Details in the Sources tab.")

# ---------------- Filtering ----------------
f = jobs[(jobs["score"] >= min_score) & (jobs["category"] != "Disqualified")].copy()
if hide_review:
    f = f[f["category"] != "Review required"]
if q:
    blob = (f["title"] + " " + f["organization"] + " " + f["description"]).str.lower()
    f = f[blob.str.contains(q.lower(), regex=False)]
if loc_q:
    f = f[(f["location"] + " " + f["country"]).str.contains(loc_q, case=False, regex=False)]
if window != "Any":
    d = pd.to_numeric(f["days_left"], errors="coerce")
    f = f[d.between(0, int(window.split()[0]))]
if hide_rejected and not tracker().empty:
    hidden = tracker().loc[tracker()["state"].isin(["Rejected", "Not interested"]), "job_id"]
    f = f[~f["job_id"].isin(hidden)]

posted = pd.to_datetime(jobs["posted_date"], errors="coerce")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Active vacancies", len(jobs))
m2.metric("Excellent / strong", int(jobs["category"].isin(["Excellent fit", "Strong fit"]).sum()))
m3.metric("New (7 days)", int((posted >= pd.Timestamp(date.today() - timedelta(days=7))).sum()))
m4.metric("Closing ≤ 7 days", int(pd.to_numeric(jobs["days_left"], errors="coerce").between(0, 7).sum()))
m5.metric("Shown", len(f))

tabs = st.tabs(["⭐ Best matches", "📋 All results", "🆕 New", "⏰ Closing soon", "📌 My tracker",
                "🔍 Analyze vacancy", "📥 Import CSV", "🌐 Sources", "❓ Help"])

with tabs[0]:
    best = f[f["score"] >= 75]
    st.caption("Score 75+ · 🔵 Review required = strong match with a possible eligibility, licence or language issue.")
    show_cards(best, "best")

with tabs[1]:
    if f.empty:
        st.info("No vacancies match the filters. Lower the minimum score or add keywords.")
    else:
        st.dataframe(f[["score", "category", "grade", "title", "organization", "location",
                        "closing_date", "source", "url"]], hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Link", display_text="Open"),
                                    "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d")})
        c1, c2, c3 = st.columns(3)
        c1.download_button("Download filtered CSV", csv_bytes(f), "damir-jobs.csv", "text/csv", width="stretch")
        c2.download_button("Download shortlist (Markdown)", shortlist_md(f), "damir-shortlist.md", "text/markdown", width="stretch")
        c3.download_button("Download all incl. disqualified", csv_bytes(jobs), "damir-jobs-all.csv", "text/csv", width="stretch")
        show_cards(f, "all")

with tabs[2]:
    fp = pd.to_datetime(f["posted_date"], errors="coerce")
    show_cards(f[fp >= pd.Timestamp(date.today() - timedelta(days=7))], "new")

with tabs[3]:
    d = pd.to_numeric(f["days_left"], errors="coerce")
    soon = f[d.between(0, 14)]
    show_cards(soon.sort_values("days_left", key=lambda c: pd.to_numeric(c, errors="coerce")), "soon")

with tabs[4]:
    t = tracker()
    st.caption("Statuses live in this browser session. Download the tracker before closing; upload it next time.")
    if t.empty:
        st.info("Set a status (Saved, Applied...) inside any job card.")
    else:
        merged = t.merge(jobs[["job_id", "title", "organization", "closing_date", "score", "url"]], on="job_id", how="left")
        st.dataframe(merged, hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Link", display_text="Open")})
        st.download_button("Download tracker", csv_bytes(t), "damir-tracker.csv", "text/csv")
    up = st.file_uploader("Restore tracker CSV", type="csv", key="trk_up")
    if up is not None and st.button("Restore tracker"):
        try:
            r = pd.read_csv(up, dtype=str).fillna("")
            if {"job_id", "state"}.issubset(r.columns):
                r["job_id"] = r["job_id"].str.lstrip("'")
                if "note" not in r.columns:
                    r["note"] = ""
                ss.tracker = r.loc[r["state"].isin(TRACKER_STATES[1:]), ["job_id", "state", "note"]].reset_index(drop=True)
                st.success(f"Restored {len(ss.tracker)} entries.")
                st.rerun()
            else:
                st.error("The CSV needs job_id and state columns.")
        except Exception as exc:
            st.error(f"Cannot read the file: {exc}")

with tabs[5]:
    st.write("Paste any vacancy (LinkedIn, UN Careers, UNOPS, email alert...) to score it instantly.")
    with st.form("analyze"):
        a_title = st.text_input("Job title *")
        a_org = st.text_input("Organization *")
        a_loc = st.text_input("Location")
        a_close = st.date_input("Closing date", value=None)
        a_url = st.text_input("Vacancy URL")
        a_desc = st.text_area("Full vacancy text *", height=280)
        go = st.form_submit_button("Analyze", type="primary")
    if go:
        if not (a_title.strip() and a_org.strip() and a_desc.strip()):
            st.error("Title, organization and vacancy text are required.")
            ss.analysis = None
        else:
            ss.analysis = {"title": a_title, "organization": a_org, "location": a_loc,
                           "closing_date": a_close.isoformat() if a_close else "", "url": a_url,
                           "description": a_desc, "source": "Manual paste"}
    if ss.analysis:
        res = score_job(ss.analysis)
        if res["status"] == "Expired":
            st.warning("The closing date has passed.")
        job_card(pd.Series(res), "analysis", track=False)
        if st.button("Add this vacancy to my results"):
            ss.extra_jobs = ss.extra_jobs + [ss.analysis]
            ss.analysis = None
            st.success("Added. It now appears in the result tabs for this session.")
            st.rerun()

with tabs[6]:
    st.write("Upload a CSV with columns **title, organization, description** (required) and optionally "
             "location, country, contract_type, posted_date, closing_date, url.")
    tmpl = pd.DataFrame([{c: "" for c in IMPORT_COLUMNS}])
    tmpl.loc[0, ["title", "organization", "location", "closing_date", "url", "description"]] = [
        "Construction Engineer", "Example Organization", "Sarajevo", "2027-01-31",
        "https://example.org/vacancy", "Paste the full vacancy text here."]
    st.download_button("Download CSV template", csv_bytes(tmpl), "job-import-template.csv", "text/csv")
    up_jobs = st.file_uploader("Upload jobs CSV", type="csv", key="jobs_up")
    if up_jobs is not None and st.button("Import and score"):
        try:
            imp = pd.read_csv(up_jobs, dtype=str).fillna("")
            missing = [c for c in ("title", "organization", "description") if c not in imp.columns]
            if missing:
                st.error("Missing columns: " + ", ".join(missing))
            else:
                ok = imp[(imp["title"].str.strip() != "") & (imp["organization"].str.strip() != "")]
                rows = [{**{c: row.get(c, "") for c in IMPORT_COLUMNS}, "source": "CSV import"}
                        for _, row in ok.iterrows()]
                ss.extra_jobs = ss.extra_jobs + rows
                st.success(f"Imported {len(rows)} rows ({len(imp) - len(ok)} skipped for missing title/organization).")
                st.rerun()
        except Exception as exc:
            st.error(f"Cannot read the CSV: {exc}")
    if ss.extra_jobs and st.button("Remove imported / analysed vacancies"):
        ss.extra_jobs = []
        st.rerun()

with tabs[7]:
    st.subheader("Source health (last search)")
    if not health.empty:
        st.dataframe(health, hide_index=True, width="stretch")
    st.subheader("Choose sources")
    edited = st.data_editor(pd.DataFrame(ss.sources, columns=["enabled", "name", "url"]), hide_index=True,
                            width="stretch", num_rows="dynamic", key="src_editor",
                            column_config={"enabled": st.column_config.CheckboxColumn("On"),
                                           "url": st.column_config.TextColumn("Public careers URL", width="large")})
    st.caption("Add a row with any careers page from Workday (myworkdayjobs.com), Oracle "
               "(…oraclecloud.com/hcmUI/CandidateExperience/…/sites/…), Greenhouse, Lever, Ashby, "
               "or an RSS/Atom feed URL.")
    if st.button("Save sources and search", type="primary"):
        new = []
        for _, row in edited.fillna("").iterrows():
            url, name = str(row["url"]).strip(), str(row["name"]).strip()
            if not url:
                continue
            spec = detect_source(url, name)
            if spec is None:
                st.error(f"Not a valid URL: {url}")
                st.stop()
            enabled = True if row["enabled"] in ("", None) else bool(row["enabled"])   # new rows default to on
            new.append({"name": name or spec["name"], "url": url, "enabled": enabled})
        ss.sources = new
        run_search.clear()
        st.rerun()
    if st.button("Reset to default sources"):
        ss.sources = [dict(s) for s in DEFAULT_SOURCES]
        run_search.clear()
        st.rerun()
    st.download_button("Download my settings", json.dumps({"keywords": list(keywords), "sources": ss.sources}, indent=2),
                       "damir-radar-settings.json", "application/json")
    set_up = st.file_uploader("Load settings JSON", type="json", key="set_up")
    if set_up is not None and st.button("Apply settings"):
        try:
            cfg = json.load(set_up)
            srcs = [s for s in cfg.get("sources", []) if isinstance(s, dict) and detect_source(s.get("url", ""))]
            ss.sources = [{"name": s.get("name", ""), "url": s["url"], "enabled": bool(s.get("enabled", True))} for s in srcs]
            if cfg.get("keywords"):
                ss.pending_keywords = "\n".join(str(k) for k in cfg["keywords"])
            run_search.clear()
            st.rerun()
        except Exception as exc:
            st.error(f"Invalid settings file: {exc}")

with tabs[8]:
    st.markdown(f"""
**How it works**
1. On opening, the app searches every enabled source for your keywords (cached for {CACHE_HOURS} hours; **Search now** forces a fresh search).
2. For the most relevant titles it reads the full vacancy text, then scores each job against your profile.
3. Duplicates and expired vacancies are removed. Disqualified roles are hidden from the main tabs (they remain in the full CSV).

**Score (100)**: title 20 · competencies 25 · organization/sector 15 · experience 15 · education 10 · location/eligibility 5 · language 5 · contract 5.

- 🔴 **Disqualified**: unrelated role family or explicit nationality restriction.
- 🔵 **Review required**: strong match, but a possible blocker: national/local post outside Bosnia and Herzegovina, licence, or a language you do not list.
- **Verify** items are unknowns. They are never counted as matched.

**Sources**: UNDP and IOM (Oracle), WFP and UNHCR (Workday), UNICEF (PageUp listing, best effort), Arbeitnow (Europe, [arbeitnow.com](https://www.arbeitnow.com)).
Career sites change without notice. If a source fails, the Sources tab shows why, and the others keep working.
Not covered automatically: LinkedIn, Indeed, UN Careers (Inspira), UNOPS. Use **Analyze vacancy** for those.
""")
