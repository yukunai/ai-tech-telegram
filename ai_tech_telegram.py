#!/usr/bin/env python3
"""AI/technology news collector and Telegram publisher.

The process is intentionally small and dependency-free: it uses public RSS
feeds, GitHub's public REST API, DeepSeek's OpenAI-compatible API, SQLite, and
the Telegram Bot API.  It is run by two systemd timers (scan and publish).
"""

from __future__ import annotations

import datetime as dt
import email.utils
import fcntl
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


STATE_DIR = Path(os.environ.get("AI_TECH_STATE_DIR", "/var/lib/ai-news"))
DB_PATH = STATE_DIR / "news.sqlite3"
LOCK_PATH = STATE_DIR / "job.lock"
LOG_PATH = STATE_DIR / "bot.log"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
MIN_SCORE = int(os.environ.get("MIN_SCORE", "70"))
GITHUB_COUNT = int(os.environ.get("GITHUB_COUNT", "3"))
WEB_COUNT = int(os.environ.get("WEB_COUNT", "5"))
GITHUB_SCORE_COUNT = int(os.environ.get("GITHUB_SCORE_COUNT", "6"))
WEB_SCORE_COUNT = int(os.environ.get("WEB_SCORE_COUNT", "5"))
SCORE_BATCH_SIZE = GITHUB_SCORE_COUNT + WEB_SCORE_COUNT
USER_AGENT = "ai-tech-telegram/1.0 (+https://github.com/openclaw/openclaw)"

KEYWORDS = (
    "ai", "artificial intelligence", "machine learning", "deep learning", "llm",
    "agent", "robot", "robotics", "gpu", "nvidia", "openai", "anthropic",
    "deepseek", "transformer", "inference", "semiconductor", "chip", "data center",
    "developer", "open source", "python", "rust", "github", "cybersecurity",
    "quantum", "人工智能", "大模型", "智能体", "机器人", "科技", "开源", "编程",
    "数据库", "云计算", "芯片", "安全", "模型", "推理", "数据中心",
)

RSS_SOURCES = (
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("NVIDIA Developer", "https://developer.nvidia.com/blog/feed/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("Hacker News", "https://hnrss.org/frontpage"),
    ("arXiv AI", "https://export.arxiv.org/rss/cs.AI"),
    ("arXiv Machine Learning", "https://export.arxiv.org/rss/cs.LG"),
    ("AWS Machine Learning", "https://aws.amazon.com/blogs/machine-learning/feed/"),
    ("Apple Machine Learning", "https://machinelearning.apple.com/rss.xml"),
    ("IEEE Spectrum AI", "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    ("TensorFlow Blog", "https://blog.tensorflow.org/feeds/posts/default"),
    ("InfoQ", "https://www.infoq.com/feed/"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/"),
    ("Meta Engineering", "https://engineering.fb.com/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("The Register AI", "https://www.theregister.com/software/ai_ml/headlines.atom"),
    ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("TechXplore AI", "https://techxplore.com/rss-feed/machine-learning-ai-news/"),
    ("MarkTechPost", "https://www.marktechpost.com/feed/"),
)

# Research feeds are useful context, but the hourly digest should be driven by
# current product, company, and industry news whenever those candidates exist.
PAPER_SOURCES = ("arXiv AI", "arXiv Machine Learning")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any, limit: int = 1200) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fit_summary(value: Any, fallback: str) -> str:
    """Keep the published Chinese core summary between 100 and 130 chars."""
    text = clean_text(value, 500)
    if not text:
        text = f"该条内容主要介绍：{clean_text(fallback, 300)}"
    # Do not append an English fallback to an otherwise Chinese summary.
    # Keep sentence fragments that contain Chinese characters and discard a
    # trailing all-English explanation sometimes emitted by the model.
    pieces = re.split(r"(?<=[。！？])", text)
    kept = [piece for piece in pieces if len(re.findall(r"[\u4e00-\u9fff]", piece)) >= 8 or len(pieces) == 1]
    text = "".join(kept).strip() or text
    padding = "该进展的实际价值仍取决于后续落地、用户反馈与持续迭代，值得继续关注其对行业和开发者的影响。"
    while len(text) < 100:
        text = f"{text}{padding}"
    if len(text) > 130:
        boundary = max(text.rfind(mark, 100, 131) for mark in "。！？；")
        text = text[: boundary + 1] if boundary >= 100 else text[:130]
    return text


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def child_link(element: ET.Element) -> str:
    for child in list(element):
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if href:
            return href
        if child.text:
            return child.text.strip()
    return ""


def parse_date(value: str) -> str:
    if not value:
        return now_iso()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError):
        return now_iso()


def http_bytes(url: str, headers: dict[str, str] | None = None, timeout: int = 25) -> bytes:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError(f"response too large: {url}")
    return data


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
    data = http_bytes(url, headers=headers, timeout=timeout)
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {url}")
    return value


def db_connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL CHECK(kind IN ('github','web')),
          source TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          published_at TEXT NOT NULL,
          discovered_at TEXT NOT NULL,
          score INTEGER,
          score_reason TEXT NOT NULL DEFAULT '',
          summary_zh TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'new',
          sent_at TEXT,
          raw_json TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_items_queue ON items(kind, status, score, published_at);
        CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered_at);
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          details TEXT NOT NULL DEFAULT ''
        );
        """
    )
    db.commit()
    return db


def setting_get(db: sqlite3.Connection, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else ""


def setting_put(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def insert_item(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    url = str(item.get("url", "")).strip()
    title = clean_text(item.get("title", ""), 500)
    if not url or not title:
        return False
    try:
        cursor = db.execute(
            """INSERT OR IGNORE INTO items
            (url,kind,source,title,description,published_at,discovered_at,raw_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                url,
                item["kind"],
                clean_text(item.get("source", ""), 120),
                title,
                clean_text(item.get("description", ""), 1600),
                parse_date(str(item.get("published_at", ""))),
                now_iso(),
                json.dumps(item.get("raw", {}), ensure_ascii=False)[:8000],
            ),
        )
        return cursor.rowcount == 1
    except sqlite3.Error:
        logging.exception("failed to insert item %s", url)
        return False


def fetch_rss(source: str, url: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(http_bytes(url, {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}))
    except Exception as exc:
        logging.warning("RSS failed source=%s error=%s", source, exc)
        return []
    entries: list[dict[str, Any]] = []
    for element in root.iter():
        tag = local_name(element.tag)
        if tag not in {"item", "entry"}:
            continue
        title = child_text(element, ("title",))
        url_value = child_link(element)
        description = child_text(element, ("description", "summary", "content", "subtitle"))
        published = child_text(element, ("pubdate", "published", "updated", "date", "issued"))
        if title and url_value:
            entries.append({
                "kind": "web",
                "source": source,
                "title": title,
                "url": url_value,
                "description": description,
                "published_at": parse_date(published),
                "raw": {"feed": url},
            })
    return entries[:80]


def relevant(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    return any(keyword in text for keyword in KEYWORDS)


def fetch_github() -> list[dict[str, Any]]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    # Three complementary queries per 15-minute scan stay within the
    # unauthenticated search limit and keep fresh repositories flowing into the
    # scoring queue instead of stalling at only one or two eligible projects.
    queries = (
        f"topic:artificial-intelligence pushed:>={since} archived:false",
        f"topic:machine-learning pushed:>={since} archived:false",
        f"topic:llm pushed:>={since} archived:false",
    )
    repos: dict[str, dict[str, Any]] = {}
    for query in queries:
        params = urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": "50"})
        url = f"https://api.github.com/search/repositories?{params}"
        try:
            payload = http_json(url, {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        except Exception as exc:
            logging.warning("GitHub search failed query=%s error=%s", query, exc)
            continue
        for repo in payload.get("items", []):
            if isinstance(repo, dict) and repo.get("html_url"):
                repos[repo["html_url"]] = repo
    result: list[dict[str, Any]] = []
    for repo in repos.values():
        if not isinstance(repo, dict) or not repo.get("html_url"):
            continue
        description = clean_text(repo.get("description", ""), 1200)
        result.append({
            "kind": "github",
            "source": "GitHub",
            "title": repo.get("full_name") or repo.get("name"),
            "url": repo.get("html_url"),
            "description": description,
            "published_at": repo.get("pushed_at") or repo.get("updated_at") or now_iso(),
            "raw": {
                "stars": repo.get("stargazers_count", 0),
                "forks": repo.get("forks_count", 0),
                "language": repo.get("language"),
                "topics": repo.get("topics", []),
                "updated_at": repo.get("updated_at"),
            },
        })
    return result


def ingest(db: sqlite3.Connection) -> tuple[int, int]:
    github_items = fetch_github()
    web_items: list[dict[str, Any]] = []
    for source, url in RSS_SOURCES:
        web_items.extend(fetch_rss(source, url))
    # Keep the queue focused on AI/technology and avoid treating GitHub links
    # surfaced by a general feed as GitHub quota items.
    web_items = [item for item in web_items if relevant(item["title"], item["description"]) and "github.com/" not in item["url"]]
    added_github = sum(1 for item in github_items if insert_item(db, item))
    added_web = sum(1 for item in web_items if insert_item(db, item))
    db.commit()
    logging.info("ingest github_seen=%d web_seen=%d new_github=%d new_web=%d", len(github_items), len(web_items), added_github, added_web)
    return added_github, added_web


def deepseek_json(prompt: str) -> dict[str, Any]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是严格的AI与科技新闻编辑。只根据输入事实评分和摘要，不得编造。必须返回JSON对象。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        # deepseek-v4-flash spends part of the completion budget on hidden
        # reasoning; 2200 can end with an empty visible content field.
        "max_tokens": 6000,
        # Scoring is a structured extraction task; disable hidden reasoning so
        # the JSON summary is returned within the completion budget.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("DeepSeek returned no content")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        content = content[start : end + 1]
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("DeepSeek JSON is not an object")
    return value


def score_new(db: sqlite3.Connection) -> int:
    github_quota = min(GITHUB_SCORE_COUNT, SCORE_BATCH_SIZE)
    web_quota = min(WEB_SCORE_COUNT, max(0, SCORE_BATCH_SIZE - github_quota))
    rows = list(db.execute(
        "SELECT * FROM items WHERE status='new' AND kind='github' ORDER BY published_at DESC, discovered_at ASC LIMIT ?",
        (github_quota,),
    ).fetchall())
    paper_placeholders = ",".join("?" for _ in PAPER_SOURCES)
    rows.extend(db.execute(
        "SELECT * FROM items WHERE status='new' AND kind='web' AND source NOT IN (" + paper_placeholders + ") "
        "ORDER BY published_at DESC, discovered_at ASC LIMIT ?",
        (*PAPER_SOURCES, web_quota),
    ).fetchall())
    # If one source is temporarily empty, fill the batch from the other source.
    if len(rows) < SCORE_BATCH_SIZE:
        selected_ids = [row["id"] for row in rows]
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            extra_sql = (
                "SELECT * FROM items WHERE status='new' AND id NOT IN (" + placeholders + ") "
                "ORDER BY published_at DESC, discovered_at ASC LIMIT ?"
            )
            rows.extend(db.execute(extra_sql, (*selected_ids, SCORE_BATCH_SIZE - len(rows))).fetchall())
        else:
            rows.extend(db.execute(
                "SELECT * FROM items WHERE status='new' ORDER BY published_at DESC, discovered_at ASC LIMIT ?",
                (SCORE_BATCH_SIZE,),
            ).fetchall())
    if not rows:
        return 0
    payload: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        raw = {}
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            pass
        payload.append({
            "index": index,
            "kind": row["kind"],
            "source": row["source"],
            "title": row["title"],
            "url": row["url"],
            "published_at": row["published_at"],
            "description": row["description"][:1000],
            "github_stats": {k: raw.get(k) for k in ("stars", "forks", "language", "topics") if k in raw},
        })
    prompt = (
        "请评估下面候选内容是否值得发给中文AI/科技读者。评分标准：\n"
        "- 新鲜度20分；可信度20分；技术/行业影响25分；信息密度20分；读者实用性15分。\n"
        f"总分0-100，低于{MIN_SCORE}分必须标记为不推荐。GitHub项目要结合stars、技术价值和近期活跃度。\n"
        "为每一项生成100-130个中文字符的客观核心摘要，以及不超过30个汉字的评分理由。摘要必须完整成句，不得少于100字。\n"
        "只返回 {\"items\": [{\"index\": 0, \"score\": 0, \"summary_zh\": \"...\", \"reason\": \"...\"}]}，每个index都要覆盖。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        result = deepseek_json(prompt)
    except Exception as first_exc:
        logging.warning("DeepSeek first scoring attempt failed; retrying once: %s", first_exc)
        time.sleep(2)
        try:
            result = deepseek_json(prompt)
        except Exception as second_exc:
            logging.exception("DeepSeek scoring failed after retry: %s", second_exc)
            return 0
    try:
        scored = result.get("items", [])
        by_index = {int(item["index"]): item for item in scored if isinstance(item, dict) and str(item.get("index", "")).isdigit()}
    except Exception as exc:
        logging.exception("DeepSeek scoring failed: %s", exc)
        return 0
    updated = 0
    for index, row in enumerate(rows):
        item = by_index.get(index, {})
        try:
            score = max(0, min(100, int(item.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        summary = fit_summary(item.get("summary_zh", ""), row["description"])
        reason = clean_text(item.get("reason", ""), 120)
        status = "candidate" if score >= MIN_SCORE else "rejected"
        db.execute("UPDATE items SET score=?, score_reason=?, summary_zh=?, status=? WHERE id=?", (score, reason, summary, status, row["id"]))
        updated += 1
    db.commit()
    eligible = db.execute("SELECT COUNT(*) FROM items WHERE status='candidate' AND score>=?", (MIN_SCORE,)).fetchone()[0]
    rejected = db.execute("SELECT COUNT(*) FROM items WHERE status='rejected'").fetchone()[0]
    logging.info("scored=%d eligible_total=%d rejected_total=%d", updated, eligible, rejected)
    return updated


def discover_chat_id(db: sqlite3.Connection) -> str:
    configured = TELEGRAM_CHAT_ID or setting_get(db, "telegram_chat_id")
    if configured:
        return configured
    if not TELEGRAM_BOT_TOKEN:
        return ""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?limit=100&timeout=0"
    try:
        payload = http_json(url, {"Accept": "application/json"}, timeout=20)
    except Exception as exc:
        logging.warning("Telegram getUpdates failed: %s", exc)
        return ""
    chats: dict[str, dict[str, Any]] = {}
    for update in payload.get("result", []):
        if not isinstance(update, dict):
            continue
        message = update.get("message") or update.get("channel_post") or update.get("edited_message") or update.get("edited_channel_post") or {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = chat
    if len(chats) == 1:
        chat_id = next(iter(chats))
        setting_put(db, "telegram_chat_id", chat_id)
        db.commit()
        logging.info("auto-discovered Telegram chat type=%s", chats[chat_id].get("type"))
        return chat_id
    if chats:
        logging.warning("multiple Telegram chats found; set TELEGRAM_CHAT_ID explicitly")
    else:
        logging.warning("no Telegram chat found; send /start to the bot first")
    return ""


def telegram_send(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {result.get('description', 'unknown error')}")


def distinct_rows(rows: list[sqlite3.Row], wanted: int) -> list[sqlite3.Row]:
    selected: list[sqlite3.Row] = []
    seen_sources: set[str] = set()
    for row in rows:
        if row["source"] not in seen_sources:
            selected.append(row)
            seen_sources.add(row["source"])
            if len(selected) >= wanted:
                return selected
    for row in rows:
        if row not in selected:
            selected.append(row)
            if len(selected) >= wanted:
                break
    return selected


def publish(db: sqlite3.Connection) -> int:
    chat_id = discover_chat_id(db)
    if not chat_id:
        return 0
    github = db.execute(
        "SELECT * FROM items WHERE kind='github' AND status='candidate' AND score>=? ORDER BY score DESC, published_at DESC LIMIT 30",
        (MIN_SCORE,),
    ).fetchall()
    paper_placeholders = ",".join("?" for _ in PAPER_SOURCES)
    web = db.execute(
        "SELECT * FROM items WHERE kind='web' AND status='candidate' AND score>=? AND source NOT IN (" + paper_placeholders + ") "
        "ORDER BY published_at DESC, score DESC LIMIT 60",
        (MIN_SCORE, *PAPER_SOURCES),
    ).fetchall()
    if len(web) < WEB_COUNT:
        paper_rows = db.execute(
            "SELECT * FROM items WHERE kind='web' AND status='candidate' AND score>=? AND source IN (" + paper_placeholders + ") "
            "ORDER BY published_at DESC, score DESC LIMIT 60",
            (MIN_SCORE, *PAPER_SOURCES),
        ).fetchall()
        web = list(web) + list(paper_rows)
    github = distinct_rows(github, GITHUB_COUNT)
    web = distinct_rows(web, WEB_COUNT)
    if len(github) < GITHUB_COUNT or len(web) < WEB_COUNT:
        logging.warning("publish skipped: eligible github=%d/%d web=%d/%d", len(github), GITHUB_COUNT, len(web), WEB_COUNT)
        return 0
    sent = 0
    for position, row in enumerate(github, 1):
        message = (
            f"[GitHub {position}/{GITHUB_COUNT}] {row['title']}\n"
            f"评分：{row['score']}/100\n"
            f"摘要：{row['summary_zh']}\n"
            f"理由：{row['score_reason']}\n"
            f"来源：{row['source']}\n{row['url']}"
        )
        try:
            telegram_send(chat_id, message[:4000])
            db.execute("UPDATE items SET status='sent', sent_at=? WHERE id=?", (now_iso(), row["id"]))
            db.commit()
            sent += 1
            time.sleep(0.4)
        except Exception as exc:
            logging.warning("Telegram GitHub send failed id=%s error=%s", row["id"], exc)
    for position, row in enumerate(web, 1):
        message = (
            f"[网页 {position}/{WEB_COUNT}] {row['title']}\n"
            f"评分：{row['score']}/100\n"
            f"摘要：{row['summary_zh']}\n"
            f"理由：{row['score_reason']}\n"
            f"来源：{row['source']}\n{row['url']}"
        )
        try:
            telegram_send(chat_id, message[:4000])
            db.execute("UPDATE items SET status='sent', sent_at=? WHERE id=?", (now_iso(), row["id"]))
            db.commit()
            sent += 1
            time.sleep(0.4)
        except Exception as exc:
            logging.warning("Telegram web send failed id=%s error=%s", row["id"], exc)
    logging.info("published=%d required=%d", sent, GITHUB_COUNT + WEB_COUNT)
    return sent


def run_job(job: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("another job is running; exiting")
            return 0
        db = db_connect()
        started = now_iso()
        run_id = db.execute("INSERT INTO runs(job,started_at,status) VALUES(?,?,?)", (job, started, "running")).lastrowid
        db.commit()
        try:
            if job == "scan":
                ingest(db)
                score_new(db)
                details = "scan complete"
                code = 0
            elif job == "publish":
                # A publish run refreshes the queue first so an hourly boundary
                # never depends on timer ordering.
                ingest(db)
                score_new(db)
                published = publish(db)
                details = f"published={published}"
                code = 0
            else:
                raise ValueError(f"unknown job: {job}")
            db.execute("UPDATE runs SET finished_at=?, status='ok', details=? WHERE id=?", (now_iso(), details, run_id))
            db.commit()
            return code
        except Exception as exc:
            logging.exception("job failed job=%s error=%s", job, exc)
            db.execute("UPDATE runs SET finished_at=?, status='error', details=? WHERE id=?", (now_iso(), str(exc)[:500], run_id))
            db.commit()
            return 1
        finally:
            db.close()


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
    )
    if len(sys.argv) != 2 or sys.argv[1] not in {"scan", "publish"}:
        print("usage: ai_tech_telegram.py scan|publish", file=sys.stderr)
        return 2
    return run_job(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
