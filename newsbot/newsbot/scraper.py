"""Direct website scraper (NO RSS). Finds article links on section pages, opens each
new article, reads its publish time from page metadata and keeps only fresh ones."""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse

log = logging.getLogger("scraper")

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

# category: india | world | business   (used for topic/section routing)
SOURCES = [
    dict(name="The Hindu", domain="thehindu.com", pattern=r"/article\d+\.ece",
         pages=[("india", "https://www.thehindu.com/news/national/"),
                ("world", "https://www.thehindu.com/news/international/"),
                ("business", "https://www.thehindu.com/business/"),
                ("india", "https://www.thehindu.com/news/cities/"),
                ("sports", "https://www.thehindu.com/sport/")]),
    dict(name="Hindustan Times", domain="hindustantimes.com", pattern=r"-\d{9,}\.html$",
         pages=[("india", "https://www.hindustantimes.com/india-news"),
                ("world", "https://www.hindustantimes.com/world-news"),
                ("business", "https://www.hindustantimes.com/business"),
                ("india", "https://www.hindustantimes.com/cities"),
                ("sports", "https://www.hindustantimes.com/cricket")]),
    dict(name="Al Jazeera", domain="aljazeera.com", pattern=r"/\d{4}/\d{1,2}/\d{1,2}/",
         pages=[("world", "https://www.aljazeera.com/news/"),
                ("india", "https://www.aljazeera.com/where/india/"),
                ("business", "https://www.aljazeera.com/economy/")]),
    dict(name="BBC", domain="bbc.com", pattern=r"/news/(articles/|[a-z\-]+-\d{6,})",
         pages=[("india", "https://www.bbc.com/news/world/asia/india"),
                ("world", "https://www.bbc.com/news/world"),
                ("business", "https://www.bbc.com/news/business")]),
    # IANS / PTI: pattern is a generic "long slug" guess -> tune after running selftest
    dict(name="IANS", domain="ianslive.in", pattern=r"/[^/]{25,}$",
         pages=[("india", "https://ianslive.in/")]),
    dict(name="The Quint", domain="thequint.com", pattern=r"/[a-z0-9\-]{25,}$",
         pages=[("india", "https://www.thequint.com/news/india"),
                ("world", "https://www.thequint.com/news/world")]),
    dict(name="The Indian Express", domain="indianexpress.com", pattern=r"/article/",
         pages=[("india", "https://indianexpress.com/section/india/"),
                ("world", "https://indianexpress.com/section/world/"),
                ("business", "https://indianexpress.com/section/business/"),
                ("india", "https://indianexpress.com/section/cities/"),
                ("sports", "https://indianexpress.com/section/sports/")]),
    dict(name="Mint", domain="livemint.com", pattern=r"-\d{10,}\.html$",
         pages=[("india", "https://www.livemint.com/latest-news"),
                ("world", "https://www.livemint.com/news/world"),
                ("business", "https://www.livemint.com/market")]),
    dict(name="Times of India", domain="indiatimes.com", pattern=r"/articleshow/\d+\.cms",
         pages=[("india", "https://timesofindia.indiatimes.com/india"),
                ("world", "https://timesofindia.indiatimes.com/world"),
                ("business", "https://timesofindia.indiatimes.com/business"),
                ("india", "https://timesofindia.indiatimes.com/city"),
                ("sports", "https://timesofindia.indiatimes.com/sports")]),
    dict(name="The Wire", domain="thewire.in", pattern=r"thewire\.in/[a-z\-]+/[a-z0-9\-]{20,}",
         pages=[("india", "https://thewire.in/")]),
    dict(name="Scroll.in", domain="scroll.in", pattern=r"scroll\.in/(article|latest|global)/\d+",
         pages=[("india", "https://scroll.in/latest/")]),
    dict(name="PTI", domain="ptinews.com", pattern=r"/[^/]{25,}$",
         pages=[("india", "https://www.ptinews.com/")]),
]

META_DATE_KEYS = [
    "article:published_time", "og:article:published_time", "datePublished",
    "pubdate", "publish-date", "publish_date", "parsely-pub-date",
    "DC.date.issued", "date", "article:modified_time",
]


@dataclass
class Article:
    url: str
    title: str
    summary: str
    published: datetime  # aware, UTC
    source: str
    category: str
    rank: int = 99  # position on the section page (0 = top)
    tags: set = field(default_factory=set)
    also: list = field(default_factory=list)
    image: str = ""


def _get(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
        log.warning("HTTP %s for %s", r.status_code, url)
    except requests.RequestException as e:
        log.warning("fetch failed %s: %s", url, e)
    return None


def _norm(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def _to_utc(s):
    try:
        dt = dtparse.parse(s)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("datePublished", "dateCreated") and isinstance(v, str):
                yield v
            else:
                yield from _walk(v)
    elif isinstance(o, list):
        for i in o:
            yield from _walk(i)


def _published(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for s in _walk(data):
            dt = _to_utc(s)
            if dt:
                return dt
    for key in META_DATE_KEYS:
        tag = (soup.find("meta", attrs={"property": key})
               or soup.find("meta", attrs={"name": key})
               or soup.find("meta", attrs={"itemprop": key}))
        if tag and tag.get("content"):
            dt = _to_utc(tag["content"])
            if dt:
                return dt
    t = soup.find("time", attrs={"datetime": True})
    if t:
        return _to_utc(t["datetime"])
    return None


def _meta(soup, *keys):
    for k in keys:
        tag = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def parse_article(html, url, source, category):
    soup = BeautifulSoup(html, "lxml")
    published = _published(soup)
    if not published:
        return None
    title = _meta(soup, "og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.string or "").strip()
    title = re.split(r"\s+[|\u2013\u2014]\s+", title)[0].strip()
    summary = _meta(soup, "og:description", "description", "twitter:description")
    if summary.strip().lower() == title.strip().lower():
        summary = ""
    if not title:
        return None
    image = _meta(soup, "og:image", "twitter:image", "og:image:url")
    image = urljoin(url, image) if image else ""
    return Article(url, title, summary, published, source, category, image=image)


def _links(html, base, src):
    soup = BeautifulSoup(html, "lxml")
    pat = re.compile(src["pattern"])
    out = []
    for a in soup.find_all("a", href=True):
        u = _norm(urljoin(base, a["href"]))
        host = urlparse(u).netloc
        if src["domain"] in host and pat.search(u) and u not in out:
            out.append(u)
    return out


def fetch_fresh(is_seen, max_age_min=45, per_source=30, workers=8):
    """Returns (fresh_articles_oldest_first, old_urls_to_mark_seen)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_age_min)

    def listing(job):
        src, cat, page = job
        html = _get(page)
        return src, cat, (_links(html, page, src) if html else [])

    jobs = [(s, c, p) for s in SOURCES for c, p in s["pages"]]
    cands, per_count = {}, {}
    with ThreadPoolExecutor(workers) as ex:
        for src, cat, links in ex.map(listing, jobs):
            log.info("%s/%s: %d links", src["name"], cat, len(links))
            for pos, u in enumerate(links):
                if u in cands or is_seen(u):
                    continue
                if per_count.get(src["name"], 0) >= per_source:
                    break
                cands[u] = (src["name"], cat, pos)
                per_count[src["name"]] = per_count.get(src["name"], 0) + 1

    def article_job(item):
        url, (name, cat, pos) = item
        html = _get(url)
        art = parse_article(html, url, name, cat) if html else None
        if art:
            art.rank = pos
        return url, art

    fresh, old = [], []
    with ThreadPoolExecutor(workers) as ex:
        for url, art in ex.map(article_job, cands.items()):
            if art is None:
                continue  # no time found -> skip, retry next cycle
            if cutoff <= art.published <= now + timedelta(minutes=15):
                fresh.append(art)
            else:
                old.append(url)  # too old -> never post, never refetch
    fresh.sort(key=lambda a: a.published)
    log.info("candidates=%d fresh=%d", len(cands), len(fresh))
    return fresh, old


if __name__ == "__main__":
    # Selftest:  python scraper.py   -> shows what each source returns right now
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    arts, _ = fetch_fresh(lambda u: False, max_age_min=180)
    by = {}
    for a in arts:
        by.setdefault(a.source, []).append(a)
    for s in SOURCES:
        items = by.get(s["name"], [])
        print(f"\n== {s['name']}: {len(items)} fresh (last 3h)")
        for a in items[-3:]:
            print(f"  {a.published.astimezone(IST):%H:%M IST} | {a.title[:80]}")
