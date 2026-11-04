"""Where a story is about, and the India-first mix of each shortlist.

`detect_region` reads the headline and summary: South Indian states, cities, parties and leaders
mean "South India"; the rest of India means "India"; otherwise the source's own region decides
(Indian outlets "India", world feeds "International"). An Indian outlet's world story stays
"International" only when it never mentions India.

`select_mix` picks the posts for one shortlist from the run's recommended stories (best first):
  - International: at most `international_share` (20%) of the list, and only stories scoring at
    least `international_min_score`, i.e. important world news
  - South India: up to `south_india_share` (40%) of the list is kept for South Indian stories
  - India and South India fill the rest, best score first
"""

from __future__ import annotations

import math
import re

SOUTH_INDIA, INDIA, INTERNATIONAL = "South India", "India", "International"

SOUTH_TERMS = [
    "tamil nadu", "chennai", "coimbatore", "madurai", "tiruchi", "trichy", "salem", "tirunelveli",
    "kerala", "keralam", "kochi", "cochin", "thiruvananthapuram", "trivandrum", "kozhikode", "thrissur",
    "karnataka", "bengaluru", "bangalore", "mysuru", "mysore", "mangaluru", "hubballi", "belagavi",
    "andhra pradesh", "andhra", "amaravati", "visakhapatnam", "vizag", "vijayawada", "tirupati", "guntur",
    "telangana", "hyderabad", "secunderabad", "warangal", "puducherry", "pondicherry", "lakshadweep",
    "dmk", "aiadmk", "ysrcp", "tdp", "brs", "m.k. stalin", "mk stalin", "siddaramaiah", "shivakumar", "revanth reddy",
    "chandrababu naidu", "pawan kalyan", "pinarayi", "jagan mohan", "udhayanidhi", "sabarimala",
    "tollywood", "kollywood", "mollywood", "sandalwood", "south india", "southern india", "southern states",
]
INDIA_TERMS = [
    "india", "indian", "indians", "delhi", "new delhi", "mumbai", "kolkata", "pune", "ahmedabad", "jaipur",
    "lucknow", "patna", "bhopal", "chandigarh", "noida", "gurugram", "gurgaon", "goa", "kashmir", "ladakh",
    "uttar pradesh", "bihar", "maharashtra", "gujarat", "rajasthan", "punjab", "west bengal", "bengal",
    "assam", "odisha", "madhya pradesh", "jharkhand", "haryana", "uttarakhand", "himachal", "manipur",
    "modi", "bjp", "lok sabha", "rajya sabha", "rahul gandhi", "election commission", "rbi",
    "reserve bank of india", "sensex", "nifty", "rupee", "isro", "adani", "ambani", "reliance industries",
    "reliance jio", "tata",
    "infosys", "wipro", "hcltech", "bollywood", "ipl", "bcci", "aadhaar", "upi",
]
_SOUTH_RE = re.compile(r"\b(" + "|".join(map(re.escape, SOUTH_TERMS)) + r")\b", re.I)
_INDIA_RE = re.compile(r"\b(" + "|".join(map(re.escape, INDIA_TERMS)) + r")\b", re.I)


def detect_region(title: str, description: str = "", source_region: str = INTERNATIONAL) -> str:
    text = f"{title} {description}"
    if _SOUTH_RE.search(text):
        return SOUTH_INDIA
    if _INDIA_RE.search(text):
        return INDIA
    if source_region == SOUTH_INDIA:
        return SOUTH_INDIA
    # A national Indian feed's story with no Indian place or name in it is still about India
    # (local news), except in the World section, which the source marks International.
    return INDIA if source_region == INDIA else INTERNATIONAL


def select_mix(rows: list, limit: int, international_share: float = 0.2, international_min_score: int = 85,
               south_india_share: float = 0.4) -> list:
    """`rows` are recommended stories with `overall` and `region` (worked out from the text for
    stories saved before regions existed)."""
    rows = sorted(({**dict(r), "region": r["region"] or detect_region(r["title"], r["description"] or "")}
                   for r in rows), key=lambda r: -(r["overall"] or 0))
    world_cap = math.floor(limit * international_share + 1e-9)
    south_slots = math.floor(limit * south_india_share + 1e-9)
    world = [r for r in rows if r["region"] == INTERNATIONAL and (r["overall"] or 0) >= international_min_score][:world_cap]
    south = [r for r in rows if r["region"] == SOUTH_INDIA][:south_slots]
    picked = {id(r) for r in world + south}
    rest = [r for r in rows if r["region"] in (INDIA, SOUTH_INDIA) and id(r) not in picked]
    chosen = world + south + rest[:max(0, limit - len(world) - len(south))]
    return sorted(chosen, key=lambda r: -(r["overall"] or 0))[:limit]
