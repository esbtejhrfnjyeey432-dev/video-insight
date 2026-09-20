# -*- coding: utf-8 -*-
"""不调用外部平台或 AI 的后端稳定性测试。"""
import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

import app
import resolver


class StabilityTests(unittest.TestCase):
    def test_health_is_dependency_free(self):
        self.assertEqual(app.health(), {"ok": True, "service": "video-insight"})

    def test_analysis_queue_times_out_cleanly(self):
        async def scenario():
            old_timeout = app.ANALYSIS_QUEUE_TIMEOUT
            app.ANALYSIS_QUEUE_TIMEOUT = 1
            first_slot = await app.acquire_analysis_slot("link")
            try:
                with self.assertRaises(HTTPException) as caught:
                    await app.acquire_analysis_slot("link")
                self.assertEqual(caught.exception.status_code, 503)
            finally:
                first_slot.release()
                app.ANALYSIS_QUEUE_TIMEOUT = old_timeout

        asyncio.run(scenario())

    def test_private_network_urls_are_rejected(self):
        with self.assertRaises(resolver.ResolveError):
            resolver.validate_public_url("http://127.0.0.1/admin")
        with mock.patch("resolver.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("10.0.0.8", 443)),
        ]):
            with self.assertRaises(resolver.ResolveError):
                resolver.validate_public_url("https://example.com/video")

    def test_public_url_is_allowed(self):
        with mock.patch("resolver.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("93.184.216.34", 443)),
        ]):
            self.assertEqual(
                resolver.validate_public_url("https://example.com/video.mp4"),
                "https://example.com/video.mp4",
            )

    def test_light_and_heavy_jobs_use_separate_pools(self):
        async def scenario():
            link_slot = await app.acquire_analysis_slot("link")
            try:
                frame_slot = await app.acquire_analysis_slot("frames")
                frame_slot.release()
            finally:
                link_slot.release()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
