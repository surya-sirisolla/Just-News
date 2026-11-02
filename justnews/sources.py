"""Catalogue of free news sources and baseline source credibility.

Edit this file to add or remove sources; no code changes are needed elsewhere.

Source kinds:
  rss          - a direct RSS/Atom feed URL
  google_news  - a Google News RSS feed (topic or search); the publisher comes from <source>
  site         - a news website homepage; its RSS feed is found via <link rel="alternate">
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

GN_PARAMS = "hl=en-US&gl=US&ceid=US:en"


def google_topic(topic: str) -> str:
    return f"https://news.google.com/rss/headlines/section/topic/{topic}?{GN_PARAMS}"


def google_search(query: str, window: str = "1d") -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{window}')}&{GN_PARAMS}"


@dataclass(frozen=True)
class Source:
    publisher: str  # shown as the article's source (Google News items use their own publisher)
    url: str
    category: str
    kind: str = "rss"

    @property
    def label(self) -> str:
        return f"{self.publisher} [{self.category}]"


SOURCES: list[Source] = [
    # Technology
    Source("TechCrunch", "https://techcrunch.com/feed/", "Technology"),
    Source("The Verge", "https://www.theverge.com/rss/index.xml", "Technology"),
    Source("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "Technology"),
    Source("Wired", "https://www.wired.com/feed/rss", "Technology"),
    Source("Engadget", "https://www.engadget.com", "Technology", kind="site"),
    Source("Google News", google_topic("TECHNOLOGY"), "Technology", kind="google_news"),
    # AI
    Source("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", "AI"),
    Source("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "AI"),
    Source("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "AI"),
    Source("VentureBeat", "https://venturebeat.com/category/ai/feed/", "AI"),
    Source(
        "Google News",
        google_search('"artificial intelligence" OR OpenAI OR Anthropic OR "Google DeepMind" OR "AI model"'),
        "AI",
        kind="google_news",
    ),
    # Business
    Source("CNBC", "https://www.cnbc.com/id/10001147/device/rss/rss.html", "Business"),
    Source("BBC News", "https://feeds.bbci.co.uk/news/business/rss.xml", "Business"),
    Source("The Guardian", "https://www.theguardian.com/uk/business/rss", "Business"),
    Source("Google News", google_topic("BUSINESS"), "Business", kind="google_news"),
    # Politics
    Source("NPR", "https://feeds.npr.org/1014/rss.xml", "Politics"),
    Source("Politico", "https://rss.politico.com/politics-news.xml", "Politics"),
    Source("The Hill", "https://thehill.com/news/feed/", "Politics"),
    Source("Google News", google_search("politics OR election OR congress OR parliament"), "Politics", kind="google_news"),
    # World
    Source("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml", "World"),
    Source("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "World"),
    Source("The Guardian", "https://www.theguardian.com/world/rss", "World"),
    Source("NPR", "https://feeds.npr.org/1004/rss.xml", "World"),
    Source("Google News", google_topic("WORLD"), "World", kind="google_news"),
]

# Baseline credibility (0-100) by publisher. Keys are lowercase substrings matched
# against the article's publisher name. Unknown publishers are rated by the evaluator LLM.
SOURCE_CREDIBILITY: dict[str, int] = {
    "reuters": 96, "associated press": 96, "ap news": 96, "bbc": 94, "npr": 92,
    "financial times": 94, "the wall street journal": 92, "wsj": 92, "bloomberg": 93,
    "the economist": 93, "the new york times": 92, "the washington post": 90,
    "the guardian": 89, "al jazeera": 85, "cnbc": 87, "politico": 87, "axios": 87,
    "the hill": 80, "cnn": 84, "abc news": 86, "cbs news": 86, "nbc news": 86,
    "pbs": 91, "deutsche welle": 89, "dw": 89, "france 24": 87, "nikkei": 90,
    "mit technology review": 91, "ars technica": 89, "wired": 87, "the verge": 86,
    "techcrunch": 86, "engadget": 82, "venturebeat": 80, "zdnet": 80, "cnet": 80,
    "the information": 89, "forbes": 78, "business insider": 78, "fortune": 84,
    "fox news": 75, "new york post": 68, "daily mail": 60, "yahoo": 75, "msn": 72,
}

# Headline patterns that are obviously not news worth posting (deals, shopping
# guides, quizzes, horoscopes, sponsored content, live-blog boilerplate).
IRRELEVANT_PATTERNS: list[str] = [
    r"\b(deal|deals|discount|coupon|promo code|% off|sale)\b",
    r"\bbest\b.*\b(to buy|for 20\d\d|of 20\d\d|right now|deals?)\b",
    r"\b(gift guide|buying guide|shopping)\b",
    r"\b(horoscope|crossword|wordle|quiz|sudoku|puzzle)\b",
    r"\b(sponsored|advertorial|partner content)\b",
    r"\bhow to (watch|stream)\b",
    r"\b(podcast|newsletter|webinar)\b",
    r"\b(obituary|in pictures|photos of the week|week in pictures)\b",
]

# Keywords that an article in the AI category must mention, so that general
# feeds and broad searches do not leak unrelated stories into it.
AI_KEYWORDS: list[str] = [
    "ai", "a.i.", "artificial intelligence", "machine learning", "llm", "chatbot", "chatgpt",
    "openai", "anthropic", "claude", "gemini", "deepmind", "copilot", "neural", "generative",
    "model", "agent", "agentic", "nvidia", "gpu", "robot", "automation", "deep learning",
]
