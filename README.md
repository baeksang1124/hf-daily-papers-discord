# hf-daily-papers-discord

Hugging Face [Daily Papers](https://huggingface.co/papers)를 가져와 관심 분야(LLM·Agent·CV·생성형) 논문만 골라, 한 줄 요약을 한국어로 번역해 디스코드 웹훅으로 올리는 스크립트.

## 동작
1. Hugging Face Daily Papers API에서 대상 날짜의 논문을 가져온다. 기본값은 어제(KST)다. 오늘 목록은 아직 채워지는 중일 수 있어서 쓰지 않는다. 그날 논문이 없으면 아무것도 보내지 않고 끝낸다.
2. 각 논문의 `ai_summary`(없으면 abstract 앞부분)를 Claude Haiku로 번역하고, 같은 호출에서 분야를 하나 고른다. 20편 단위로 나눠 호출해 응답 잘림을 막는다.
3. 관심 분야 논문만 분야별로 묶고, 분야 안에서는 업보트 순으로 `제목(링크) + 업보트 + 한글 한 줄 + 키워드`를 디스코드 embed로 보낸다. 관심 분야 밖 논문은 마지막에 개수만 적는다. embed 글자 한도에 맞춰 여러 메시지로 나눈다.

번역·분류 호출이 실패하면 해당 묶음은 영어 원문으로 보내고, 분야는 키워드 규칙으로 정한다.

### 분야
| 분야 | 기준 |
|---|---|
| 🧠 LLM | 언어 모델의 학습·추론(reasoning)·정렬·효율화·평가, 텍스트 생성 |
| 🤖 Agent | 도구 사용, 계획, 웹·GUI·코드 에이전트, 멀티에이전트 |
| 👁️ CV | 이미지·영상·3D의 인식과 이해 (검출, 분할, 시각 질의응답 등) |
| 🎨 생성형 | 이미지·영상·오디오·3D 생성 (diffusion, flow matching 등). 텍스트 생성은 LLM |

여러 분야에 걸치는 논문은 핵심 기여 기준으로 한 분야에만 넣는다. 분야를 바꾸려면 `hf_daily_to_discord.py`의 `CATEGORIES`(Haiku 분류 기준)와 `_KEYWORD_RULES`(대체 규칙)를 고친다.

### 종료 코드
스케줄러가 실패를 알아챌 수 있도록, 실패하면 0이 아닌 값으로 끝난다.

| 코드 | 의미 |
|---|---|
| `0` | 전송 완료, 또는 그날 논문 없음 |
| `1` | HF 조회 실패(3회 시도 후), 디스코드 전송 실패(일부 메시지라도), `DISCORD_WEBHOOK_URL` 없음 |
| `2` | 인자 오류 (`--date` 형식 등) |

번역·분류 실패는 영어 원문과 키워드 분류로 대신 보내므로 실패로 치지 않는다.

## 설치
```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
```

| 환경변수 | 설명 |
|---|---|
| `DISCORD_WEBHOOK_URL` | (필수) 디스코드 채널 웹훅 URL |
| `ANTHROPIC_API_KEY` | 번역·분류용 키. 없으면 영어 원문 + 키워드 분류 |
| `HF_TRANSLATE` | `0`이면 번역·AI 분류를 건너뛰고 영어 원문 + 키워드 분류 (기본 `1`) |
| `HF_MODEL` | 번역·분류 모델 (기본 `claude-haiku-4-5-20251001`) |

## 사용
스크립트는 `.env`를 직접 읽지 않으므로 먼저 환경변수로 불러온다.
```bash
set -a; source .env; set +a

python hf_daily_to_discord.py                    # 어제(KST) 목록
python hf_daily_to_discord.py --date 2026-06-02  # 특정 날짜
python hf_daily_to_discord.py --dry-run          # 디스코드로 안 보내고 콘솔에만 출력
```

## GitHub Actions로 매일 실행
`.github/workflows/daily-digest.yml`이 매일 09:10 KST(00:10 UTC)에 **어제(KST) 날짜** 목록을 올린다.
HF 목록은 UTC 날짜 기준으로 하루 동안 채워지므로 다 찬 어제 목록을 쓰고, 주말처럼 목록이 빈 날은 게시하지 않는다(실행 상태를 저장하지 않아도 중복 게시 없음).
HF 조회나 디스코드 전송이 실패하면 종료 코드가 0이 아니므로 실행이 실패로 표시되고 GitHub 알림이 온다.

1. 저장소 Settings → Secrets and variables → Actions → **New repository secret**
   - `DISCORD_WEBHOOK_URL` (필수)
   - `ANTHROPIC_API_KEY` (번역·분야 분류용, 선택)
2. Actions 탭 → **HF Daily Papers digest** → **Run workflow**로 수동 실행. `date`로 날짜 지정, `dry_run`으로 전송 없이 로그만 확인 가능.

참고: GitHub 예약 실행은 부하에 따라 수 분~수십 분 늦게 시작될 수 있고, 저장소에 60일간 활동이 없으면 예약 워크플로가 자동 비활성화된다(Actions 탭에서 다시 켤 수 있음).

### 서버에서 매일 실행 (cron 예시)
기본값이 어제(KST) 목록이므로 하루 한 번 돌리면 같은 날짜를 두 번 보내거나 건너뛰지 않는다.
```cron
0 9 * * * cd /path/to/hf-daily-papers-discord && set -a && . ./.env && set +a && python hf_daily_to_discord.py >> digest.log 2>&1
```

## 테스트
네트워크 없이 가짜 응답으로 돈다. 추가 설치는 필요 없다.
```bash
python -m unittest -v
```
