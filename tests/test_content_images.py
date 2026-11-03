"""Offline tests for Agent 3 (content creator) and Agent 4 (image designer): fake Gemini, Jev and
Wikimedia, no network or API keys."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from PIL import Image

from justnews.agents.content import ContentCreatorAgent, normalize_hashtags
from justnews.agents.images import ImageDesignerAgent, license_allowed
from justnews.config import Settings
from justnews.db import Database
from justnews.llm import LLMError, Usage
from justnews.models import Article, Evaluation

NOW = datetime.now(timezone.utc)


class FakeGemini:
    def __init__(self, model, posts=None, error=None):
        self.model, self.posts, self.error = model, posts or {}, error
        self.usage = Usage(model, 0, 0)
        self.prompts = []

    def generate_json(self, system, prompt, schema):
        self.prompts.append(prompt)
        if self.error:
            self.usage.failed_calls += 1
            raise self.error
        self.usage.calls += 1
        if "DRAFTS AND NOTES:" in prompt:  # a revision round: this model changes nothing
            items = json.loads(prompt.split("DRAFTS AND NOTES:\n", 1)[1])
            return {"posts": [{"id": it["id"], **it["draft"], "reply": "Kept it."} for it in items]}
        items = json.loads(prompt.split("ARTICLES:\n", 1)[1])
        return {"posts": [{"id": it["id"], **self.posts.get(it["headline"], {})} for it in items]}


class FakeJev:
    model = "jev-test"

    def __init__(self, flag=(), fit=0.9):
        self.flag, self.fit = set(flag), fit
        self.usage = Usage("jev-test", 0.042, 0)

    def review_posts(self, posts, sources):
        return {k: "Jev: may state facts not in the source (p=0.80)" for k, p in posts.items() if p["caption"] in self.flag}

    def choose_photo(self, post, candidates):
        return next(iter(candidates)), self.fit


def draft(**kw):
    base = {"headline": "Automakers push permanent ban on Chinese cars",
            "caption": "Major automakers asked Congress to make a ban on Chinese vehicles permanent, according to Reuters.",
            "hashtags": ["Automotive", "trade policy", "#China", "automotive"], "image_text": "Automakers push Chinese car ban.",
            "visual": "graphic", "image_query": ""}
    return {**base, **kw}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = replace(Settings(), db_path=root / "t.db", output_dir=root / "out", typesafe_api_key="",
                                gemini_api_key="", max_posts_per_run=10)
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def recommend(self, *titles, run_id="r1"):
        arts = [Article(title=t, url=f"https://reuters.com/{i}", source_name="Reuters", category="Politics",
                        published_at=NOW - timedelta(hours=1), description="Summary of the story.")
                for i, t in enumerate(titles)]
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, arts)
        self.db.save_evaluations(run_id, [Evaluation(a.id, 80, 90, 90, 80, 85 - i, "Recommended", "ok")
                                          for i, a in enumerate(arts)])
        return arts

    # --- Agent 3 ----------------------------------------------------------------
    def test_hashtags_are_normalized(self):
        self.assertEqual(normalize_hashtags(["Automotive", "trade policy", "#China", "automotive", "2026"]),
                         ["#Automotive", "#TradePolicy", "#China"])

    def test_hashtags_drop_unsupported_years_and_publisher(self):
        source = "Trump, Vance and candidates make final push before US midterms in 2026"
        self.assertEqual(normalize_hashtags(["USPolitics", "Campaign2024", "Midterms2026", "WiredNews"], source, "Wired"),
                         ["#USPolitics", "#Midterms2026"])

    def test_creates_validated_drafts_with_code_built_attribution(self):
        arts = self.recommend("Automakers urge Congress to ban Chinese vehicles", "Second story")
        gemini = FakeGemini("gemini-a", {arts[0].title: draft(), arts[1].title: draft(caption="")})
        result = ContentCreatorAgent(self.settings, self.db, clients=[gemini], verifier=False).run("r1")
        self.assertEqual((result.created, result.pending), (1, 1))  # empty caption -> retried next run
        post = self.db.posts_for_run("r1")[0]
        self.assertEqual(post["attribution"], "Source: Reuters – https://reuters.com/0")
        self.assertEqual(json.loads(post["hashtags"]), ["#Automotive", "#TradePolicy", "#China"])
        self.assertEqual(post["image_text"], "Automakers push Chinese car ban")  # trailing period removed
        self.assertEqual(post["status"], "draft")
        self.assertIn("Automakers urge Congress", gemini.prompts[0])
        # Already-written posts are not sent to Gemini again.
        again = FakeGemini("gemini-a", {arts[1].title: draft()})
        ContentCreatorAgent(self.settings, self.db, clients=[again], verifier=False).run("r1")
        self.assertNotIn("Automakers urge Congress", again.prompts[0])

    def test_falls_back_to_next_model_when_daily_quota_is_used_up(self):
        arts = self.recommend("Automakers urge Congress to ban Chinese vehicles")
        first = FakeGemini("gemini-big", error=LLMError("daily free-tier quota exhausted", fatal=True))
        second = FakeGemini("gemini-lite", {arts[0].title: draft()})
        result = ContentCreatorAgent(self.settings, self.db, clients=[first, second], verifier=False).run("r1")
        self.assertEqual(result.created, 1)
        self.assertEqual(self.db.posts_for_run("r1")[0]["model"], "gemini-lite")

    def test_timeout_on_one_model_falls_back_to_next(self):
        arts = self.recommend("Automakers urge Congress to ban Chinese vehicles")
        hung = FakeGemini("gemini-big", error=LLMError("ReadTimeout: read timed out"))
        lite = FakeGemini("gemini-lite", {arts[0].title: draft()})
        result = ContentCreatorAgent(self.settings, self.db, clients=[hung, lite], verifier=False).run("r1")
        self.assertEqual(result.created, 1)

    def test_all_models_unavailable_leaves_posts_pending(self):
        self.recommend("Automakers urge Congress to ban Chinese vehicles")
        dead = FakeGemini("gemini-a", error=LLMError("daily free-tier quota exhausted", fatal=True))
        result = ContentCreatorAgent(self.settings, self.db, clients=[dead], verifier=False).run("r1")
        self.assertEqual((result.created, result.pending), (0, 1))
        self.assertIn("quota", result.error)

    def test_jev_flags_unsupported_caption_for_review(self):
        arts = self.recommend("Automakers urge Congress to ban Chinese vehicles")
        bad = "Automakers demanded a $50bn ban that will start next week."
        gemini = FakeGemini("gemini-a", {arts[0].title: draft(caption=bad)})
        result = ContentCreatorAgent(self.settings, self.db, clients=[gemini], verifier=FakeJev(flag=[bad])).run("r1")
        self.assertEqual(result.needs_review, 1)
        post = self.db.posts_for_run("r1")[0]
        self.assertEqual(post["status"], "needs_review")
        self.assertIn("facts not in the source", post["review_note"])

    # --- Agent 4 ----------------------------------------------------------------
    def test_license_filter(self):
        allowed = ["cc0", "pd", "cc-by", "cc-by-sa"]
        for ok in ("cc0", "pd", "cc-by-4.0", "cc-by-sa-3.0", "cc-by-2.0"):
            self.assertTrue(license_allowed(ok, allowed), ok)
        for bad in ("cc-by-nc-4.0", "cc-by-nd-4.0", "cc-by-nc-sa-2.0", "gfdl", "", "fair use"):
            self.assertFalse(license_allowed(bad, allowed), bad)
        self.assertFalse(license_allowed("cc-by-sa-4.0", ["cc0", "cc-by"]))  # cc-by does not admit BY-SA

    def make_posts(self, visual, query=""):
        arts = self.recommend("Microsoft AI chief warns on conscious machines")
        gemini = FakeGemini("gemini-a", {arts[0].title: draft(visual=visual, image_query=query)})
        ContentCreatorAgent(self.settings, self.db, clients=[gemini], verifier=False).run("r1")

    def test_graphic_is_rendered_at_configured_size(self):
        self.make_posts("graphic")
        result = ImageDesignerAgent(self.settings, self.db, chooser=False).run("r1")
        self.assertEqual((result.graphics, result.photos, result.failed), (1, 0, 0))
        post = self.db.posts_for_run("r1")[0]
        with Image.open(post["image_path"]) as img:
            self.assertEqual(img.size, (1080, 1080))
        self.assertEqual(post["image_kind"], "graphic")

    def test_licensed_photo_used_with_credit_and_unlicensed_skipped(self):
        self.make_posts("photo", "Microsoft headquarters")
        session = FakeCommons()
        result = ImageDesignerAgent(self.settings, self.db, chooser=FakeJev(fit=0.9), session=session).run("r1")
        self.assertEqual(result.photos, 1)
        post = self.db.posts_for_run("r1")[0]
        self.assertEqual(post["image_credit"], "File photo: Jane Doe / Wikimedia Commons, CC BY 4.0")
        self.assertEqual(post["image_license"], "CC BY 4.0")
        self.assertNotIn("nc.jpg", " ".join(session.fetched))  # the NC-licensed result was never downloaded

    def test_poor_photo_fit_falls_back_to_graphic(self):
        self.make_posts("photo", "Microsoft headquarters")
        result = ImageDesignerAgent(self.settings, self.db, chooser=FakeJev(fit=0.2), session=FakeCommons()).run("r1")
        self.assertEqual((result.photos, result.graphics), (0, 1))
        self.assertIn("clearly showed", self.db.posts_for_run("r1")[0]["image_note"])


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1600, 1200), (40, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


class FakeCommons:
    """Mimics the Commons search API and thumbnail downloads."""

    def __init__(self):
        self.headers, self.fetched = {}, []

    def get(self, url, timeout=None):
        self.fetched.append(url)
        if "api.php" in url:
            def page(i, name, lic, short):
                meta = {"License": {"value": lic}, "LicenseShortName": {"value": short},
                        "Artist": {"value": "<a href='x'>Jane Doe</a>"}, "ImageDescription": {"value": "Building"}}
                return {"index": i, "title": f"File:{name}", "imageinfo": [{
                    "mime": "image/jpeg", "width": 3000, "height": 2000, "thumburl": f"https://upload.example/{name}",
                    "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}", "extmetadata": meta}]}
            body = {"query": {"pages": {"1": page(1, "nc.jpg", "cc-by-nc-4.0", "CC BY-NC 4.0"),
                                        "2": page(2, "hq.jpg", "cc-by-4.0", "CC BY 4.0")}}}
            return NS(status_code=200, json=lambda: body, headers={"Content-Type": "application/json"})
        return NS(status_code=200, content=_jpeg(), headers={"Content-Type": "image/jpeg"})


if __name__ == "__main__":
    unittest.main()
