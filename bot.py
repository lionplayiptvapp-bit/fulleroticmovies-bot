from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


# ============================================================
# AYARLAR
# ============================================================

BASE_URL = "https://www.fulleroticmovies.net"

NEWEST_URL = f"{BASE_URL}/videos/"
CATEGORIES_URL = f"{BASE_URL}/categories/"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DATABASE_FILE = "fulleroticmovies_bot.db"

CHECK_INTERVAL_SECONDS = 600
REQUEST_DELAY_SECONDS = 2.0

MAX_ARCHIVE_PAGES_PER_CATEGORY = 10

WANTED_CATEGORY_SLUGS = {
    "classic",
    "70s",
    "80s",
    "90s",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 "
        "PersonalContentMonitor/1.0"
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

@dataclass(frozen=True)
class Category:
    name: str
    slug: str
    url: str


@dataclass
class Article:
    title: str
    url: str
    image_url: str = ""
    source_name: str = ""
    source_url: str = ""


# ============================================================
# VERİTABANI
# ============================================================

def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")

    return connection


def init_database() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                image_url TEXT,
                sent INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS article_sources (
                article_url TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(article_url, source_url)
            );

            CREATE TABLE IF NOT EXISTS categories (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_articles_sent
            ON articles(sent);

            CREATE INDEX IF NOT EXISTS idx_article_sources_url
            ON article_sources(article_url);
            """
        )
        connection.commit()


def normalize_url(url: str) -> str:
    parsed = urlparse(url)

    scheme = parsed.scheme.lower() or "https"
    hostname = parsed.hostname.lower() if parsed.hostname else ""

    path = re.sub(r"/+", "/", parsed.path)

    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") + "/"

    return f"{scheme}://{hostname}{path}"


def article_exists(url: str) -> bool:
    url = normalize_url(url)

    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM articles WHERE url = ?",
            (url,),
        ).fetchone()

    return row is not None


def save_article(
    article: Article,
    *,
    sent: bool,
) -> bool:
    normalized_url = normalize_url(article.url)

    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO articles (
                title,
                url,
                image_url,
                sent
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                article.title,
                normalized_url,
                article.image_url,
                int(sent),
            ),
        )

        is_new = cursor.rowcount > 0

        if not is_new:
            connection.execute(
                """
                UPDATE articles
                SET
                    title = ?,
                    image_url = CASE
                        WHEN ? != '' THEN ?
                        ELSE image_url
                    END,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE url = ?
                """,
                (
                    article.title,
                    article.image_url,
                    article.image_url,
                    normalized_url,
                ),
            )

        connection.execute(
            """
            INSERT OR IGNORE INTO article_sources (
                article_url,
                source_name,
                source_url
            )
            VALUES (?, ?, ?)
            """,
            (
                normalized_url,
                article.source_name,
                article.source_url,
            ),
        )

        connection.commit()

    return is_new


def mark_as_sent(url: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE articles
            SET sent = 1
            WHERE url = ?
            """,
            (normalize_url(url),),
        )
        connection.commit()


def save_categories(categories: Iterable[Category]) -> None:
    with get_connection() as connection:
        for category in categories:
            connection.execute(
                """
                INSERT INTO categories (
                    slug,
                    name,
                    url
                )
                VALUES (?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    url = excluded.url,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (
                    category.slug,
                    category.name,
                    category.url,
                ),
            )

        connection.commit()


# ============================================================
# HTTP
# ============================================================

def get_soup(url: str) -> BeautifulSoup:
    logging.info("Sayfa alınıyor: %s", url)

    response = SESSION.get(
        url,
        timeout=30,
    )
    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type",
        "",
    ).lower()

    if "html" not in content_type:
        raise RuntimeError(
            f"HTML olmayan cevap alındı: {content_type}"
        )

    return BeautifulSoup(
        response.text,
        "html.parser",
    )


# ============================================================
# KATEGORİ KEŞFİ
# ============================================================

CATEGORY_URL_PATTERN = re.compile(
    r"^https://www\.fulleroticmovies\.net/category/([^/]+)/?$",
    re.IGNORECASE,
)


def discover_categories() -> list[Category]:
    soup = get_soup(CATEGORIES_URL)

    categories: dict[str, Category] = {}

    for anchor in soup.select("a[href]"):
        href = urljoin(
            CATEGORIES_URL,
            anchor.get("href", ""),
        )

        href = normalize_url(href)

        match = CATEGORY_URL_PATTERN.match(href)

        if not match:
            continue

        slug = match.group(1).lower()

        raw_name = anchor.get_text(
            " ",
            strip=True,
        )

        name = re.sub(
            r"\s+\d[\d,.Kk]*\s*$",
            "",
            raw_name,
        ).strip()

        if not name:
            name = slug.replace("-", " ").title()

        categories[slug] = Category(
            name=name,
            slug=slug,
            url=f"{BASE_URL}/category/{slug}/",
        )

    result = sorted(
        categories.values(),
        key=lambda category: category.name.lower(),
    )

    save_categories(result)

    logging.info(
        "%s kategori keşfedildi.",
        len(result),
    )

    return result


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

        if current.name in {
            "article",
            "li",
            "figure",
        }:
            return current

        classes = " ".join(
            current.get("class", [])
        ).lower()

        if any(
            word in classes
            for word in (
                "video",
                "movie",
                "item",
                "card",
                "thumb",
                "post",
            )
        ):
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
        first_srcset_url = srcset.split(",")[0].strip().split(" ")[0]
        candidates.insert(0, first_srcset_url)

    for candidate in candidates:
        if not candidate:
            continue

        if candidate.startswith("data:"):
            continue

        return urljoin(page_url, candidate)

    return ""


def scrape_listing(
    page_url: str,
    source_name: str,
) -> list[Article]:
    soup = get_soup(page_url)

    articles: dict[str, Article] = {}

    for anchor in soup.select("a[href]"):
        href = urljoin(
            page_url,
            anchor.get("href", ""),
        )
        href = normalize_url(href)

        if not VIDEO_URL_PATTERN.match(href):
            continue

        title = anchor.get_text(
            " ",
            strip=True,
        )

        container = find_container(anchor)

        if not title:
            heading = container.select_one(
                "h1, h2, h3, h4, .title"
            )

            if heading:
                title = heading.get_text(
                    " ",
                    strip=True,
                )

        if not title:
            image = container.select_one("img")

            if image:
                title = (
                    image.get("alt")
                    or image.get("title")
                    or ""
                ).strip()

        if not title:
            continue

        image_url = extract_image(
            container,
            page_url,
        )

        articles[href] = Article(
            title=title,
            url=href,
            image_url=image_url,
            source_name=source_name,
            source_url=page_url,
        )

    return list(articles.values())


# ============================================================
# SAYFALAMA
# ============================================================

def paginated_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url.rstrip("/") + "/"

    return f"{base_url.rstrip('/')}/{page}/"


# ============================================================
# TELEGRAM
# ============================================================

def ensure_telegram_configured() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN ortam değişkeni eksik."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID ortam değişkeni eksik."
        )


def build_telegram_message(article: Article) -> str:
    return f"/qbleech {article.url}"


def telegram_request(
    method: str,
    payload: dict,
) -> dict:
    ensure_telegram_configured()

    endpoint = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    response = SESSION.post(
        endpoint,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram hatası: {result}"
        )

    return result


def send_text_message(article: Article) -> None:
    telegram_request(
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": build_telegram_message(article),
        },
    )


def send_photo_message(article: Article) -> None:
    telegram_request(
        "sendPhoto",
        {
            "chat_id": CHAT_ID,
            "photo": article.image_url,
            "caption": build_telegram_message(article),
            "parse_mode": "HTML",
        },
    )


def send_to_telegram(article: Article) -> None:
    send_text_message(article)


# ============================================================
# İLK KURULUM
# ============================================================

def initialize_newest() -> None:
    articles = scrape_listing(
        NEWEST_URL,
        "Newest",
    )

    for article in articles:
        save_article(
            article,
            sent=True,
        )

    logging.info(
        "Başlangıç kaydı tamamlandı: %s içerik.",
        len(articles),
    )


def database_is_empty() -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM articles"
        ).fetchone()

    return row["total"] == 0


# ============================================================
# YENİ İÇERİK TAKİBİ
# ============================================================

def check_newest() -> None:
    articles = scrape_listing(
        NEWEST_URL,
        "Newest",
    )

    for article in reversed(articles):
        if article_exists(article.url):
            save_article(
                article,
                sent=False,
            )
            continue

        inserted = save_article(
            article,
            sent=False,
        )

        if not inserted:
            continue

        try:
            send_to_telegram(article)
            mark_as_sent(article.url)

            logging.info(
                "Telegram'a gönderildi: %s",
                article.title,
            )

        except Exception:
            logging.exception(
                "Telegram gönderim hatası: %s",
                article.title,
            )

        time.sleep(2)


def run_watch_mode() -> None:
    init_database()

    if database_is_empty():
        initialize_newest()

    while True:
        try:
            check_newest()
        except Exception:
            logging.exception(
                "Newest kontrolü başarısız oldu."
            )

        logging.info(
            "%s saniye bekleniyor.",
            CHECK_INTERVAL_SECONDS,
        )
        time.sleep(CHECK_INTERVAL_SECONDS)


def check_once() -> None:
    """
    Tek seferlik kontrol. GitHub Actions gibi ortamlar için.
    Veritabanı boşsa önce init yapar, sonra yeni içerik kontrolü yapar.
    """
    init_database()

    if database_is_empty():
        initialize_newest()
        logging.info("İlk kayıt yapıldı. Sonraki çalıştırmada yeni içerikler gönderilecek.")
        return

    check_newest()
    logging.info("Tek seferlik kontrol tamamlandı.")


# ============================================================
# ARŞİVLEME
# ============================================================

def archive_source(
    source_name: str,
    source_url: str,
    max_pages: int,
) -> None:
    previous_page_urls: set[str] = set()

    for page_number in range(1, max_pages + 1):
        page_url = paginated_url(
            source_url,
            page_number,
        )

        try:
            articles = scrape_listing(
                page_url,
                source_name,
            )

            if not articles:
                logging.info(
                    "%s sayfa %s: içerik bulunamadı.",
                    source_name,
                    page_number,
                )
                break

            current_urls = {
                normalize_url(article.url)
                for article in articles
            }

            signature = "|".join(
                sorted(current_urls)
            )

            if signature in previous_page_urls:
                logging.warning(
                    "Tekrarlanan sayfa tespit edildi, durduruldu."
                )
                break

            previous_page_urls.add(signature)

            new_count = 0

            for article in articles:
                inserted = save_article(
                    article,
                    sent=True,
                )

                if inserted:
                    new_count += 1

            logging.info(
                "%s | sayfa %s | bulunan: %s | yeni: %s",
                source_name,
                page_number,
                len(articles),
                new_count,
            )

        except requests.HTTPError as error:
            status = error.response.status_code if error.response else "?"

            logging.error(
                "%s sayfa %s HTTP hatası: %s",
                source_name,
                page_number,
                status,
            )
            break

        except Exception:
            logging.exception(
                "%s sayfa %s arşivlenemedi.",
                source_name,
                page_number,
            )
            break

        time.sleep(REQUEST_DELAY_SECONDS)


def archive_newest(max_pages: int) -> None:
    init_database()

    archive_source(
        source_name="Newest Archive",
        source_url=NEWEST_URL,
        max_pages=max_pages,
    )


def archive_selected_categories(
    max_pages: int,
) -> None:
    init_database()

    categories = discover_categories()

    selected = [
        category
        for category in categories
        if category.slug in WANTED_CATEGORY_SLUGS
    ]

    if not selected:
        logging.warning(
            "WANTED_CATEGORY_SLUGS içindeki kategoriler bulunamadı."
        )
        return

    for category in selected:
        archive_source(
            source_name=category.name,
            source_url=category.url,
            max_pages=max_pages,
        )

        time.sleep(REQUEST_DELAY_SECONDS)


def archive_all_categories(
    max_pages: int,
) -> None:
    init_database()

    categories = discover_categories()

    if not categories:
        logging.warning("Kategori bulunamadı.")
        return

    logging.info(
        "%s kategori arşivlenmeye başlanıyor (her biri %s sayfa).",
        len(categories),
        max_pages,
    )

    for index, category in enumerate(categories, start=1):
        logging.info(
            "[%s/%s] Arşivleniyor: %s",
            index,
            len(categories),
            category.name,
        )

        archive_source(
            source_name=category.name,
            source_url=category.url,
            max_pages=max_pages,
        )

        time.sleep(REQUEST_DELAY_SECONDS)

    logging.info("Tüm kategorilerin arşivlenmesi tamamlandı.")


def list_categories() -> None:
    categories = discover_categories()

    print(
        f"\nToplam kategori: {len(categories)}\n"
    )

    for category in categories:
        print(
            f"{category.slug:<40} {category.name}"
        )


# ============================================================
# ARŞİVİ TELEGRAM'A GÖNDERME
# ============================================================

def reset_sent() -> None:
    """
    Tüm içerikleri sent=0 yapar, böylece hepsi yeniden gönderilir.
    """
    with get_connection() as connection:
        count = connection.execute(
            "UPDATE articles SET sent = 0"
        ).rowcount
        connection.commit()

    logging.info("%s içerik gönderilmek üzere sıfırlandı.", count)


def send_archived(limit: int, delay: float) -> None:
    """
    Veritabanındaki gönderilmemiş içerikleri id sırasına göre
    (en yeni arşivlenen son) Telegram'a gönderir.
    """
    ensure_telegram_configured()

    with get_connection() as connection:
        total_unsent = connection.execute(
            "SELECT COUNT(*) AS total FROM articles WHERE sent = 0"
        ).fetchone()["total"]

        rows = connection.execute(
            """
            SELECT
                a.title,
                a.url,
                a.image_url,
                COALESCE(
                    (SELECT s.source_name
                     FROM article_sources s
                     WHERE s.article_url = a.url
                     ORDER BY s.discovered_at ASC
                     LIMIT 1),
                    'Arşiv'
                ) AS source_name
            FROM articles a
            WHERE a.sent = 0
            ORDER BY a.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        logging.info("Gönderilecek bekleyen içerik yok.")
        return

    logging.info(
        "Toplam bekleyen: %s | Bu turda gönderilecek: %s | gecikme: %ss",
        total_unsent,
        len(rows),
        delay,
    )

    sent_count = 0

    for row in rows:
        article = Article(
            title=row["title"],
            url=row["url"],
            image_url=row["image_url"] or "",
            source_name=row["source_name"],
            source_url=NEWEST_URL,
        )

        try:
            send_to_telegram(article)
            mark_as_sent(article.url)
            sent_count += 1

            logging.info(
                "[%s/%s] Gönderildi: %s",
                sent_count,
                len(rows),
                article.title,
            )

        except Exception as error:
            logging.error(
                "Gönderilemedi: %s | %s",
                article.title,
                error,
            )
            break

        time.sleep(delay)

    logging.info(
        "Bu tur tamamlandı. Gönderilen: %s | Kalan: %s",
        sent_count,
        total_unsent - sent_count,
    )


# ============================================================
# GÖNDERİLEMEYENLERİ TEKRAR DENEME
# ============================================================

def retry_unsent(limit: int = 20) -> None:
    ensure_telegram_configured()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                title,
                url,
                image_url
            FROM articles
            WHERE sent = 0
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    for row in rows:
        article = Article(
            title=row["title"],
            url=row["url"],
            image_url=row["image_url"] or "",
            source_name="Tekrar deneme",
            source_url=NEWEST_URL,
        )

        try:
            send_to_telegram(article)
            mark_as_sent(article.url)

            logging.info(
                "Bekleyen içerik gönderildi: %s",
                article.title,
            )

        except Exception:
            logging.exception(
                "Bekleyen içerik yine gönderilemedi."
            )
            break

        time.sleep(2)


# ============================================================
# KOMUT SATIRI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Web sitesi metadata takip ve Telegram bildirim botu"
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    subparsers.add_parser(
        "watch",
        help="Yeni içerikleri sürekli takip et.",
    )

    subparsers.add_parser(
        "check-once",
        help="Tek seferlik kontrol (GitHub Actions için).",
    )

    subparsers.add_parser(
        "init",
        help="Mevcut ilk sayfayı sessizce kaydet.",
    )

    subparsers.add_parser(
        "categories",
        help="Sitedeki kategorileri keşfet ve listele.",
    )

    archive_newest_parser = subparsers.add_parser(
        "archive-newest",
        help="Newest sayfalarını Telegram'a göndermeden arşivle.",
    )
    archive_newest_parser.add_argument(
        "--pages",
        type=int,
        default=10,
    )

    archive_categories_parser = subparsers.add_parser(
        "archive-categories",
        help="Seçilen kategorileri sessizce arşivle.",
    )
    archive_categories_parser.add_argument(
        "--pages",
        type=int,
        default=MAX_ARCHIVE_PAGES_PER_CATEGORY,
    )

    archive_all_parser = subparsers.add_parser(
        "archive-all",
        help="Tüm kategorileri sessizce arşivle.",
    )
    archive_all_parser.add_argument(
        "--pages",
        type=int,
        default=MAX_ARCHIVE_PAGES_PER_CATEGORY,
    )

    retry_parser = subparsers.add_parser(
        "retry",
        help="Gönderilemeyen Telegram kayıtlarını yeniden dene.",
    )
    retry_parser.add_argument(
        "--limit",
        type=int,
        default=20,
    )

    subparsers.add_parser(
        "reset-sent",
        help="Tüm içerikleri gönderilmemiş olarak işaretle.",
    )

    send_archived_parser = subparsers.add_parser(
        "send-archived",
        help="Arşivdeki gönderilmemiş içerikleri Telegram'a gönder.",
    )
    send_archived_parser.add_argument(
        "--limit",
        type=int,
        default=100,
    )
    send_archived_parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
    )

    args = parser.parse_args()

    init_database()

    if args.command == "watch":
        run_watch_mode()

    elif args.command == "check-once":
        check_once()

    elif args.command == "init":
        initialize_newest()

    elif args.command == "categories":
        list_categories()

    elif args.command == "archive-newest":
        archive_newest(args.pages)

    elif args.command == "archive-categories":
        archive_selected_categories(args.pages)

    elif args.command == "archive-all":
        archive_all_categories(args.pages)

    elif args.command == "retry":
        retry_unsent(args.limit)

    elif args.command == "reset-sent":
        reset_sent()

    elif args.command == "send-archived":
        send_archived(args.limit, args.delay)


if __name__ == "__main__":
    main()
