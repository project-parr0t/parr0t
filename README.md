# Agent Game Translation Framework

`main`은 특정 게임 엔진에 의존하지 않는 중앙 제어 프레임워크입니다. 게임 전체 맥락과 캐릭터 말투를 먼저 분석한 뒤, 번역 AI와 독립 검수 AI를 폴백 체인으로 운용합니다. 최상위 오케스트레이터는 의미를 직접 판단하지 않고 각 에이전트가 정상 완료했는지, 거부·오류·형식 위반이 있었는지, 결정론적 무결성 검사를 통과했는지만 확인합니다.

엔진별 SDK는 `sdk-<engine>-<version>` 브랜치가 소유합니다. 해당 브랜치는 `main`의 오케스트레이션·프로바이더·상태 머신을 상속하고, 엔진 고유의 추출·적용·패킹·검증 구현과 그 엔진에만 해당하는 문서·FAQ·합성 테스트를 추가합니다. `main`은 SDK 브랜치의 구현이나 주의사항을 역으로 포함하지 않습니다.

정확한 소유 범위와 병합 규칙은 [코어/엔진 SDK 브랜치 계약](docs/sdk-branch-contract.md)을 따릅니다.

`sdk-rpg-maker-mv-1.6.1` 브랜치는 RPG Maker MV 1.6.1용 읽기 전용 추출, manifest 기반 적용, 구조 검증, 결정론적 ZIP 패킹을 추가합니다. 사용 절차와 엔진 고유 무결성 규칙은 [RPG Maker MV 1.6.1 어댑터 문서](docs/rpg-maker-mv-1.6.1.md)를 참고하십시오.

## 핵심 동작

```text
전체 스크립트
  → 컨텍스트 분석 AI 체인
     (스토리/장면/관계 변화/캐릭터 말투 규칙)
  → 번역 AI 체인
     (거부·오류·형식 실패 시 다음 모델)
  → 플레이스홀더/태그/스크립트 침범 검사
  → 독립 compact judge 체인
     (짧은 ok/rewrite 판정, 거부·오류 시 다음 judge)
  → 문제가 감지된 항목만 rewriter 체인
     (수정 후 결정론 검사와 compact judge 재확인)
  → 최종 무결성 검사
  → 에이전트 작업 디렉터리에만 결과 저장
```

- 컨텍스트 분석이 전부 거부되거나 실패하면 번역을 시작하지 않습니다.
- 번역기와 검수기는 서로 다른 제공자/모델 체인을 쓸 수 있습니다.
- 허구의 폭력·욕설·공포·로맨스·성인 등급 요소를 이유로 임의 순화/거부하지 않도록 기존 문장의 언어 변환이라는 작업 성격을 명시합니다. 기본 프리셋에서는 거부 항목만 기록해 1차 단계 완료 후 다음 제공자로 넘깁니다.
- 검수기는 정상 후보를 다시 출력하지 않고 짧은 판정만 반환합니다. 문제가 감지된 항목만 재작성하므로 출력 토큰과 불필요한 변형을 줄입니다.
- judge가 거부하거나 잘못된 결과를 내면 다음 judge로 라우팅하며, rewriter도 독립 체인으로 교체할 수 있습니다.
- `{name}`, `%s`, `<tag>`, `\\n`, 엔진 제어 코드와 사전 지정어를 결정론적으로 검사합니다.
- 원문에 없던 `<script>`, `javascript:`, `eval(` 등의 삽입을 차단합니다.
- 이전에 승인된 동일 화자의 번역을 후속 대사의 스타일 앵커로 제공합니다.
- 동일 원문·컨텍스트·사전·모델·설정은 콘텐츠 해시 캐시로 다시 처리하지 않습니다.
- unpack/pack도 입력과 도구 지문이 같으면 다시 실행하지 않습니다.
- 기본 `openai-deepseek` 프리셋은 문장별 GPT-6 Luna 번역을 먼저 모두 수행한 뒤, 거부·오류 문장만 DeepSeek으로 넘깁니다. 이후 GPT-6.1 Sol 검수 전체를 마치고 검수 실패 항목만 DeepSeek으로 넘깁니다.
- 모든 허용 경로 또는 DeepSeek 쿼타가 소진되면 해당 항목은 `source_preserved` 상태로 정확한 원문을 유지합니다. 다른 문장의 진행은 중단하지 않습니다.
- DeepSeek 번역·재작성 요청은 JSON Object 출력과 원문 길이 기반 출력 상한을 사용하고, compact judge는 검증된 plain verdict 모드에서 최대 24토큰만 사용합니다. `thinking=disabled`와 `reasoning_effort=none`으로 불필요한 추론을 끕니다. 비사고 모드에서 효과가 없는 `temperature`는 최적화 수단으로 사용하지 않습니다. 공급자 안전장치를 비활성화하는 옵션으로 가장하지 않으며, 실제 거부는 문장별 폴백으로 격리합니다.
- 명시적 거절은 바로 다음 경로로 보내고, 형식 오류·일시적 공급자 오류·속도 제한·타임아웃·무결성 실패만 설정된 grace retry를 수행합니다.
- 번역 후보가 바뀔 때마다 세대와 해시가 갱신되므로, 이전 후보를 본 검수 결과를 재사용하지 않습니다.
- 상위 오케스트레이터에는 건별 상태·범주 코드·개수만 노출합니다. 원문, 번역문, 후보문, 공급자가 작성한 거부 설명은 비공개 데이터 평면에만 남고 감사 로그나 표준 출력으로 전달되지 않습니다.

## 데이터 격리 원칙

SDK 저장소에는 게임 데이터와 번역 데이터가 들어가지 않습니다. 실제 작업은 반드시 저장소 밖에 만듭니다.

```powershell
python -m pip install -e .
game-l10n init-workspace C:\agent-work\my-game --sdk-root C:\src\agent-game-translation-sdk
```

권장 운영 구성은 Codex App의 네이티브 번역·판정 에이전트와 독립 API 폴백을 조합하는 방식입니다. CLI만 사용하는 환경에서는 새 작업공간의 기본 `openai-deepseek` API 프리셋을 바로 사용할 수 있습니다. 어느 구성이든 역할별 공급자를 교체할 수 있으며 특정 회사나 모델은 코어 의존성이 아닙니다. 자세한 구조는 [공급자 중립 리뷰 파이프라인](docs/review-pipeline.md), [API + API 폴백 라우팅](docs/fallback-routing.md), [Codex App + DeepSeek API](docs/codex-app-deepseek.md)를 참고하십시오.

생성되는 작업공간의 `game-data/`, `inputs/`, `translations/`, `.game-l10n/`에는 원문·결과·캐시·감사 로그가 남습니다. SDK 저장소의 `.gitignore`, `AGENTS.md`, 개인정보 검사 스크립트가 이 경계를 보조합니다.

Codex Work에서는 SDK 저장소와 게임 폴더, 외부 에이전트 작업공간이 모두 허용된 프로젝트 범위 안에 있어야 합니다. `init-workspace`에 `--game-root`를 지정하면 원본은 읽기 전용 입력으로 취급됩니다. 권장 배치는 [docs/codex-work.md](docs/codex-work.md)를 참고하십시오. 저장소의 `AGENTS.md`와 번들 `game-localization` 스킬 덕분에 Codex/Sol이 저장소를 열고 절차를 바로 판별할 수 있습니다.

## Git 승인 흐름

초기에 에이전트가 사용자에게 Git 사용 여부를 물은 뒤 답을 기록합니다. 이 명령은 Git을 초기화하거나 커밋하지 않습니다.

```powershell
game-l10n policy --workspace C:\agent-work\my-game --git-choice enabled
# 또는 거부 시: --git-choice disabled
```

번역·패치 성공을 사용자가 명시적으로 확인한 뒤에만 재사용 가능한 도구를 정제합니다.

```powershell
game-l10n promote-sdk --workspace C:\agent-work\my-game `
  --engine fixture-engine --version 1.0 --user-confirmed-success
```

이 단계는 작업공간의 `sdk-export/sdk-fixture-engine-1.0/`에 `tools/`와 해시 manifest만 만듭니다. 원문과 번역문은 복사하지 않습니다. 그 후 에이전트가 커밋/푸시 여부를 다시 묻고 승인받아야 합니다.

```powershell
game-l10n policy --workspace C:\agent-work\my-game --publish-approved yes
game-l10n publish-sdk --workspace C:\agent-work\my-game `
  --sdk-root C:\src\agent-game-translation-sdk `
  --profile C:\agent-work\my-game\sdk-export\sdk-fixture-engine-1.0 `
  --commit --push
```

`publish-sdk`는 깨끗한 `main`에서 해당 `sdk-<engine>-<version>` 브랜치를 만들며, 저장소가 없을 때 임의로 `git init`하지 않습니다.

## 전체 맥락 번역

입력은 순서가 보존된 JSON, JSONL 또는 CSV이며 최소 필드는 `id`, `source`입니다. `scene`, `speaker`, `previous`, `next`, `metadata`를 함께 줄 수 있습니다. 실제 데이터 파일은 작업공간에만 두십시오.

전체 대본 중 일부만 번역하려면 번역 대상과 전체 컨텍스트를 분리합니다.

```powershell
game-l10n translate --workspace C:\agent-work\my-game `
  --input inputs\chapter-3.json `
  --context-input inputs\full-script.json `
  --output translations\chapter-3.ko.json
```

컨텍스트 패스는 전체 대본을 순서대로 장면 단위 청크로 분석해 다음 정보를 만듭니다.

- 전체 스토리와 장면별 사건/톤
- 관계 변화 타임라인
- 캐릭터 성향, 격식 수준, 호칭, 말끝, 문장 길이와 리듬
- 각 판단의 근거 대사 ID
- 전역 스타일 규칙

예를 들어 `Hello, Darwin.`을 단독 예문으로 번역하지 않습니다. 무뚝뚝한 집사라는 전체 흐름상의 프로필, 다윈과의 관계, 현재 장면의 긴장도, 앞선 검수 완료 대사를 함께 제공해 격식 있는 어조를 선택하고 검수기도 같은 기준으로 확인합니다.

## 설정

`init-workspace`가 만드는 `game-l10n.json`에서 제공자와 경로를 설정합니다. API 키는 값이 아니라 환경변수 이름만 기록합니다.

```json
{
  "providers": [
    {"name": "openai-luna", "type": "openai_responses", "model": "gpt-6-luna", "api_key_env": "OPENAI_API_KEY", "reasoning_effort": "none"},
    {"name": "deepseek", "type": "compatible_chat", "model": "deepseek-flash", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "response_format": "json_object", "thinking": "disabled", "reasoning_effort": "none", "review_verdict_format": "plain", "task_max_output_tokens": {"game_translation_review_judge": 24}},
    {"name": "openai-sol", "type": "openai_responses", "model": "gpt-6.1-sol", "api_key_env": "OPENAI_API_KEY", "reasoning_effort": "low", "task_max_output_tokens": {"game_translation_review_judge": 512, "game_translation_review_rewrite": 2048}}
  ],
  "routing": {
    "context_analyzers": ["openai-sol", "deepseek"],
    "translators": ["openai-luna", "deepseek"],
    "reviewers": ["openai-sol", "deepseek"],
    "rewriters": ["deepseek", "openai-sol"],
    "require_review": true,
    "review_max_rounds": 2,
    "use_style_memory": true,
    "style_anchor_limit": 6,
    "retry_refusals_once": false,
    "grace_retries": 3,
    "defer_fallbacks_until_primary_complete": true,
    "preserve_source_on_failure": true,
    "review_refusal_policy": "fallback",
    "context_chunk_chars": 30000,
    "max_workers": 8
  }
}
```

`reviewers`는 짧은 판정만 수행하고 `rewriters`는 `rewrite`로 판정된 항목만 수정합니다. 따라서 강한 모델을 모든 문장의 재출력에 쓰지 않고 실제 오류 후보에 집중할 수 있습니다. `command` 제공자는 Codex/OpenCode/사내 에이전트 하네스를 연결하는 중립 어댑터입니다. 셸을 거치지 않고 stdin으로 요청 JSON을 받아 stdout으로 결과 JSON 하나를 반환합니다. 자세한 규약은 [docs/provider-protocol.md](docs/provider-protocol.md)를 참고하십시오.

번역 호출 하나에는 출력 대상 문장 하나만 들어갑니다. 다만 전체 대본 분석, 장면 상태, 관계 변화, 캐릭터 말투, 앞뒤 문장과 용어집은 `context-only` 근거로 계속 전달됩니다. 따라서 거부 범위는 한 문장으로 제한하면서도 흐름과 어체 판단은 유지됩니다.

## Python API

```python
from pathlib import Path
from game_l10n import LocalizationOrchestrator

# ProviderRegistry, ProjectContext, ContextPack, OrchestratorConfig를 구성한 뒤 사용합니다.
results = orchestrator.translate_many(segments)
```

실제 애플리케이션에서는 CLI가 컨텍스트 분석·캐시·데이터 경계를 함께 보장하므로 우선 권장됩니다.

## 검사

여러 작업에서 반복될 수 있는 문제와 중립적인 해결법은 [FAQ](docs/faq.md)에 유지합니다. 특정 게임·파일·대사·번역·작업 결과를 식별할 수 있는 내용은 FAQ와 SDK 기록에 남기지 않습니다.

```powershell
python scripts/check_repo_privacy.py
python -m unittest discover -s tests -t . -v
```

OpenAI 연동은 Responses API의 strict JSON Schema 출력을 사용합니다. 구조화 출력은 거부를 명시적으로 감지할 수 있어 폴백 라우팅에 적합합니다. 공급자 정책을 우회하지 않으며, 정상적인 게임 번역 요청이 특정 제공자에서 처리되지 않을 때 구성된 다른 제공자로 넘깁니다.
