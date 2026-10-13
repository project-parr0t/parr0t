> **개발 방식 공개:** `Parr0t`은 완전히 바이브 코드로 제작된 시스템이며, 일반 개발자는 방향 지시와 사례 분석만 수행했습니다.

# Parr0t

게임의 말을 문맥째 옮기는, 해적 안대 앵무새 번역 오케스트레이터.

`Parr0t`은 사람이 명령어를 하나씩 실행하는 일반 사용자용 프로그램이 아닙니다. **[Codex](https://github.com/openai/codex) 또는 [OpenCodex](https://github.com/lidge-jun/opencodex) 경로에서 동작하는 코딩 에이전트가 읽고 운용하는 에이전트 전용 프레임워크**입니다. 사람은 번역 대상과 공급자, Git 사용 여부, 최종 패치 승인 같은 방향만 결정합니다. 대본 분석, 캐릭터 말투 구성, 추출, 번역, 거부 폴백, 검수, 패킹, 무결성 검사는 에이전트가 수행합니다.

이름은 앵무새(parrot)에서 왔습니다. 공식 마스코트는 **해적 안대를 쓴 앵무새**입니다.

## 무엇을 보장하는가

- 전체 대본의 순서와 장면, 관계 변화, 캐릭터 성향을 먼저 분석한 뒤 문장을 번역합니다.
- 한 문장의 거부나 오류가 배치 전체를 중단시키지 않으며, 해당 문장만 다음 번역 경로로 보냅니다.
- 번역을 마친 뒤 독립 검수 경로가 의미, 말투, 용어, 플레이스홀더와 제어 코드를 확인합니다.
- 최상위 오케스트레이터는 원문이나 번역문을 직접 읽고 판단하지 않고 상태와 무결성 결과만 관리합니다.
- 모든 번역 경로가 실패한 항목은 원문을 보존하며, 원본 게임 데이터는 수정하지 않습니다.
- 게임 데이터, 번역 결과, 작업 로그는 외부 에이전트 작업공간에만 남고 이 저장소에는 들어오지 않습니다.
- 첫 구현 전에 기존 `sdk-*` 브랜치를 강제로 조사하며, 호환 SDK가 있으면 재사용하고 부분 호환이면 새로 만들지 않고 확장합니다.
- 특정 공급자에 종속되지 않습니다. Codex App의 에이전트와 외부 API를 조합하는 구성을 권장하지만 역할별 공급자를 교체할 수 있습니다.
- 완성된 페이로드는 Steam 라이브러리 자동 탐색과 수동 폴백을 갖춘 최소 설치 ZIP으로 만들 수 있습니다.

## 에이전트에게 시작시키기

이 저장소와 번역 대상 폴더를 같은 Codex 프로젝트 범위에서 열고 다음처럼 요청합니다.

```text
Parr0t의 AGENTS.md와 docs/agent-usage.md를 먼저 읽어라.
원본 게임 폴더는 읽기 전용으로 다루고, 저장소 밖에 별도 작업공간을 만들어라.
전체 스크립트 흐름과 캐릭터별 말투를 분석한 뒤 번역하고, 거부 항목만 폴백하며,
독립 검수와 결정론적 무결성 검사를 통과한 새 패치를 만들어라.
시작하기 전에 Git 사용 여부만 물어보고, 실제 대사나 번역문을 상위 오케스트레이터나 저장소에 남기지 마라.
```

에이전트는 [`AGENTS.md`](AGENTS.md)를 정책 계약으로, [`docs/agent-usage.md`](docs/agent-usage.md)를 실행 절차로 사용합니다. 상황별 복사 가능한 요청문은 [`docs/prompt-examples.md`](docs/prompt-examples.md)에 있습니다.

## 저장소 구조

```text
main                         공급자 중립 오케스트레이션 코어
sdk-<engine>-<version>       재사용 가능한 추출·적용·패킹 구현
외부 에이전트 작업공간       원문, 번역문, 캐시, 패치, 비공개 로그
```

`main`은 어떤 게임 엔진에도 의존하지 않습니다. 엔진별 구현과 주의사항은 해당 SDK 브랜치만 소유하며, 의존성은 `main`에서 SDK 브랜치 방향으로만 흐릅니다. 자세한 경계는 [`docs/sdk-branch-contract.md`](docs/sdk-branch-contract.md)를 따릅니다.

## 에이전트 문서

- [`docs/agent-usage.md`](docs/agent-usage.md): Codex 및 OpenCodex 기준 전체 운용 절차
- [`docs/sdk-selection.md`](docs/sdk-selection.md): 기존 SDK 브랜치 조사·평가·재사용을 강제하는 시작 게이트
- [`docs/prompt-examples.md`](docs/prompt-examples.md): 신규 작업, 재개, 검증, SDK 승격 요청 예제
- [`docs/execution-budget.md`](docs/execution-budget.md): 범위 확장·중복 검사·불필요한 위임을 막는 실행 예산
- [`docs/runtime-approval.md`](docs/runtime-approval.md): 실제 설치·업데이트·제거·실행을 분리하는 일회성 승인 경계
- [`docs/installer-contract.md`](docs/installer-contract.md): 사용자용 Windows 설치 파일·메시지·오류 코드·상태 파일 계약
- [`docs/steam-packaging.md`](docs/steam-packaging.md): Steam 설치 ZIP 제작과 상태 파일·영수증 기반 설치·복원
- [`docs/steam-faq.md`](docs/steam-faq.md): Steam 게임 탐색, 실행 래퍼 및 Steam 비종료 원칙
- [`docs/wrapper-executable.md`](docs/wrapper-executable.md): Steam용 공용 래퍼 EXE의 재현 빌드·서명·해시·백신 신뢰 계약
- [`docs/instance-release.md`](docs/instance-release.md): Windows 기본 사용자 환경, README 보존, 버전 호환성과 릴리스 동일성 계약
- [`docs/review-pipeline.md`](docs/review-pipeline.md): 저토큰 독립 검수와 선택적 재작성
- [`docs/provider-protocol.md`](docs/provider-protocol.md): 공급자·명령형 워커 연결 규약
- [`docs/codex-app-deepseek.md`](docs/codex-app-deepseek.md): 권장 App + API 구성 예시
- [`docs/fallback-routing.md`](docs/fallback-routing.md): API + API 및 공급자 중립 폴백
- [`docs/faq.md`](docs/faq.md): 작업 출처를 제거한 재사용 가능한 문제 해결법

설치 패키지명 `game-l10n-sdk`와 내부 CLI `game-l10n`은 기존 자동화 호환성을 위해 유지합니다. 이는 사람용 인터페이스를 뜻하지 않으며 에이전트가 사용하는 안정된 실행 경계입니다.

## 주의

- 이 저장소만으로 모든 게임을 즉시 패치할 수 있는 범용 DLL은 제공하지 않습니다. 대상 형식에 맞는 SDK 브랜치나 비공개 어댑터가 필요합니다.
- 실제 패치 설치와 실행은 기본적으로 사용자가 수행합니다. 에이전트에게 맡기는 경우에도 설치·업데이트·제거·실행은 각각 별도 일회성 승인이 필요하며, 성공 확인 전에는 작업별 구현을 공개 SDK로 승격하지 않습니다.
- 배포 ZIP을 사람이 직접 실행하는 경우에는 설치기의 메뉴 선택과 최종 확인만 사용하며, 에이전트용 승인 파일이나 하네스를 요구하지 않습니다.
- 공급자 안전장치를 우회하거나 비활성화한다고 주장하지 않습니다. 정상적인 번역 요청이 거부되면 문장 단위로 격리해 허용된 다른 경로로 넘깁니다.

실험적 에이전트 프레임워크이며 라이선스는 [MIT](LICENSE)입니다.
