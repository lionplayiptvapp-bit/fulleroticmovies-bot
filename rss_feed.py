from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup, Tag


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.fulleroticmovies.net"
NEWEST_URL = f"{BASE_URL}/videos/"
CATEGORIES_URL = f"{BASE_URL}/categories/"

DATABASE_FILE = "feed.db"
RSS_FILE = "docs/feed.xml"

CHECK_INTERVAL_SECONDS = 600
REQUEST_DELAY_SECONDS = 2.0

MAX_ITEMS_IN_FEED = 200

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 "
        "RSSFeedGenerator/1.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# VERİ MODELLERİ
# ============================================================

@dataclass
class Article:
    title: str
    url: str
    image_url: str = ""
    description: str = ""
    categories: list[str] = None
    discovered_at: str = ""


# ============================================================
# VERİTABANI
# ============================================================

def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def init_database() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                image_url TEXT,
                description TEXT,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS article_categories (
                article_url TEXT NOT NULL,
                category TEXT NOT NULL,
                UNIQUE(article_url, category)
            );
            """
        )
        conn.commit()


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    path = re.sub(r"/+", "/", parsed.path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") + "/"
    return f"{scheme}://{hostname}{path}"


def article_exists(url: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE url = ?",
            (normalize_url(url),),
        ).fetchone()
    return row is not None


def save_article(article: Article) -> bool:
    normalized = normalize_url(article.url)
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO articles (title, url, image_url, description) VALUES (?, ?, ?, ?)",
            (article.title, normalized, article.image_url, article.description),
        )
        is_new = cursor.rowcount > 0

        if article.categories:
            for cat in article.categories:
                conn.execute(
                    "INSERT OR IGNORE INTO article_categories (article_url, category) VALUES (?, ?)",
                    (normalized, cat),
                )
        conn.commit()
    return is_new


def get_recent_articles(limit: int = MAX_ITEMS_IN_FEED) -> list[Article]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT a.title, a.url, a.image_url, a.description, a.discovered_at,
                   GROUP_CONCAT(c.category, ', ') AS categories
            FROM articles a
            LEFT JOIN article_categories c ON a.url = c.article_url
            GROUP BY a.url
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        Article(
            title=row["title"],
            url=row["url"],
            image_url=row["image_url"] or "",
            description=row["description"] or "",
            categories=row["categories"].split(", ") if row["categories"] else [],
            discovered_at=row["discovered_at"],
        )
        for row in rows
    ]


# ============================================================
# HTTP
# ============================================================

def get_soup(url: str) -> BeautifulSoup:
    logging.info("Sayfa alınıyor: %s", url)
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


# ============================================================
# İÇERİK AYRIŞTIRMA
# ============================================================

VIDEO_URL_PATTERN = re.compile(
    r"^https://www\.fulleroticmovies\.net/video/[^/]+/?$",
    re.IGNORECASE,
)


def find_container(anchor: Tag) -> Tag:
    current: Tag | None = anchor
    for _ in range(6):
        if current is None:
            break
        if current.name in {"article", "li", "figure"}:
            return current
        classes = " ".join(current.get("class", [])).lower()
        if any(w in classes for w in ("video", "movie", "item", "card", "thumb", "post")):
            return current
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        current = parent
    return anchor.parent if isinstance(anchor.parent, Tag) else anchor


def extract_image(container: Tag, page_url: str) -> str:
    image = container.select_one("img")
    if not image:
        return ""
    candidates = [
        image.get("data-src"),
        image.get("data-lazy-src"),
        image.get("data-original"),
        image.get("src"),
    ]
    srcset = image.get("srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        candidates.insert(0, first)
    for c in candidates:
        if c and not c.startswith("data:"):
            return urljoin(page_url, c)
    return ""


def scrape_listing(page_url: str, source_name: str = "Newest") -> list[Article]:
    soup = get_soup(page_url)
    articles: dict[str, Article] = {}

    for anchor in soup.select("a[href]"):
        href = normalize_url(urljoin(page_url, anchor.get("href", "")))
        if not VIDEO_URL_PATTERN.match(href):
            continue

        title = anchor.get_text(" ", strip=True)
        container = find_container(anchor)

        if not title:
            heading = container.select_one("h1, h2, h3, h4, .title")
            if heading:
                title = heading.get_text(" ", strip=True)

        if not title:
            image = container.select_one("img")
            if image:
                title = (image.get("alt") or image.get("title") or "").strip()

        if not title:
            continue

        image_url = extract_image(container, page_url)
        articles[href] = Article(
            title=title,
            url=href,
            image_url=image_url,
            description="",
            categories=[source_name] if source_name != "Newest" else [],
        )

    return list(articles.values())


def scrape_video_page(url: str) -> tuple[str, list[str]]:
    """Video sayfasından açıklama ve kategori etiketlerini çeker."""
    try:
        soup = get_soup(url)

        description = ""
        desc_el = soup.select_one(".description, .content-description, .video-description, p")
        if desc_el:
            description = desc_el.get_text(" ", strip=True)[:500]

        categories = []
        for tag in soup.select("a[href*='/category/']"):
            cat_name = tag.get_text(" ", strip=True)
            if cat_name:
                categories.append(cat_name)

        return description, categories
    except Exception as e:
        logging.warning("Video sayfası alınamadı: %s | %s", url, e)
        return "", []


# ============================================================
# KATEGORİ KEŞFİ
# ============================================================

CATEGORY_URL_PATTERN = re.compile(
    r"^https://www\.fulleroticmovies\.net/category/([^/]+)/?$",
    re.IGNORECASE,
)


def discover_categories() -> list[tuple[str, str]]:
    """Returns list of (name, url) tuples."""
    soup = get_soup(CATEGORIES_URL)
    categories: dict[str, tuple[str, str]] = {}

    for anchor in soup.select("a[href]"):
        href = normalize_url(urljoin(CATEGORIES_URL, anchor.get("href", "")))
        match = CATEGORY_URL_PATTERN.match(href)
        if not match:
            continue
        slug = match.group(1).lower()
        raw_name = anchor.get_text(" ", strip=True)
        name = re.sub(r"\s+\d[\d,.Kk]*\s*$", "", raw_name).strip()
        if not name:
            name = slug.replace("-", " ").title()
        categories[slug] = (name, f"{BASE_URL}/category/{slug}/")

    return sorted(categories.values(), key=lambda x: x[0].lower())


# ============================================================
# RSS GENERATION
# ============================================================

def generate_rss() -> None:
    articles = get_recent_articles(MAX_ITEMS_IN_FEED)

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    items_xml = []
    for article in articles:
        pub_date = article.discovered_at
        try:
            dt = datetime.strptime(pub_date, "%Y-%m-%d %H:%M:%S")
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except (ValueError, TypeError):
            pub_date = now

        description = article.description or ""
        if article.image_url:
            description = f'<img src="{escape(article.image_url)}" /><br/>{escape(description)}'

        categories_xml = ""
        if article.categories:
            categories_xml = "\n".join(
                f"      <category>{escape(cat)}</category>"
                for cat in article.categories
            ) + "\n"

        items_xml.append(f"""    <item>
      <title>{escape(article.title)}</title>
      <link>{escape(article.url)}</link>
      <guid isPermaLink="true">{escape(article.url)}</guid>
      <description>{escape(description) if not article.image_url else f'<![CDATA[{description}]]>'}</description>
      <pubDate>{pub_date}</pubDate>{categories_xml}
    </item>""")

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Full Erotic Movies - Newest</title>
    <link>{BASE_URL}/videos/</link>
    <description>Latest content from Full Erotic Movies</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <atom:link href="https://lionplayiptvapp-bit.github.io/fulleroticmovies-bot/feed.xml" rel="self" type="application/rss+xml" />
{chr(10).join(items_xml)}
  </channel>
</rss>
"""

    os.makedirs(os.path.dirname(RSS_FILE), exist_ok=True)
    with open(RSS_FILE, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    logging.info("RSS feed oluşturuldu: %s (%s içerik)", RSS_FILE, len(articles))


# ============================================================
# CHECK / SCRAPE
# ============================================================

def check_newest() -> int:
    """Newest sayfasını kontrol et, yeni içerik varsa DB'ye ekle. Yeni içerik sayısını döndür."""
    articles = scrape_listing(NEWEST_URL, "Newest")
    new_count = 0

    for article in articles:
        if not article_exists(article.url):
            saved = save_article(article)
            if saved:
                new_count += 1
                logging.info("Yeni içerik: %s", article.title)
        time.sleep(0.5)

    return new_count


def run_check_once() -> None:
    init_database()
    new_count = check_newest()
    logging.info("Yeni içerik sayısı: %s", new_count)
    generate_rss()


def run_watch() -> None:
    init_database()
    while True:
        try:
            run_check_once()
        except Exception:
            logging.exception("Kontrol başarısız.")
        logging.info("%s saniye bekleniyor.", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)


# ============================================================
# ARCHIVE
# ============================================================

def paginated_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url.rstrip("/") + "/"
    return f"{base_url.rstrip('/')}/{page}/"


def archive_newest(max_pages: int) -> None:
    init_database()
    for page in range(1, max_pages + 1):
        url = paginated_url(NEWEST_URL, page)
        try:
            articles = scrape_listing(url, "Newest")
            if not articles:
                break
            for a in articles:
                save_article(a)
            logging.info("Sayfa %s: %s içerik", page, len(articles))
        except Exception:
            logging.exception("Sayfa %s alınamadı", page)
            break
        time.sleep(REQUEST_DELAY_SECONDS)
    generate_rss()


def archive_all_categories(max_pages: int) -> None:
    init_database()
    categories = discover_categories()
    logging.info("%s kategori bulundu", len(categories))

    for idx, (name, url) in enumerate(categories, 1):
        logging.info("[%s/%s] Arşivleniyor: %s", idx, len(categories), name)
        for page in range(1, max_pages + 1):
            page_url = paginated_url(url, page)
            try:
                articles = scrape_listing(page_url, name)
                if not articles:
                    break
                for a in articles:
                    a.categories = [name]
                    save_article(a)
            except Exception:
                logging.exception("Sayfa alınamadı: %s sayfa %s", name, page)
                break
            time.sleep(REQUEST_DELAY_SECONDS)
        time.sleep(REQUEST_DELAY_SECONDS)

    generate_rss()


# ============================================================
# CLI
# ============================================================

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="RSS Feed Generator")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-once", help="Tek seferlik kontrol + RSS güncelle")
    sub.add_parser("watch", help="Sürekli çalış")
    sub.add_parser("generate-rss", help="Sadece RSS XML üret")

    archive_newest_p = sub.add_parser("archive-newest", help="Newest sayfalarını arşivle")
    archive_newest_p.add_argument("--pages", type=int, default=10)

    archive_all_p = sub.add_parser("archive-all", help="Tüm kategorileri arşivle")
    archive_all_p.add_argument("--pages", type=int, default=10)

    args = parser.parse_args()

    if args.command == "check-once":
        run_check_once()
    elif args.command == "watch":
        run_watch()
    elif args.command == "generate-rss":
        init_database()
        generate_rss()
    elif args.command == "archive-newest":
        archive_newest(args.pages)
    elif args.command == "archive-all":
        archive_all_categories(args.pages)


if __name__ == "__main__":
    main()
