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

    def test_readiness_reports_all_required_dependencies(self):
        checks = app.readiness_checks()
        self.assertEqual(set(checks), {"api_key", "ffmpeg", "static"})
        self.assertTrue(checks["ffmpeg"])
        self.assertTrue(checks["static"])

    def test_production_api_docs_are_not_exposed(self):
        if app.DEPLOY_MODE:
            self.assertIsNone(app.app.docs_url)
            self.assertIsNone(app.app.openapi_url)

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

    def test_transcript_text_supports_whole_text_and_sentences(self):
        payload = {
            "transcripts": [
                {"text": "第一段完整转写。"},
                {"sentences": [{"text": "第二段。"}, {"text": "第三段。"}]},
            ]
        }
        self.assertEqual(
            app._transcript_text(payload),
            "第一段完整转写。\n第二段。第三段。",
        )

    def test_remote_asr_submits_polls_and_reads_transcript(self):
        submitted = mock.Mock(status_code=200)
        submitted.json.return_value = {"output": {"task_id": "task-1"}}
        completed = mock.Mock(status_code=200)
        completed.json.return_value = {
            "output": {
                "task_status": "SUCCEEDED",
                "result": {"transcription_url": "https://example.com/result.json"},
            }
        }
        transcript = mock.Mock(status_code=200)
        transcript.json.return_value = {
            "transcripts": [{"sentences": [{"text": "这是语音内容。"}]}]
        }
        transcript.raise_for_status.return_value = None
        with mock.patch("app.requests.post", return_value=submitted) as post, \
             mock.patch("app.requests.get", side_effect=[completed, transcript]) as get:
            text = app.transcribe_remote_audio(
                "https://example.com/video.mp4", {"api_key": "test-key"}, timeout=1)
        self.assertEqual(text, "这是语音内容。")
        self.assertEqual(post.call_args.kwargs["json"]["input"]["file_url"],
                         "https://example.com/video.mp4")
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
