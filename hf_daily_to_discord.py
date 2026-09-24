#!/usr/bin/env python3
"""
HF Daily Papers -> 분야 분류 + 한 줄 요약(한글 번역) -> Discord 웹훅 전송

동작:
  1) Hugging Face Daily Papers API에서 대상 날짜(기본: 어제 KST)의 논문을 통째로 가져온다.
  2) 각 논문의 ai_summary(한 줄 요약)를 한글로 번역하고 분야(LLM/Agent/CV/생성형/기타)를
     고른다(가벼운 Haiku 호출). 호출을 못 하거나 실패하면 영어 원문 + 키워드 규칙으로 분류.
  3) 관심 분야 논문만 분야별로 묶어 "제목 + 한글 한 줄 + 키워드 + 업보트 + 링크"로
     디스코드 웹훅에 보낸다. 기타 분야는 개수만 적는다.

환경변수:
  DISCORD_WEBHOOK_URL   (필수) 디스코드 채널 웹훅 URL
  ANTHROPIC_API_KEY     번역·AI 분류에 필요. 없으면 영어 원문 + 키워드 분류
  HF_TRANSLATE          기본 "1". "0"이면 번역·AI 분류를 건너뛰고 영어 원문 + 키워드 분류
  HF_MODEL              번역·분류 모델. 기본 claude-haiku-4-5-20251001

종료 코드:
  0  전송 완료, 또는 대상 날짜에 논문이 없음
  1  HF 조회 실패, 디스코드 전송 실패(일부라도), DISCORD_WEBHOOK_URL 없음
  2  인자 오류 (--date 형식 등)

사용:
  python hf_daily_to_discord.py                # 어제(KST) 목록
  python hf_daily_to_discord.py --date 2026-06-02
  python hf_daily_to_discord.py --dry-run      # 디스코드로 안 보내고 콘솔에만 출력
"""

import os
import re
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
# HF 조회 시도 횟수. 일시적인 네트워크 오류 한 번으로 하루치를 놓치지 않기 위함.
FETCH_ATTEMPTS = 3

# 관심 분야: (키, 디스코드 섹션 제목, 분류 기준). 이 순서대로 디스코드에 나온다.
# 분류 기준 문구는 그대로 Haiku 프롬프트에 들어간다. 분야를 바꾸려면 여기와 _KEYWORD_RULES를 고친다.
CATEGORIES = [
    ("LLM", "🧠 LLM", "언어 모델의 학습·추론(reasoning)·정렬·효율화·평가, 텍스트 생성"),
    ("Agent", "🤖 Agent", "도구 사용, 계획, 웹·GUI·코드 에이전트, 멀티에이전트"),
    ("CV", "👁️ CV", "이미지·영상·3D의 인식과 이해 (검출, 분할, 시각 질의응답 등)"),
    ("생성형", "🎨 생성형", "이미지·영상·오디오·3D 생성 (diffusion, flow matching 등)"),
]
OTHER = "기타"
_INTEREST_KEYS = {key for key, _, _ in CATEGORIES}


class FetchError(Exception):
    """HF 조회 실패. '그날 논문 없음'(빈 리스트)과 구분하기 위해 따로 둔다."""


def fetch_daily(date_str):
    """특정 날짜의 데일리 논문 목록. 논문 없는 날은 빈 리스트, 조회 실패는 FetchError."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            r = requests.get(HF_API, params={"date": date_str}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[warn] fetch 실패 ({date_str}, {attempt}/{FETCH_ATTEMPTS}): {e}", file=sys.stderr)
            if attempt == FETCH_ATTEMPTS:
                raise FetchError(f"{date_str} 조회 실패: {e}") from e
            time.sleep(2 ** attempt)
            continue
        if not isinstance(data, list):
            raise FetchError(f"{date_str} 응답이 리스트가 아님: {str(data)[:200]}")
        return data


def default_date(now=None):
    """기본 대상 날짜 = 어제(KST). 오늘 목록은 아직 채워지는 중일 수 있어서 쓰지 않는다."""
    now = now or dt.datetime.now(KST)
    return (now.astimezone(KST).date() - dt.timedelta(days=1)).isoformat()


def parse_date(s):
    """--date 인자 검증. YYYY-MM-DD만 받는다."""
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"YYYY-MM-DD 형식이어야 합니다: {s!r}")


def _clean(text):
    """줄바꿈·연속 공백을 공백 하나로. 섞여 있으면 디스코드 블록 모양이 깨진다."""
    return " ".join((text or "").split())


def extract(papers):
    """필요한 필드만 뽑고 업보트 내림차순 정렬."""
    out = []
    for item in papers:
        paper = item.get("paper", {}) or {}
        pid = paper.get("id") or item.get("id") or ""
        title = _clean(item.get("title") or paper.get("title"))
        one_liner = _clean(paper.get("ai_summary"))
        if not one_liner:
            # ai_summary 없으면 abstract 앞부분으로 대체
            abstract = _clean(item.get("summary") or paper.get("summary"))
            one_liner = (abstract[:300] + "…") if len(abstract) > 300 else abstract
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


# LLM 분류를 못 쓸 때의 대체 규칙. 제목·요약·HF 키워드에서 찾고, 위에 있는 규칙이 우선한다.
# 에이전트·생성처럼 좁은 분야를 먼저 보고, 범위가 넓은 LLM을 마지막에 본다.
_KEYWORD_RULES = [
    (cat, re.compile(pattern, re.IGNORECASE)) for cat, pattern in [
        ("Agent", r"\bagent(s|ic)?\b|multi-agent|tool[- ]use|\bgui\b|web navigation"),
        ("생성형", r"diffusion|flow matching|text-to-(image|video|audio|speech|3d|music)"
                 r"|\b(image|video|audio|music|3d|speech) (generation|synthesis|editing)"),
        ("CV", r"\bvision\b|visual|\bimages?\b|\bvideos?\b|segmentation|object detection"
               r"|\b3d\b|point cloud|\bvlms?\b"),
        ("LLM", r"\bllms?\b|language models?|reasoning|\brlhf\b|instruction[- ]tun"
                r"|chain[- ]of[- ]thought|tokeniz"),
    ]
]


def classify_by_keywords(p):
    """키워드 규칙으로 분야 하나를 고른다. 영어 원문 기준."""
    text = " ".join([p["title"], p["one_liner"], " ".join(map(str, p["keywords"]))])
    for cat, pattern in _KEYWORD_RULES:
        if pattern.search(text):
            return cat
    return OTHER


# 한 번 호출에 처리할 최대 논문 수. 너무 크면 응답이 max_tokens에서 잘려 JSON 파싱 실패.
LLM_BATCH_SIZE = 20


def _llm_prompt(items):
    cats = "\n".join(f"   - {key}: {desc}" for key, _, desc in CATEGORIES)
    numbered = "\n".join(
        f"{i+1}. 제목: {p['title']}\n   요약: {p['one_liner']}" for i, p in enumerate(items)
    )
    return (
        "다음은 AI 논문의 제목과 한 줄 요약(영어)이다. 각 논문마다 두 가지를 하라.\n"
        "1) 요약을 자연스러운 한국어로 번역한다. 전문 용어(LoRA, RLHF 등)는 굳이 풀어쓰지 말고 그대로 둔다.\n"
        "2) 아래 분야 중 하나를 고른다.\n"
        f"{cats}\n"
        f"   - {OTHER}: 위 어디에도 뚜렷하게 해당하지 않음\n"
        "   여러 분야에 걸치면 논문의 핵심 기여 하나만 고른다. "
        "예: LLM 기반 에이전트 → Agent, 텍스트로 이미지를 만드는 모델 → 생성형.\n"
        "설명 없이 JSON 배열만 출력한다. 순서와 개수는 입력과 동일해야 한다.\n"
        f'예: [{{"ko": "번역1", "cat": "{CATEGORIES[0][0]}"}}, {{"ko": "번역2", "cat": "{OTHER}"}}]\n\n'
        f"{numbered}"
    )


def _call_llm(items):
    """번역 + 분야 분류를 1회 호출로. [{"ko": ..., "cat": ...}, ...], 실패하면 None."""
    fallback = "이 묶음은 영어 원문 + 키워드 분류"
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
                "messages": [{"role": "user", "content": _llm_prompt(items)}],
            },
            timeout=60,
        )
        r.raise_for_status()
        resp = r.json()
        if resp.get("stop_reason") == "max_tokens":
            print(f"[warn] 응답이 max_tokens에서 잘림 → {fallback}", file=sys.stderr)
            return None
        text = "".join(
            b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"
        ).strip()
        text = text.replace("```json", "").replace("```", "").strip()
        out = json.loads(text)
        if isinstance(out, list) and len(out) == len(items):
            return [x if isinstance(x, dict) else {} for x in out]
        print(f"[warn] 응답 개수 불일치 → {fallback}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] 번역·분류 실패 → {fallback}: {e}", file=sys.stderr)
    return None


def enrich(items):
    """각 논문의 one_liner를 한글로 바꾸고 cat(분야)을 붙인다.
    LLM을 못 쓰거나 실패한 논문은 영어 원문을 두고 키워드 규칙으로 분류한다."""
    use_llm = TRANSLATE and bool(ANTHROPIC_API_KEY)
    if TRANSLATE and not ANTHROPIC_API_KEY:
        print("[warn] ANTHROPIC_API_KEY 없음 → 번역 생략, 키워드로 분류", file=sys.stderr)
    for i in range(0, len(items), LLM_BATCH_SIZE):
        chunk = items[i:i + LLM_BATCH_SIZE]
        results = (_call_llm(chunk) if use_llm else None) or [{}] * len(chunk)
        for p, r in zip(chunk, results):
            cat = str(r.get("cat") or "").strip()
            # 키워드 분류는 영어 원문으로 해야 하므로 번역 반영보다 먼저 한다.
            p["cat"] = cat if cat in _INTEREST_KEYS or cat == OTHER else classify_by_keywords(p)
            ko = r.get("ko")
            if isinstance(ko, str) and ko.strip():
                p["one_liner"] = ko.strip()


# 디스코드 마크다운에서 서식 기호로 해석되는 문자. 앞에 \ 를 붙여 글자 그대로 보이게 한다.
_MD_SPECIAL = re.compile(r"([\\*_~`|])")


def md_escape(text):
    return _MD_SPECIAL.sub(r"\\\1", text)


def link_text(text):
    """[제목](url)의 제목 부분. 대괄호는 링크 문법을 깨뜨리므로 소괄호로 바꾼다."""
    return md_escape(text.replace("[", "(").replace("]", ")"))


def build_blocks(items):
    """논문 1편 = 텍스트 블록 하나."""
    blocks = []
    for p in items:
        head = (f"**[{link_text(p['title'])}]({p['url']})**" if p["url"]
                else f"**{md_escape(p['title'])}**")
        lines = [f"{head}  ▲{p['upvotes']}"]
        if p["one_liner"]:
            lines.append(md_escape(p["one_liner"]))
        # 키워드는 인라인 코드(`...`) 안에 들어가므로 백틱만 빼면 된다.
        keywords = [str(k).replace("`", "") for k in p["keywords"] if k]
        if keywords:
            lines.append("`" + "` `".join(keywords) + "`")
        blocks.append("\n".join(lines))
    return blocks


def build_sections(items):
    """관심 분야별로 묶은 블록 목록과 관심 분야 논문 수. 기타 분야는 마지막에 개수만 적는다."""
    blocks, shown = [], 0
    for key, label, _ in CATEGORIES:
        group = [p for p in items if p.get("cat") == key]
        if not group:
            continue
        shown += len(group)
        paper_blocks = build_blocks(group)
        # 섹션 제목이 청크 끝에 홀로 남지 않도록 첫 논문 블록에 붙인다.
        paper_blocks[0] = f"__**{label} · {len(group)}편**__\n{paper_blocks[0]}"
        blocks.extend(paper_blocks)
    excluded = len(items) - shown
    if excluded:
        blocks.append(f"*그 외 분야 {excluded}편 제외*")
    return blocks, shown


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


def _retry_after(resp):
    """429 응답이 알려준 대기 시간(초). 본문이 JSON이 아니면 1초."""
    try:
        return float(resp.json().get("retry_after", 1))
    except (ValueError, AttributeError, TypeError):
        return 1.0


def _send(payload):
    """웹훅 1회 전송. 429면 안내받은 시간만큼 기다렸다가 한 번 더. 성공 여부 반환."""
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:  # rate limit
            time.sleep(_retry_after(resp) + 0.5)
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        # 예외 메시지에는 웹훅 URL(토큰 포함)이 들어가므로 종류만 남긴다.
        print(f"[error] discord 전송 오류: {type(e).__name__}", file=sys.stderr)
        return False
    if resp.status_code >= 300:
        print(f"[error] discord {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return False
    return True


def post_discord(date_str, shown, total, chunks, dry_run=False):
    """청크를 embed 메시지로 차례로 보낸다. 실패한 메시지 수를 돌려준다."""
    n = len(chunks)
    failed = 0
    for i, desc in enumerate(chunks, 1):
        title = f"🤗 HF Daily Papers — {date_str} (관심 분야 {shown}편 / 전체 {total}편)"
        if n > 1:
            title += f"  · {i}/{n}"
        payload = {"embeds": [{"title": title, "description": desc, "color": 0xFFD21E}]}
        if dry_run:
            print(f"\n===== embed {i}/{n} =====\n{title}\n{desc}")
            continue
        if not _send(payload):
            failed += 1
        time.sleep(0.7)  # 웹훅 rate limit 여유
    return failed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=parse_date, help="YYYY-MM-DD (미지정 시 어제 KST)")
    ap.add_argument("--dry-run", action="store_true", help="디스코드 미전송, 콘솔 출력만")
    args = ap.parse_args(argv)

    if not args.dry_run and not DISCORD_WEBHOOK_URL:
        print("[error] DISCORD_WEBHOOK_URL 환경변수가 필요합니다.", file=sys.stderr)
        return 1

    date_str = args.date or default_date()
    try:
        papers = fetch_daily(date_str)
    except FetchError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    items = extract(papers)
    if not items:
        print(f"[info] {date_str}: 논문 없음. 보내지 않고 종료.")
        return 0

    enrich(items)
    blocks, shown = build_sections(items)
    chunks = chunk_blocks(blocks)
    failed = post_discord(date_str, shown, len(items), chunks, dry_run=args.dry_run)
    if failed:
        print(f"[error] {date_str}: 메시지 {len(chunks)}개 중 {failed}개 전송 실패", file=sys.stderr)
        return 1
    print(f"[done] {date_str}: 관심 분야 {shown}편 / 전체 {len(items)}편 ({len(chunks)}개 메시지)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
