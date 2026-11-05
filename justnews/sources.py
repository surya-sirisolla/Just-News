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
GN_INDIA = "hl=en-IN&gl=IN&ceid=IN:en"  # Google News, India edition

SOUTH_STATES = '"Tamil Nadu" OR Kerala OR Karnataka OR Telangana OR "Andhra Pradesh" OR Puducherry'
SOUTH_CITIES = "Hyderabad OR Chennai OR Bengaluru OR Kochi OR Visakhapatnam OR Coimbatore OR Vijayawada"


def google_topic(topic: str, edition: str = GN_PARAMS) -> str:
    return f"https://news.google.com/rss/headlines/section/topic/{topic}?{edition}"


def google_search(query: str, window: str = "1d", edition: str = GN_PARAMS) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{window}')}&{edition}"


@dataclass(frozen=True)
class Source:
    publisher: str  # shown as the article's source (Google News items use their own publisher)
    url: str
    category: str
    kind: str = "rss"
    region: str = "International"  # default region of its stories; the text can say otherwise (regions.py)

    @property
    def label(self) -> str:
        return f"{self.publisher} [{self.category}]"


# Mostly Indian and South Indian outlets; international news comes from a few world feeds and
# reaches the shortlist only when it is important (see regions.py).
SOURCES: list[Source] = [
    # India
    Source("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", "India", region="India"),
    Source("The Indian Express", "https://indianexpress.com/section/india/feed/", "India", region="India"),
    Source("NDTV", "https://feeds.feedburner.com/ndtvnews-india-news", "India", region="India"),
    Source("The Times of India", "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms", "India", region="India"),
    Source("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "India", region="India"),
    Source("Google News", google_topic("NATION", GN_INDIA), "India", kind="google_news", region="India"),
    # South India
    Source("The Hindu", "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss", "South India", region="South India"),
    Source("The Hindu", "https://www.thehindu.com/news/national/kerala/feeder/default.rss", "South India", region="South India"),
    Source("The Hindu", "https://www.thehindu.com/news/national/karnataka/feeder/default.rss", "South India", region="South India"),
    Source("The Hindu", "https://www.thehindu.com/news/national/andhra-pradesh/feeder/default.rss", "South India", region="South India"),
    Source("The Hindu", "https://www.thehindu.com/news/national/telangana/feeder/default.rss", "South India", region="South India"),
    Source("Deccan Chronicle", "https://www.deccanchronicle.com/google_feeds.xml", "South India", region="South India"),
    Source("Google News", google_search(SOUTH_STATES, edition=GN_INDIA), "South India", kind="google_news", region="South India"),
    Source("Google News", google_search(SOUTH_CITIES, edition=GN_INDIA), "South India", kind="google_news", region="South India"),
    # Business (Indian outlets, plus one international)
    Source("The Hindu", "https://www.thehindu.com/business/feeder/default.rss", "Business", region="India"),
    Source("The Indian Express", "https://indianexpress.com/section/business/feed/", "Business", region="India"),
    Source("Mint", "https://www.livemint.com/rss/news", "India", region="India"),  # Mint's general news feed
    Source("The Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms", "Business", region="India"),
    Source("Google News", google_topic("BUSINESS", GN_INDIA), "Business", kind="google_news", region="India"),
    Source("BBC News", "https://feeds.bbci.co.uk/news/business/rss.xml", "Business"),
    # Technology
    # (Indian outlets' tech and AI sections carry global stories too: the text decides the region.)
    Source("The Indian Express", "https://indianexpress.com/section/technology/feed/", "Technology"),
    Source("The Hindu", "https://www.thehindu.com/sci-tech/technology/feeder/default.rss", "Technology"),
    Source("Mint", "https://www.livemint.com/rss/technology", "Technology"),
    Source("Google News", google_topic("TECHNOLOGY", GN_INDIA), "Technology", kind="google_news"),
    Source("TechCrunch", "https://techcrunch.com/feed/", "Technology"),
    Source("The Verge", "https://www.theverge.com/rss/index.xml", "Technology"),
    # AI
    Source("Google News", google_search('"artificial intelligence" OR AI', edition=GN_INDIA), "AI", kind="google_news"),
    Source("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", "AI"),
    Source("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "AI"),
    Source("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "AI"),
    # World
    Source("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml", "World"),
    Source("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "World"),
    Source("Google News", google_topic("WORLD", GN_INDIA), "World", kind="google_news"),
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
    # India
    "the hindu": 91, "press trust of india": 90, "pti": 90, "the indian express": 88, "indian express": 88,
    "business standard": 87, "mint": 86, "livemint": 86, "the economic times": 85, "economic times": 85,
    "hindustan times": 83, "ndtv": 82, "the new indian express": 82, "deccan herald": 82, "theprint": 82,
    "india today": 80, "scroll": 80, "the news minute": 80, "the times of india": 78, "times of india": 78,
    "moneycontrol": 78, "onmanorama": 78, "mathrubhumi": 78, "ani": 78, "the federal": 75, "the quint": 74,
    "deccan chronicle": 72, "firstpost": 72, "telangana today": 70, "news18": 70, "zee news": 60,
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
