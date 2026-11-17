> **개발 방식 공개:** Parr0t은 완전히 바이브 코딩으로 제작되었습니다. 개발자는 방향 제시와 사례 분석을 맡았고, 구현과 문서화는 코딩 에이전트를 중심으로 진행했습니다.

# Parr0t

게임의 전체 문맥을 분석하고, 번역·독립 검수·검증을 거쳐 복원 가능한 한국어 패치를 만드는 프레임워크입니다.
Codex에서 요청 프롬프트로 시작하거나, OMP에서 프롬프트·통신 격리 훅을 연결한 네이티브 에이전트로 실행할 수 있습니다.

## 사용법

1. [프롬프트 조합기](https://project-parr0t.github.io/parr0t/)에서 **한국어 패치 제작** 또는 **기존 프로젝트 업데이트·마이그레이션**을 선택합니다. 업데이트·마이그레이션에는 이전 레포지토리 URL을 입력합니다.
2. 생성된 프롬프트를 복사해 Codex 또는 아래 런처로 시작한 OMP 세션에 전달합니다. 패치 제작에는 **게임 폴더도 같은 메시지에 첨부**하거나 경로를 적고, 프로젝트 갱신에는 필요한 현재 게임 빌드를 추가합니다.
3. 외부 작업 폴더에 생성된 패치와 검증 결과를 확인합니다. 실제 게임에 설치하거나 실행하는 권한은 별도로 승인합니다.

한국어 패치 **존재 여부 검사만 생략**하거나 기존 한국어를 원어부터 재번역하는 요청을 구분할 수 있습니다. 폰트·UI 이미지·모드 제작, 역할별 모델·문맥 파악, 약/강검수 횟수와 전체/단계별 동시 실행 상한도 함께 조합할 수 있습니다.

Sol 검수는 최초 호출을 포함해 대상당 최대 **2회**입니다. 폴백 검수 모델·횟수는 사용자가 지정하며, 템플릿에 이미 지정한 값이 있으면 그대로 따릅니다. 지정하지 않은 폴백 설정을 임의의 모델이나 횟수로 채우지 않습니다.

게임 파일과 추출문을 대화에 붙여 넣지 않아도 됩니다. 원본 게임은 읽기 전용으로 유지하며, 실제 처리 전에 Parr0t의 text-free control-plane 진입과 SDK 확인을 수행합니다. 게임 작업의 Git 사용 여부와 제공자 정책은 각각의 승인·기존 설정을 따릅니다.

조합기의 로컬 실행과 GitHub Pages 게시 설정은 [시작 안내](manual/starting_prompts.md)에 정리했습니다. Pages 게시 주소는 위 링크이며, 첫 게시에는 저장소의 Pages 소스 설정이 필요합니다.

## OMP에서 사용하기

Python 3.11 이상과 OMP가 설치되어 있어야 합니다. 전체 코어 저장소를 내려받고 아래처럼 **게임 폴더를 열지 않은 상태에서 외부 작업 폴더부터** 초기화합니다.

```text
python -m pip install -e .
game-l10n init-workspace "<external-workspace>" --sdk-root "<repository>" --game-root "<source-root>"
python -m game_l10n.omp_session --workspace "<external-workspace>" --model "<approved-provider/model-id>" --mode workspace --foreground
```

`--model`에는 현재 OMP에서 허용된 정확한 최상위 모델 ID를 지정합니다. 새 세션에 위의 게임별 요청을 전달하세요. 기본 `prompt` 모드는 하위 에이전트의 충돌하는 협업 지시와 직접 메시지·기록 열람 경로를 제한하는 경량 훅을 연결하며, 일반 Python 실행은 유지합니다. 전체 실행 가드·전용 실행기가 필요하면 `--guarded`를 선택합니다. 어느 모드도 OS 샌드박스는 아닙니다. Codex의 기본 제공자 설정이나 전역 OMP 설정은 변경하지 않습니다. 이미 초기화된 작업은 첫 두 명령을 반복하지 않습니다.

명령만 확인하려면 마지막 명령에 `--print-command`를 추가합니다. 이 옵션은 세션이나 파일을 만들지 않습니다. 제어 세션 회전이 요구되는 작업은 `--mode rotation`을 사용하며, 이전 대화 기록을 불러오는 `omp --resume`와는 다릅니다. 워커 모델 바인딩과 호스트 조건은 [OMP 진입 규약](docs/control-plane-contract.md#omp-entry)을 따릅니다.

## 하는 일

- 전체 게임 흐름과 화면 구조를 먼저 분석하고, 대사가 있다면 장면·인물 관계·캐릭터별 말투까지 반영합니다.
- 문장 단위 번역 실패가 전체 작업을 중단하지 않도록 폴백합니다.
- 대사는 자연스러움·말투·패러디를, UI는 기능·용어·수치·단위의 일관성을 독립 검수합니다.
- 기존 한국어를 개선할 때는 원어를 뜻의 기준으로 삼고 항목 대응과 누락·오역을 다시 확인합니다.
- 플레이스홀더, 태그, 제어 코드, UI 배치와 게임 로직이 손상되지 않았는지 확인합니다.
- 원본을 수정하지 않고 새 패치와 복원 가능한 설치본을 만듭니다.

## 데이터와 SDK

게임 파일, 추출문, 번역문, 로그와 API 키는 Parr0t 저장소에 넣지 않습니다. 게임별 데이터는 각 호스트의 외부 작업 폴더에만 남습니다.

`main`은 엔진 중립 오케스트레이션 코어이며, 실제 게임 형식은 `sdk-<engine>-<version>` 브랜치가 담당합니다. 새 도구를 만들기 전에 기존 SDK를 확인하고, 성공이 검증된 재사용 코드만 게임 데이터를 제거한 뒤 SDK로 승격합니다.

## 에이전트 문서

이 README는 사람을 위한 시작 안내입니다. Codex와 OMP는 다음 문서를 공통 실행 규약으로 사용합니다.

- [`AGENTS.md`](AGENTS.md)
- [`docs/agent-usage.md`](docs/agent-usage.md)
- [`docs/control-plane-contract.md`](docs/control-plane-contract.md)
- [`docs/coverage-gates.md`](docs/coverage-gates.md)
- [`docs/release-checkpoint.md`](docs/release-checkpoint.md)
- [`docs/prompt-examples.md`](docs/prompt-examples.md): 재개·검증·개별 설치/실행 승인 등 에이전트용 요청 예제
- [`docs/faq.md`](docs/faq.md)

Parr0t은 모델이나 API의 안전장치, DRM, 안티치트 또는 플랫폼 보호 기능을 우회하는 도구가 아닙니다.

[정식 저장소](https://github.com/project-parr0t/parr0t) · [MIT License](LICENSE)
