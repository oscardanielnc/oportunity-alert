"""
Reddit monitor via API JSON pública — sin credenciales ni PRAW.
Lee subreddits públicos con GET requests simples.
"""
import hashlib
import logging
import time
import requests
from datetime import datetime, timezone
from filters.keyword_filter import ticker_in_text

logger = logging.getLogger(__name__)

SUBREDDITS = ["wallstreetbets", "stocks", "investing", "SecurityAnalysis"]
USER_AGENT = "OportunityAlert/1.0 by oscar_pairus"

SQUEEZE_KEYWORDS = [
    "short squeeze", "gamma squeeze", "unusual options", "short interest",
    "days to cover", "float rotation", "squeeze play", "high short interest",
]


def fetch_reddit_mentions(watchlist: list[str], posts_per_sub: int = 25) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    articles = []

    for sub in SUBREDDITS:
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/new.json",
                params={"limit": posts_per_sub},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            if resp.status_code == 429:
                logger.warning(f"Reddit rate limit en r/{sub} — esperando siguiente ciclo")
                continue
            resp.raise_for_status()

            posts = resp.json().get("data", {}).get("children", [])

            for post in posts:
                try:
                    data = post.get("data", {})
                    title = data.get("title", "")
                    body = data.get("selftext", "")
                    full_text = f"{title} {body}".upper()

                    tickers_found = [t for t in watchlist if ticker_in_text(t, full_text)]
                    if not tickers_found:
                        continue

                    created_utc = data.get("created_utc", 0)
                    pub_dt = datetime.fromtimestamp(created_utc, tz=timezone.utc)
                    age_minutes = (now_utc - pub_dt).total_seconds() / 60

                    squeeze_alert = any(kw.lower() in full_text.lower() for kw in SQUEEZE_KEYWORDS)
                    post_id = hashlib.md5(data.get("id", title).encode()).hexdigest()

                    articles.append({
                        "id": post_id,
                        "source": f"REDDIT_{sub.upper()}",
                        "title": title,
                        "summary": body[:400] if body else "",
                        "url": f"https://reddit.com{data.get('permalink', '')}",
                        "published_at": pub_dt.isoformat(),
                        "age_minutes": round(age_minutes, 1),
                        "tickers_found": tickers_found,
                        "raw_text": full_text,
                        "squeeze_alert": squeeze_alert,
                        "score": data.get("score", 0),
                    })
                except Exception as e:
                    logger.debug(f"Error procesando post r/{sub}: {e}")
                    continue

            logger.debug(f"Reddit r/{sub}: {len(posts)} posts leídos")

        except Exception as e:
            logger.warning(f"Error accediendo r/{sub}: {e}")
        finally:
            time.sleep(1.5)  # evitar rate limit de Reddit en requests consecutivos

    if articles:
        logger.info(f"Reddit: {len(articles)} posts relevantes en {len(SUBREDDITS)} subreddits")

    return articles
