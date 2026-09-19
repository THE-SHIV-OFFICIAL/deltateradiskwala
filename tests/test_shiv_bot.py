import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from SHIV_BOT.SHIV_config import Settings
from SHIV_BOT.SHIV_database import Database
from SHIV_BOT.SHIV_extractors import ExtractionError, MediaResolver
from SHIV_BOT.SHIV_formatting import render_premium


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp_dir.name) / "shiv.sqlite3"))
        await self.database.connect()

    async def asyncTearDown(self):
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_quota_and_payment_ownership(self):
        await self.database.upsert_user(100, "shiv", "Shiv")
        await self.database.upsert_user(200, "other", "Other")

        for expected in (2, 1, 0):
            allowed, remaining = await self.database.consume_quota(100, "downloads", 3)
            self.assertTrue(allowed)
            self.assertEqual(expected, remaining)

        allowed, remaining = await self.database.consume_quota(100, "downloads", 3)
        self.assertFalse(allowed)
        self.assertEqual(0, remaining)

        payment_id = await self.database.create_payment(100, "pro_7d", 15)
        self.assertFalse(
            await self.database.attach_payment_submission(
                payment_id, 200, utr="WRONGUSER"
            )
        )
        self.assertTrue(
            await self.database.attach_payment_submission(
                payment_id, 100, utr="RIGHTUSER"
            )
        )
        payment = await self.database.review_payment(payment_id, 1000, True)
        self.assertIsNotNone(payment)
        self.assertEqual("pro", await self.database.effective_plan(100))

        await self.database._db().execute(
            "UPDATE users SET expires_at=1 WHERE user_id=?", (100,)
        )
        await self.database._db().commit()
        refreshed = await self.database.refresh_expired_premium()
        self.assertEqual([100], [row["user_id"] for row in refreshed])
        self.assertEqual("free", await self.database.effective_plan(100))


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            max_url_length=2048,
            terabox_api_url="",
            terabox_api_key="",
            terabox_cookie="",
            diskwalla_api_url="",
            diskwalla_api_key="",
            diskwalla_cookie="",
        )
        self.resolver = MediaResolver(self.settings)

    async def test_direct_media_link(self):
        result = await self.resolver.resolve("https://93.184.216.34/video.mp4")
        self.assertEqual("direct", result.source)
        self.assertEqual("video/mp4", result.mime)

    async def test_private_and_unsupported_links_are_rejected(self):
        with self.assertRaises(ExtractionError):
            await self.resolver.resolve("http://127.0.0.1/video.mp4")
        with self.assertRaises(ExtractionError):
            await self.resolver.resolve("https://93.184.216.34/page.html")

    async def test_provider_requires_configured_resolver(self):
        with self.assertRaisesRegex(ExtractionError, "TeraBox resolver"):
            await self.resolver.resolve("https://terabox.com/s/abc")


class ConfigTests(unittest.TestCase):
    def test_required_configuration_and_defaults(self):
        values = {
            "BOT_TOKEN": "test-token",
            "API_ID": "12345",
            "API_HASH": "test-hash",
            "ADMIN_IDS": "100,200",
            "OWNER_ID": "300",
            "CUSTOM_EMOJI_IDS": "premium:123456789,success:987654321",
        }
        with patch.dict(os.environ, values, clear=False):
            settings = Settings.from_env()
        self.assertEqual((100, 200, 300), settings.admin_ids)
        self.assertEqual("data/shiv_deltatera.sqlite3", settings.database_path)
        self.assertEqual(123456789, settings.custom_emoji_ids["premium"])

    def test_custom_emoji_falls_back_and_renders_entity_when_configured(self):
        fallback_text, fallback_entities = render_premium(
            "[[emoji:premium]] Plan", {}
        )
        self.assertEqual("💎 Plan", fallback_text)
        self.assertEqual([], fallback_entities)

        rich_text, rich_entities = render_premium(
            "[[emoji:premium]] Plan", {"premium": 123456789}
        )
        self.assertEqual("💎 Plan", rich_text)
        self.assertEqual(1, len(rich_entities))
        self.assertEqual(123456789, rich_entities[0].custom_emoji_id)


if __name__ == "__main__":
    unittest.main()