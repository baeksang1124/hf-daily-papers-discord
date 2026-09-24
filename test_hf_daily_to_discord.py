"""hf_daily_to_discord 테스트. 네트워크 없이 가짜 응답으로 돈다.

실행: python -m unittest -v
"""

import io
import datetime as dt
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import requests

import hf_daily_to_discord as m


class FakeResp:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def fake_papers(n):
    return [
        {"title": f"Paper {i}",
         "paper": {"id": f"2609.{i:05d}", "ai_summary": "s" * 200,
                   "upvotes": i, "ai_keywords": ["a", "b"]}}
        for i in range(n)
    ]


def run_main(argv):
    """main()을 돌리고 (종료 코드, stdout, stderr)를 돌려준다. argparse 오류는 SystemExit 코드로."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = m.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class DateTest(unittest.TestCase):
    def test_default_date_is_yesterday_in_kst(self):
        # UTC 00:30 = KST 09:30 → 어제는 9/23
        now = dt.datetime(2026, 9, 24, 0, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(m.default_date(now), "2026-09-23")
        # UTC 9/23 20:00 = KST 9/24 05:00 → 어제는 9/23 (UTC 날짜가 아니라 KST 기준)
        now = dt.datetime(2026, 9, 23, 20, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(m.default_date(now), "2026-09-23")

    def test_parse_date(self):
        self.assertEqual(m.parse_date("2026-06-02"), "2026-06-02")
        for bad in ("2026/06/02", "20260602", "2026-13-01", "yesterday"):
            with self.assertRaises(Exception, msg=bad):
                m.parse_date(bad)

    def test_bad_date_arg_exits_2(self):
        code, _, _ = run_main(["--date", "2026/06/02", "--dry-run"])
        self.assertEqual(code, 2)


@mock.patch.object(m.time, "sleep", lambda s: None)
class FetchTest(unittest.TestCase):
    def test_empty_day_returns_empty_list(self):
        with mock.patch.object(m.requests, "get", return_value=FakeResp(body=[])):
            self.assertEqual(m.fetch_daily("2026-09-20"), [])

    def test_error_raises_after_retries(self):
        get = mock.Mock(side_effect=requests.ConnectionError("down"))
        with mock.patch.object(m.requests, "get", get), redirect_stderr(io.StringIO()):
            with self.assertRaises(m.FetchError):
                m.fetch_daily("2026-09-23")
        self.assertEqual(get.call_count, m.FETCH_ATTEMPTS)

    def test_transient_error_then_success(self):
        get = mock.Mock(side_effect=[requests.ConnectionError("blip"), FakeResp(body=fake_papers(2))])
        with mock.patch.object(m.requests, "get", get), redirect_stderr(io.StringIO()):
            self.assertEqual(len(m.fetch_daily("2026-09-23")), 2)

    def test_non_list_response_raises(self):
        with mock.patch.object(m.requests, "get", return_value=FakeResp(body={"error": "x"})):
            with self.assertRaises(m.FetchError):
                m.fetch_daily("2026-09-23")


@mock.patch.object(m.time, "sleep", lambda s: None)
@mock.patch.object(m, "TRANSLATE", False)
@mock.patch.object(m, "DISCORD_WEBHOOK_URL", "https://example.invalid/webhook")
class MainTest(unittest.TestCase):
    def run_with(self, papers, post_side_effect, argv=("--date", "2026-09-23")):
        post = mock.Mock(side_effect=post_side_effect)
        with mock.patch.object(m, "fetch_daily", return_value=papers), \
                mock.patch.object(m.requests, "post", post):
            code, out, err = run_main(list(argv))
        return code, out, err, post

    def test_success_exits_0(self):
        code, out, _, post = self.run_with(fake_papers(3), lambda *a, **k: FakeResp(204))
        self.assertEqual(code, 0)
        self.assertEqual(post.call_count, 1)
        self.assertIn("[done]", out)

    def test_empty_day_exits_0_without_posting(self):
        code, out, _, post = self.run_with([], lambda *a, **k: FakeResp(204))
        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIn("논문 없음", out)

    def test_fetch_failure_exits_1(self):
        with mock.patch.object(m, "fetch_daily", side_effect=m.FetchError("down")):
            code, _, err = run_main(["--date", "2026-09-23"])
        self.assertEqual(code, 1)
        self.assertIn("[error]", err)

    def test_missing_webhook_exits_1(self):
        with mock.patch.object(m, "DISCORD_WEBHOOK_URL", ""):
            code, _, _ = run_main(["--date", "2026-09-23"])
        self.assertEqual(code, 1)

    def test_discord_error_exits_1(self):
        code, out, err, _ = self.run_with(fake_papers(40), lambda *a, **k: FakeResp(500, text="err"))
        self.assertEqual(code, 1)
        self.assertIn("discord 500", err)
        self.assertNotIn("[done]", out)

    def test_429_then_success_exits_0(self):
        responses = iter([FakeResp(429, body={"retry_after": 0.1}), FakeResp(204)])
        code, _, _, post = self.run_with(fake_papers(3), lambda *a, **k: next(responses))
        self.assertEqual(code, 0)
        self.assertEqual(post.call_count, 2)

    def test_429_twice_is_logged_and_exits_1(self):
        code, _, err, post = self.run_with(
            fake_papers(3), lambda *a, **k: FakeResp(429, body={"retry_after": 0.1}))
        self.assertEqual(code, 1)
        self.assertEqual(post.call_count, 2)  # 원래 요청 1번 + 재시도 1번
        self.assertIn("discord 429", err)

    def test_network_error_does_not_log_webhook_url(self):
        secret = "https://discord.com/api/webhooks/123/SECRET_TOKEN"
        exc = requests.ConnectionError(f"Max retries exceeded with url: {secret}")
        with mock.patch.object(m, "DISCORD_WEBHOOK_URL", secret):
            code, out, err, _ = self.run_with(fake_papers(3), exc)
        self.assertEqual(code, 1)
        self.assertNotIn("SECRET_TOKEN", out + err)

    def test_dry_run_does_not_post(self):
        code, out, _, post = self.run_with(
            fake_papers(3), lambda *a, **k: FakeResp(204), argv=("--date", "2026-09-23", "--dry-run"))
        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIn("embed 1/1", out)


class FormatTest(unittest.TestCase):
    def test_chunks_respect_limit_and_keep_order(self):
        blocks = [f"block-{i}-" + "x" * 500 for i in range(30)]
        chunks = m.chunk_blocks(blocks)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= m.CHUNK_CHAR_LIMIT for c in chunks))
        self.assertEqual("\n\n".join(chunks), "\n\n".join(blocks))

    def test_title_with_markdown_chars(self):
        items = [{"title": "[Survey] A*B_C ~test~", "one_liner": "2*3 = 6",
                  "keywords": ["k`1", "k2"], "upvotes": 5,
                  "url": "https://huggingface.co/papers/2609.00001"}]
        block = m.build_blocks(items)[0]
        head, one_liner, keywords = block.split("\n")
        self.assertEqual(
            head,
            r"**[(Survey) A\*B\_C \~test\~](https://huggingface.co/papers/2609.00001)**  ▲5")
        self.assertEqual(one_liner, r"2\*3 = 6")
        self.assertEqual(keywords, "`k1` `k2`")

    def test_extract_collapses_newlines(self):
        items = m.extract([{"title": "Line one\n  line two",
                            "paper": {"id": "1", "summary": "abs\n\ntract", "upvotes": 1}}])
        self.assertEqual(items[0]["title"], "Line one line two")
        self.assertEqual(items[0]["one_liner"], "abs tract")


if __name__ == "__main__":
    unittest.main()
