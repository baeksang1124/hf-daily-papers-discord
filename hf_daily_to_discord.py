#!/usr/bin/env python3
"""
HF Daily Papers -> 한 줄 요약(한글 번역) -> Discord 웹훅 전송

동작:
  1) Hugging Face Daily Papers API에서 그날치 논문을 통째로 가져온다.
  2) 각 논문의 ai_summary(한 줄 요약)를 한글로 일괄 번역한다(가벼운 Haiku 1회 호출).
  3) "제목 + 한글 한 줄 + 키워드 + 업보트 + 링크"로 압축해 디스코드 웹훅으로 보낸다.

환경변수:
  DISCORD_WEBHOOK_URL   (필수) 디스코드 채널 웹훅 URL
  ANTHROPIC_API_KEY     (번역 켤 때 필요) 없으면 영어 원문 그대로 전송
  HF_TRANSLATE          기본 "1". "0"이면 번역 건너뛰고 영어 그대로
  HF_MODEL              번역 모델. 기본 claude-haiku-4-5-20251001

사용:
  python hf_daily_to_discord.py                # 오늘(KST) 기준, 비어있으면 하루씩 뒤로
  python hf_daily_to_discord.py --date 2026-06-02
  python hf_daily_to_discord.py --dry-run      # 디스코드로 안 보내고 콘솔에만 출력
"""

import os
import sys
import json
import time
import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import requests

HF_API = "https://huggingface.co/api/daily_papers"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
KST = ZoneInfo("Asia/Seoul")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TRANSLATE = os.environ.get("HF_TRANSLATE", "1") != "0"
MODEL = os.environ.get("HF_MODEL", "claude-haiku-4-5-20251001")

# 디스코드 embed description 한도는 4096자. 여유를 두고 청크 분할.
CHUNK_CHAR_LIMIT = 3800
REQUEST_TIMEOUT = 30


def fetch_daily(date_str):
    """특정 날짜의 데일리 논문 목록을 가져온다. 실패/빈값이면 빈 리스트."""
    try:
        r = requests.get(HF_API, params={"date": date_str}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[warn] fetch 실패 ({date_str}): {e}", file=sys.stderr)
        return []


def pick_latest(date_override=None):
    """date_override가 있으면 그날만. 없으면 오늘(KST)부터 최대 3일 뒤로 가며 첫 비어있지 않은 날 사용."""
    if date_override:
        return date_override, fetch_daily(date_override)
    today = dt.datetime.now(KST).date()
    for back in range(0, 3):
        d = (today - dt.timedelta(days=back)).isoformat()
        papers = fetch_daily(d)
        if papers:
            return d, papers
    return today.isoformat(), []


def extract(papers):
    """필요한 필드만 뽑고 업보트 내림차순 정렬."""
    out = []
    for item in papers:
        paper = item.get("paper", {}) or {}
        pid = paper.get("id") or item.get("id") or ""
        title = (item.get("title") or paper.get("title") or "").strip()
        one_liner = (paper.get("ai_summary") or "").strip()
        if not one_liner:
            # ai_summary 없으면 abstract 앞부분으로 대체
            abs = (item.get("summary") or paper.get("summary") or "").strip()
            one_liner = (abs[:300] + "…") if len(abs) > 300 else abs
        keywords = paper.get("ai_keywords") or []
        upvotes = paper.get("upvotes", item.get("upvotes", 0)) or 0
        out.append({
            "id": pid,
            "title": title,
            "one_liner": one_liner,
            "keywords": keywords[:5],
            "upvotes": int(upvotes),
            "url": f"https://huggingface.co/papers/{pid}" if pid else "",
        })
    out.sort(key=lambda x: x["upvotes"], reverse=True)
    return out


# 한 번 호출에 번역할 최대 항목 수. 너무 크면 응답이 max_tokens에서 잘려 JSON 파싱 실패.
TRANSLATE_BATCH_SIZE = 20


def _translate_chunk(texts):
    """항목 리스트를 1회 호출로 번역. 실패 시 그 묶음만 영어 원문 반환."""
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "다음은 AI 논문 한 줄 요약(영어)들이다. 각 항목을 자연스러운 한국어로 번역하라.\n"
        "전문 용어(LoRA, RLHF 등)는 굳이 풀어쓰지 말고 그대로 둔다.\n"
        "설명 없이 JSON 배열만 출력한다. 순서와 개수는 입력과 동일해야 한다.\n"
        '예: ["번역1", "번역2"]\n\n'
        f"{numbered}"
    )
    try:
        r = requests.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        resp = r.json()
        if resp.get("stop_reason") == "max_tokens":
            print("[warn] 응답이 max_tokens에서 잘림 → 이 묶음 영어 원문 사용", file=sys.stderr)
            return texts
        text = "".join(
            b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"
        ).strip()
        text = text.replace("```json", "").replace("```", "").strip()
        ko = json.loads(text)
        if isinstance(ko, list) and len(ko) == len(texts):
            return [str(x) for x in ko]
        print("[warn] 번역 개수 불일치 → 영어 원문 사용", file=sys.stderr)
    except Exception as e:
        print(f"[warn] 번역 실패 → 영어 원문 사용: {e}", file=sys.stderr)
    return texts


def translate_batch(texts):
    """영어 한 줄 요약 리스트를 한글 번역. 큰 목록은 잘림 방지를 위해 나눠서 호출."""
    if not TRANSLATE or not ANTHROPIC_API_KEY or not texts:
        return texts
    out = []
    for i in range(0, len(texts), TRANSLATE_BATCH_SIZE):
        out.extend(_translate_chunk(texts[i:i + TRANSLATE_BATCH_SIZE]))
    return out


def build_blocks(items):
    """논문 1편 = 텍스트 블록 하나."""
    blocks = []
    for p in items:
        lines = [f"**[{p['title']}]({p['url']})**  ▲{p['upvotes']}" if p["url"]
                 else f"**{p['title']}**  ▲{p['upvotes']}"]
        if p["one_liner"]:
            lines.append(p["one_liner"])
        if p["keywords"]:
            lines.append("`" + "` `".join(p["keywords"]) + "`")
        blocks.append("\n".join(lines))
    return blocks


def chunk_blocks(blocks):
    """디스코드 한도에 맞게 블록들을 청크로 묶는다."""
    chunks, cur, cur_len = [], [], 0
    for b in blocks:
        add = len(b) + 2
        if cur and cur_len + add > CHUNK_CHAR_LIMIT:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += add
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def post_discord(date_str, total, chunks, dry_run=False):
    n = len(chunks)
    for i, desc in enumerate(chunks, 1):
        title = f"🤗 HF Daily Papers — {date_str} (총 {total}편)"
        if n > 1:
            title += f"  · {i}/{n}"
        payload = {"embeds": [{"title": title, "description": desc, "color": 0xFFD21E}]}
        if dry_run:
            print(f"\n===== embed {i}/{n} =====\n{title}\n{desc}")
            continue
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:  # rate limit
            retry = resp.json().get("retry_after", 1)
            time.sleep(float(retry) + 0.5)
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        elif resp.status_code >= 300:
            print(f"[error] discord {resp.status_code}: {resp.text}", file=sys.stderr)
        time.sleep(0.7)  # 웹훅 rate limit 여유


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (미지정 시 오늘 KST 기준)")
    ap.add_argument("--dry-run", action="store_true", help="디스코드 미전송, 콘솔 출력만")
    args = ap.parse_args()

    if not args.dry_run and not DISCORD_WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL 환경변수가 필요합니다.")

    date_str, papers = pick_latest(args.date)
    items = extract(papers)
    if not items:
        print(f"[info] {date_str}: 논문 없음. 종료.")
        return

    ko = translate_batch([p["one_liner"] for p in items])
    for p, k in zip(items, ko):
        p["one_liner"] = k

    chunks = chunk_blocks(build_blocks(items))
    post_discord(date_str, len(items), chunks, dry_run=args.dry_run)
    print(f"[done] {date_str}: {len(items)}편 전송 ({len(chunks)}개 메시지)")


if __name__ == "__main__":
    main()
