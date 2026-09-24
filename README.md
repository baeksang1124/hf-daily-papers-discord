# hf-daily-papers-discord

Hugging Face [Daily Papers](https://huggingface.co/papers)를 가져와 각 논문의 한 줄 요약을 한국어로 번역하고, 디스코드 웹훅으로 올리는 스크립트.

## 동작
1. Hugging Face Daily Papers API에서 그날치 논문을 가져온다. 오늘(KST) 목록이 비어 있으면 최대 3일 전까지 거슬러 올라간다.
2. 각 논문의 `ai_summary`(없으면 abstract 앞부분)를 Claude Haiku 한 번 호출로 일괄 번역한다. 20편 단위로 나눠 호출해 응답 잘림을 막는다.
3. `제목(링크) + 업보트 + 한글 한 줄 + 키워드`를 업보트 순으로 정렬해 디스코드 embed로 보낸다. embed 글자 한도에 맞춰 여러 메시지로 나눈다.

번역에 실패하면 해당 묶음만 영어 원문으로 보낸다.

## 설치
```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
```

| 환경변수 | 설명 |
|---|---|
| `DISCORD_WEBHOOK_URL` | (필수) 디스코드 채널 웹훅 URL |
| `ANTHROPIC_API_KEY` | 번역용 키. 없으면 영어 원문 전송 |
| `HF_TRANSLATE` | `0`이면 번역 건너뜀 (기본 `1`) |
| `HF_MODEL` | 번역 모델 (기본 `claude-haiku-4-5-20251001`) |

## 사용
스크립트는 `.env`를 직접 읽지 않으므로 먼저 환경변수로 불러온다.
```bash
set -a; source .env; set +a

python hf_daily_to_discord.py                    # 오늘(KST) 기준 최신
python hf_daily_to_discord.py --date 2026-06-02  # 특정 날짜
python hf_daily_to_discord.py --dry-run          # 디스코드로 안 보내고 콘솔에만 출력
```

## GitHub Actions로 매일 실행
`.github/workflows/daily-digest.yml`이 매일 09:10 KST(00:10 UTC)에 **어제(KST) 날짜** 목록을 올린다.
HF 목록은 UTC 날짜 기준으로 하루 동안 채워지므로 다 찬 어제 목록을 쓰고, 주말처럼 목록이 빈 날은 게시하지 않는다(실행 상태를 저장하지 않아도 중복 게시 없음).

1. 저장소 Settings → Secrets and variables → Actions → **New repository secret**
   - `DISCORD_WEBHOOK_URL` (필수)
   - `ANTHROPIC_API_KEY` (번역용, 선택)
2. Actions 탭 → **HF Daily Papers digest** → **Run workflow**로 수동 실행. `date`로 날짜 지정, `dry_run`으로 전송 없이 로그만 확인 가능.

참고: GitHub 예약 실행은 부하에 따라 수 분~수십 분 늦게 시작될 수 있고, 저장소에 60일간 활동이 없으면 예약 워크플로가 자동 비활성화된다(Actions 탭에서 다시 켤 수 있음).

### 서버에서 매일 실행 (cron 예시)
HF 목록은 미국 시간 기준으로 올라오므로 KST 오전에 돌리면 보통 전날 목록이 잡힌다.
```cron
0 9 * * * cd /path/to/hf-daily-papers-discord && set -a && . ./.env && set +a && python hf_daily_to_discord.py >> digest.log 2>&1
```
