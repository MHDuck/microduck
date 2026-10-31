"""Browser regression tests with mocked RPC: no robot or network connection.

Run with Playwright installed; WEB_TEST_BROWSER can select an installed browser.
"""
import importlib.util
import os
from pathlib import Path
import unittest


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is required")
class StandUpButtons(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.runtime = await async_playwright().start()
        options = {"headless": True}
        if os.environ.get("WEB_TEST_BROWSER"):
            options["executable_path"] = os.environ["WEB_TEST_BROWSER"]
        self.browser = await self.runtime.chromium.launch(**options)
        self.page = await self.browser.new_page()
        await self.page.route("**/*", lambda route: route.abort())
        html = Path(__file__).resolve().parents[1] / "mediad/webclient/index.html"
        await self.page.set_content(html.read_text(encoding="utf-8"))
        await self.page.evaluate("""() => {
          window.calls = []; window.sent = [];
          window.health = {healthy: true, imu: {ready: true}};
          window.models = {stand: 'standup.onnx'};
          window.enableResult = {accepted: true};
          call = async (method, params = {}) => {
            window.calls.push({method, params});
            if (method === 'robot.health') return window.health;
            if (method === 'robot.subscribe') return window.models;
            return method === 'robot.enable' ? window.enableResult : {accepted: true};
          };
          tell = (method, params) => window.sent.push({method, params});
        }""")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def start(self, index=0):
        await self.page.locator("button[data-stand-up]").nth(index).click()
        await self.page.wait_for_function("window.calls.some(c => c.method === 'robot.enable')")

    async def complete(self):
        await self.page.evaluate("onState({policy: 'standup'}); onState({policy: 'stand'})")

    async def test_both_buttons_request_explicit_standup(self):
        for index in (0, 1):
            await self.start(index)
            command = await self.page.evaluate("window.calls.filter(c => c.method === 'robot.enable').at(-1)")
            self.assertEqual(command["params"], {"on": True, "toggle": False, "stand_up": True, "ignore_protection": True})
            self.assertNotIn("robot.init", await self.page.evaluate("window.calls.map(c => c.method)"))
            await self.complete()
            self.assertIn("起身完成", await self.page.locator("#standup-status").text_content())

    async def test_unready_imu_does_not_enable(self):
        await self.page.evaluate("window.health.imu.ready = false")
        await self.page.locator("button[data-stand-up]").first.click()
        self.assertFalse(await self.page.evaluate("window.calls.some(c => c.method === 'robot.enable')"))
        self.assertIn("IMU", await self.page.locator("#standup-status").text_content())

    async def test_missing_model_does_not_enable(self):
        await self.page.evaluate("window.models = {}")
        await self.page.locator("button[data-stand-up]").first.click()
        self.assertFalse(await self.page.evaluate("window.calls.some(c => c.method === 'robot.enable')"))
        self.assertIn("未配置", await self.page.locator("#standup-status").text_content())

    async def test_refusal_is_visible_and_allows_manual_retry(self):
        await self.page.evaluate("window.enableResult = {accepted:false, reason:'overcurrent protection latched'}")
        await self.start()
        self.assertIn("overcurrent", await self.page.locator("#standup-status").text_content())
        self.assertTrue(await self.page.locator("button[data-stand-up]").first.is_enabled())

    async def test_no_duplicate_or_stale_direction_during_recovery(self):
        await self.page.evaluate("keys.add('w'); refreshTwist()")
        await self.start()
        await self.page.evaluate("window.sent = []")
        await self.page.evaluate("standUp()")
        await self.page.keyboard.down("w")
        await self.page.wait_for_timeout(150)
        self.assertEqual(await self.page.evaluate("window.calls.filter(c => c.method === 'robot.enable').length"), 1)
        self.assertEqual(await self.page.evaluate("window.sent.length"), 0)
        await self.complete()
        self.assertEqual(await self.page.evaluate("[twist.vx, padTwist.vx, keys.size]"), [0, 0, 0])
        await self.page.keyboard.up("w")

    async def test_fault_after_start_is_not_reported_as_success(self):
        await self.start()
        await self.page.evaluate("onState({policy:'standup'}); onState({policy:'protection'})")
        self.assertIn("起身失败", await self.page.locator("#standup-status").text_content())

    async def test_cancelled_request_cannot_resume_during_new_attempt(self):
        await self.page.evaluate("""() => {
          window.healthReplies = [];
          const original = call;
          call = (method, params) => method === 'robot.health'
            ? new Promise(resolve => window.healthReplies.push(resolve)) : original(method, params);
        }""")
        await self.page.locator("button[data-stand-up]").first.click()
        await self.page.locator('[data-call="robot.relax"]').click()
        await self.page.locator("button[data-stand-up]").last.click()
        await self.page.evaluate("window.healthReplies.shift()(window.health)")
        self.assertFalse(await self.page.evaluate("window.calls.some(c => c.method === 'robot.enable')"))
        await self.page.evaluate("window.healthReplies.shift()(window.health)")
        await self.page.wait_for_function("window.calls.some(c => c.method === 'robot.enable')")
        self.assertEqual(await self.page.evaluate("window.calls.filter(c => c.method === 'robot.enable').length"), 1)

    async def test_disconnect_does_not_report_completed_or_auto_retry(self):
        await self.start()
        await self.page.evaluate("stopTelemetry(); onState({policy:'stand'})")
        self.assertIn("连接已断开", await self.page.locator("#standup-status").text_content())
        self.assertEqual(await self.page.evaluate("window.calls.filter(c => c.method === 'robot.enable').length"), 1)


if __name__ == "__main__":
    unittest.main()
