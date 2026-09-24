"""hf_daily_to_discord 테스트. 네트워크 없이 가짜 응답으로 돈다.

실행: python -m unittest -v
"""

import io
import json
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
    # 키워드 분류로 LLM 분야에 들어가도록 제목에 'language model'을 넣는다.
    return [
        {"title": f"Language model paper {i}",
         "paper": {"id": f"2609.{i:05d}", "ai_summary": "s" * 200,
                   "upvotes": i, "ai_keywords": ["a", "b"]}}
        for i in range(n)
    ]


def item(title, one_liner="", keywords=(), upvotes=1, cat=None):
    p = {"id": "2609.00001", "title": title, "one_liner": one_liner, "keywords": list(keywords),
         "upvotes": upvotes, "url": "https://huggingface.co/papers/2609.00001"}
    if cat:
        p["cat"] = cat
    return p


def anthropic_resp(results):
    """Anthropic Messages API 응답 흉내. results는 [{"ko":..., "cat":...}, ...]."""
    text = json.dumps(results, ensure_ascii=False)
    return FakeResp(200, body={"stop_reason": "end_turn",
                               "content": [{"type": "text", "text": text}]})


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
        self.assertIn("관심 분야 3편 / 전체 3편", out)

    def test_other_papers_are_counted_not_sent(self):
        papers = fake_papers(2) + [{"title": "Protein folding with graph networks",
                                    "paper": {"id": "2609.99999", "upvotes": 100}}]
        code, out, _, _ = self.run_with(
            papers, lambda *a, **k: FakeResp(204), argv=("--date", "2026-09-23", "--dry-run"))
        self.assertEqual(code, 0)
        self.assertNotIn("Protein folding", out)
        self.assertIn("그 외 분야 1편 제외", out)
        self.assertIn("관심 분야 2편 / 전체 3편", out)


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

    def test_sections_follow_category_order_with_header_on_first_paper(self):
        items = [item("gen", cat="생성형"), item("llm a", cat="LLM"), item("other", cat="기타"),
                 item("llm b", cat="LLM")]
        blocks, shown = m.build_sections(items)
        self.assertEqual(shown, 3)
        self.assertTrue(blocks[0].startswith("__**🧠 LLM · 2편**__\n**[llm a]"))
        self.assertTrue(blocks[1].startswith("**[llm b]"))
        self.assertTrue(blocks[2].startswith("__**🎨 생성형 · 1편**__\n**[gen]"))
        self.assertEqual(blocks[3], "*그 외 분야 1편 제외*")
        self.assertNotIn("other", "".join(blocks))

    def test_sections_when_nothing_matches(self):
        blocks, shown = m.build_sections([item("x", cat="기타"), item("y", cat="기타")])
        self.assertEqual(shown, 0)
        self.assertEqual(blocks, ["*그 외 분야 2편 제외*"])


class KeywordClassifyTest(unittest.TestCase):
    def test_examples(self):
        cases = [
            ("A web agent for GUI tasks", "Agent"),
            ("Diffusion transformers for text-to-video", "생성형"),
            ("Open-vocabulary object detection", "CV"),
            ("Scaling test-time reasoning in LLMs", "LLM"),
            ("Protein folding with graph networks", "기타"),
            # 여러 분야에 걸치면 좁은 분야가 우선
            ("A multimodal agent that reads images", "Agent"),
            ("Visual reasoning benchmark for VLMs", "CV"),
        ]
        for title, want in cases:
            self.assertEqual(m.classify_by_keywords(item(title)), want, title)

    def test_uses_hf_keywords_too(self):
        self.assertEqual(m.classify_by_keywords(item("Something", keywords=["LLM"])), "LLM")


@mock.patch.object(m, "TRANSLATE", True)
@mock.patch.object(m, "ANTHROPIC_API_KEY", "test-key")
class EnrichTest(unittest.TestCase):
    def test_llm_translation_and_category_applied(self):
        items = [item("Paper A", "summary a"), item("Paper B", "summary b")]
        resp = anthropic_resp([{"ko": "요약 A", "cat": " CV "}, {"ko": "요약 B", "cat": "기타"}])
        with mock.patch.object(m.requests, "post", return_value=resp) as post:
            m.enrich(items)
        self.assertEqual([(p["one_liner"], p["cat"]) for p in items],
                         [("요약 A", "CV"), ("요약 B", "기타")])
        prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("제목: Paper A", prompt)

    def test_unknown_category_falls_back_to_keywords(self):
        items = [item("A web agent", "english")]
        resp = anthropic_resp([{"ko": "한글", "cat": "Robotics"}])
        with mock.patch.object(m.requests, "post", return_value=resp):
            m.enrich(items)
        self.assertEqual((items[0]["one_liner"], items[0]["cat"]), ("한글", "Agent"))

    def test_llm_failure_keeps_english_and_uses_keywords(self):
        items = [item("Diffusion for video generation", "english")]
        with mock.patch.object(m.requests, "post", side_effect=requests.ConnectionError("x")), \
                redirect_stderr(io.StringIO()):
            m.enrich(items)
        self.assertEqual((items[0]["one_liner"], items[0]["cat"]), ("english", "생성형"))

    def test_count_mismatch_falls_back(self):
        items = [item("LLM paper", "english"), item("Other", "english")]
        resp = anthropic_resp([{"ko": "하나만", "cat": "LLM"}])
        with mock.patch.object(m.requests, "post", return_value=resp), \
                redirect_stderr(io.StringIO()):
            m.enrich(items)
        self.assertEqual([p["one_liner"] for p in items], ["english", "english"])
        self.assertEqual([p["cat"] for p in items], ["LLM", "기타"])

    def test_batches_of_llm_batch_size(self):
        items = [item(f"LLM paper {i}", "english") for i in range(m.LLM_BATCH_SIZE + 5)]
        calls = []

        def post(url, headers=None, json=None, timeout=None):
            n = json["messages"][0]["content"].count("제목: ")
            calls.append(n)
            return anthropic_resp([{"ko": "한글", "cat": "LLM"}] * n)

        with mock.patch.object(m.requests, "post", post):
            m.enrich(items)
        self.assertEqual(calls, [m.LLM_BATCH_SIZE, 5])
        self.assertTrue(all(p["one_liner"] == "한글" for p in items))

    def test_no_api_key_skips_llm(self):
        items = [item("LLM paper", "english")]
        with mock.patch.object(m, "ANTHROPIC_API_KEY", ""), \
                mock.patch.object(m.requests, "post") as post, \
                redirect_stderr(io.StringIO()) as err:
            m.enrich(items)
        post.assert_not_called()
        self.assertEqual(items[0]["cat"], "LLM")
        self.assertIn("ANTHROPIC_API_KEY 없음", err.getvalue())


if __name__ == "__main__":
    unittest.main()
