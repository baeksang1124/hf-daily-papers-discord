# hf-daily-papers-discord

Hugging Face [Daily Papers](https://huggingface.co/papers)를 가져와 각 논문의 한 줄 요약을 한국어로 번역하고, 디스코드 웹훅으로 올리는 스크립트.

## 동작
1. Hugging Face Daily Papers API에서 대상 날짜의 논문을 가져온다. 기본값은 어제(KST)다. 오늘 목록은 아직 채워지는 중일 수 있어서 쓰지 않는다. 그날 논문이 없으면 아무것도 보내지 않고 끝낸다.
2. 각 논문의 `ai_summary`(없으면 abstract 앞부분)를 Claude Haiku로 일괄 번역한다. 20편 단위로 나눠 호출해 응답 잘림을 막는다.
3. `제목(링크) + 업보트 + 한글 한 줄 + 키워드`를 업보트 순으로 정렬해 디스코드 embed로 보낸다. embed 글자 한도에 맞춰 여러 메시지로 나눈다.

번역에 실패하면 해당 묶음만 영어 원문으로 보낸다.

### 종료 코드
스케줄러가 실패를 알아챌 수 있도록, 실패하면 0이 아닌 값으로 끝난다.

| 코드 | 의미 |
|---|---|
| `0` | 전송 완료, 또는 그날 논문 없음 |
| `1` | HF 조회 실패(3회 시도 후), 디스코드 전송 실패(일부 메시지라도), `DISCORD_WEBHOOK_URL` 없음 |
| `2` | 인자 오류 (`--date` 형식 등) |

번역 실패는 영어 원문으로 대신 보내므로 실패로 치지 않는다.

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

python hf_daily_to_discord.py                    # 어제(KST) 목록
python hf_daily_to_discord.py --date 2026-06-02  # 특정 날짜
python hf_daily_to_discord.py --dry-run          # 디스코드로 안 보내고 콘솔에만 출력
```

### 매일 자동 실행 (cron 예시)
기본값이 어제(KST) 목록이므로 하루 한 번 돌리면 같은 날짜를 두 번 보내거나 건너뛰지 않는다.
```cron
0 9 * * * cd /path/to/hf-daily-papers-discord && set -a && . ./.env && set +a && python hf_daily_to_discord.py >> digest.log 2>&1
```

## 테스트
네트워크 없이 가짜 응답으로 돈다. 추가 설치는 필요 없다.
```bash
python -m unittest -v
```
