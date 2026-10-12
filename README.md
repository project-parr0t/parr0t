# Agent Game Translation SDK

게임 전체 맥락과 캐릭터 말투를 먼저 분석한 뒤, 번역 AI와 독립 검수 AI를 폴백 체인으로 운용하는 Python SDK입니다. 최상위 오케스트레이터는 의미를 직접 판단하지 않고 각 에이전트가 정상 완료했는지, 거부·오류·형식 위반이 있었는지, 결정론적 무결성 검사를 통과했는지만 확인합니다.

## 핵심 동작

```text
전체 스크립트
  → 컨텍스트 분석 AI 체인
     (스토리/장면/관계 변화/캐릭터 말투 규칙)
  → 번역 AI 체인
     (거부·오류·형식 실패 시 다음 모델)
  → 플레이스홀더/태그/스크립트 침범 검사
  → 독립 검수 AI 체인
     (의미·누락·첨가·말투·용어를 확인하고 필요 시 수정)
  → 최종 무결성 검사
  → 에이전트 작업 디렉터리에만 결과 저장
```

- 컨텍스트 분석이 전부 거부되거나 실패하면 번역을 시작하지 않습니다.
- 번역기와 검수기는 서로 다른 제공자/모델 체인을 쓸 수 있습니다.
- 허구의 폭력·욕설·공포·로맨스·성인 등급 요소를 이유로 임의 순화/거부하지 않도록 변환 작업의 성격을 명시합니다. 한 번의 명확화 재시도 뒤에도 거부하면 다음 제공자로 넘깁니다.
- 검수기가 거부하거나 잘못된 결과를 내면 다음 검수기로 라우팅합니다.
- `{name}`, `%s`, `<tag>`, `\\n`, 엔진 제어 코드와 사전 지정어를 결정론적으로 검사합니다.
- 원문에 없던 `<script>`, `javascript:`, `eval(` 등의 삽입을 차단합니다.
- 이전에 승인된 동일 화자의 번역을 후속 대사의 스타일 앵커로 제공합니다.
- 동일 원문·컨텍스트·사전·모델·설정은 콘텐츠 해시 캐시로 다시 처리하지 않습니다.
- unpack/pack도 입력과 도구 지문이 같으면 다시 실행하지 않습니다.

## 데이터 격리 원칙

SDK 저장소에는 게임 데이터와 번역 데이터가 들어가지 않습니다. 실제 작업은 반드시 저장소 밖에 만듭니다.

```powershell
python -m pip install -e .
game-l10n init-workspace C:\agent-work\my-game --sdk-root C:\src\agent-game-translation-sdk
```

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
  --engine unity --version 2023.1 --user-confirmed-success
```

이 단계는 작업공간의 `sdk-export/sdk-unity-2023.1/`에 `tools/`와 해시 manifest만 만듭니다. 원문과 번역문은 복사하지 않습니다. 그 후 에이전트가 커밋/푸시 여부를 다시 묻고 승인받아야 합니다.

```powershell
game-l10n policy --workspace C:\agent-work\my-game --publish-approved yes
game-l10n publish-sdk --workspace C:\agent-work\my-game `
  --sdk-root C:\src\agent-game-translation-sdk `
  --profile C:\agent-work\my-game\sdk-export\sdk-unity-2023.1 `
  --commit --push
```

`publish-sdk`는 깨끗한 `main`에서 `sdk-unity-2023.1` 브랜치를 만들며, 저장소가 없을 때 임의로 `git init`하지 않습니다.

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
    {"name": "t1", "type": "openai_responses", "model": "YOUR_MODEL", "api_key_env": "OPENAI_API_KEY"},
    {"name": "t2", "type": "compatible_chat", "model": "local-model", "base_url": "http://127.0.0.1:8000/v1", "api_key_env": "LOCAL_API_KEY"},
    {"name": "r1", "type": "command", "command": ["python", "tools/model_adapter.py"]}
  ],
  "routing": {
    "context_analyzers": ["t1", "t2"],
    "translators": ["t1", "t2"],
    "reviewers": ["r1", "t2"],
    "require_review": true,
    "use_style_memory": true,
    "style_anchor_limit": 6,
    "retry_refusals_once": true,
    "context_chunk_chars": 30000,
    "max_workers": 1
  }
}
```

`command` 제공자는 Codex/OpenCode/사내 에이전트 하네스를 연결하는 중립 어댑터입니다. 셸을 거치지 않고 stdin으로 요청 JSON을 받아 stdout으로 결과 JSON 하나를 반환합니다. 자세한 규약은 [docs/provider-protocol.md](docs/provider-protocol.md)를 참고하십시오.

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
