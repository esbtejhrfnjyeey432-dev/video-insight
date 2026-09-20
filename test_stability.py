# -*- coding: utf-8 -*-
"""不调用外部平台或 AI 的后端稳定性测试。"""
import asyncio
import unittest

from fastapi import HTTPException

import app


class StabilityTests(unittest.TestCase):
    def test_health_is_dependency_free(self):
        self.assertEqual(app.health(), {"ok": True, "service": "video-insight"})

    def test_analysis_queue_times_out_cleanly(self):
        async def scenario():
            old_timeout = app.ANALYSIS_QUEUE_TIMEOUT
            app.ANALYSIS_QUEUE_TIMEOUT = 1
            await app.acquire_analysis_slot()
            try:
                with self.assertRaises(HTTPException) as caught:
                    await app.acquire_analysis_slot()
                self.assertEqual(caught.exception.status_code, 503)
            finally:
                app._analysis_slots.release()
                app.ANALYSIS_QUEUE_TIMEOUT = old_timeout

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
