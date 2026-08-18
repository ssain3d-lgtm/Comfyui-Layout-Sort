# ComfyUI Layout Sort

워크플로우를 **데이터 흐름 순서대로 자동 정렬**해주는 ComfyUI 커스텀 노드입니다.
`Layout Sort` 노드를 워크플로우에 놓고 실행하거나 노드의 **✨ Sort now** 버튼을 누르면,
현재 캔버스의 모든 노드가 기능 흐름(로더 → 프롬프트 → 샘플링 → 디코딩 → 저장)에 따라
왼쪽에서 오른쪽으로 깔끔하게 재배치됩니다.

A ComfyUI custom node that auto-arranges the current workflow into a clean,
left-to-right layered layout based on its data flow. Trigger it by executing
the node, or instantly with the **✨ Sort now** button. No AI required —
with an optional LM Studio integration that suggests semantic clusters for
ungrouped nodes and creates named group frames for them.

## 설치 (Install)

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/ssain3d-lgtm/Comfyui-Layout-Sort.git
```

ComfyUI를 재시작하면 `utils/layout` 카테고리에 **Layout Sort (Auto Arrange Workflow)**
노드가 나타납니다. 별도 의존성 없음 (ComfyUI 기본 구성만으로 동작).

## 사용법 (Usage)

두 가지 방법으로 정렬할 수 있습니다.

1. **즉시 정렬** — 노드의 **✨ Sort now** 버튼 클릭. 큐 실행 없이 현재 그래프를
   서버로 보내 계산한 뒤 바로 적용합니다.
2. **실행 시 정렬** — 워크플로우를 Queue 하면 노드가 실행되는 시점에 레이아웃이
   정리됩니다. `trigger` 입력(모든 타입 허용)에 아무 출력이나 연결하면 해당 노드
   이후에 실행되도록 순서를 제어할 수 있습니다.

### 옵션 (Options)

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `direction` | `left_to_right` | 흐름 방향. `top_to_bottom`도 지원 |
| `layer_spacing` | `80` | 레이어(열) 사이 간격 (px) |
| `node_spacing` | `40` | 같은 레이어 안 노드 사이 간격 (px) |
| `group_mode` | `cluster` | 그룹 처리 방식 (아래 참조) |
| `animate` | `true` | 노드 이동 애니메이션 |
| `llm_clustering` | `false` | 로컬 LLM으로 그룹 없는 노드를 기능별 클러스터링 (아래 참조) |
| `llm_base_url` | `http://127.0.0.1:1234/v1` | OpenAI 호환 엔드포인트 (LM Studio 기본값) |
| `llm_model` | (빈 값) | 모델 ID. 비워두면 서버에 로드된 첫 모델 자동 선택 |

API 토큰은 보안상 위젯이 아니라 노드의 **🔑 LLM API key** 버튼으로
관리합니다 (아래 참조).

### 그룹 처리 (`group_mode`)

- **`cluster` (기본)** — 재귀 컴파운드 레이아웃. 모든 그룹(중첩 그룹 포함)이
  하나의 클러스터가 되어 내부를 먼저 정렬하고, 부모 컨테이너가 자식 클러스터
  블록과 소속 노드를 다시 계층 배치합니다. 형제 프레임끼리는 어떤 깊이에서도
  절대 겹치지 않으며, 각 링크는 양 끝점의 최소공통조상 레벨에서 정확히 한 번
  배치에 반영됩니다.
- **`refit`** — 그룹을 무시하고 전체를 한 번에 정렬한 뒤, 각 그룹 프레임을
  기존 소속 노드들의 새 위치에 맞게 다시 감쌉니다. 멤버가 섞여 있던 그룹은
  프레임이 겹칠 수 있지만, 전체 링크 길이는 더 짧아지는 경향이 있습니다.

두 모드 모두, 노드가 하나도 없는 빈 그룹 프레임은 새 레이아웃 위에 방치되지
않도록 원래 크기 그대로 레이아웃 아래쪽에 따로 옮겨둡니다.

## LLM 클러스터 제안 (LM Studio 연동)

그룹이 없는 스파게티 워크플로우에서는 링크 구조만으로 "어디까지가 한 기능
단위인지"를 알 수 없습니다. `llm_clustering`을 켜면 로컬 LLM에게 노드
목록(타입·제목·서브그래프 이름 해석 포함)과 연결 관계를 보내 기능별
클러스터를 제안받고, 그 결과를 클러스터 배치에 반영한 뒤 **이름 붙은 그룹
프레임을 캔버스에 자동 생성**합니다.

사용법:

1. [LM Studio](https://lmstudio.ai)를 실행하고 모델(예: Qwen 계열)을 로드한 뒤
   **Local Server**를 켭니다 (기본 `http://127.0.0.1:1234`).
2. Layout Sort 노드에서 `llm_clustering`을 `true`로 바꾸고 정렬을 실행합니다.
   Ollama 등 다른 OpenAI 호환 서버는 `llm_base_url`만 바꾸면 됩니다.

**API 토큰**: LM Studio 로컬 서버는 기본 설정에서 토큰 없이 동작합니다.
LM Studio에 API 키를 설정해뒀거나 인증이 필요한 프록시/원격 서버·OpenRouter
등을 쓸 때만 토큰이 필요합니다.

토큰은 **의도적으로 노드 위젯이 아닙니다** — ComfyUI 위젯 값은 워크플로우
JSON과 생성 이미지 PNG 메타데이터에 저장되어 공유 시 그대로 유출되기
때문입니다. 대신:

- 노드의 **🔑 LLM API key** 버튼 → 마스킹된 입력창에 키 입력 → 서버 측
  파일에만 저장됩니다 (기본 위치: ComfyUI `user/` 디렉토리, 파일 권한
  600, `LAYOUT_SORT_KEY_FILE` 환경변수로 경로 변경 가능). 그래프·PNG·
  브라우저 저장소 어디에도 기록되지 않고, 키를 되돌려주는 API도 없습니다
  (설정 여부만 조회 가능). 버튼에 ✓가 붙으면 설정된 상태입니다.
- 또는 환경변수 `LAYOUT_SORT_LLM_API_KEY` — ComfyUI 실행 전에 설정.
  우선순위는 저장 파일 > 환경변수입니다.

알아둘 점: ComfyUI 포트에 접근할 수 있는 사람은 (ComfyUI 특성상 원래
모든 기능을 쓸 수 있으므로) 키를 새로 덮어쓰거나 LLM 호출에 사용할 수는
있습니다. 다만 저장된 키를 읽어갈 수는 없습니다.

동작 규칙:

- **기존 그룹이 항상 우선입니다.** LLM 클러스터는 어떤 그룹에도 속하지 않은
  노드만 가져갈 수 있습니다. 이미 정리된 부분은 절대 건드리지 않습니다.
- 멤버가 2개 미만인 클러스터, 존재하지 않는 노드 ID는 자동으로 걸러집니다.
- LLM 서버가 꺼져 있거나 응답이 이상하면 **일반 정렬로 자동 폴백**하고
  우측 상단 토스트로 사유를 알려줍니다. 정렬 자체는 항상 동작합니다.
- `group_mode`가 `cluster`일 때만 동작합니다.
- thinking 모델의 `<think>` 블록, 마크다운 코드펜스, 잡담 섞인 응답도
  방어적으로 파싱합니다. 구조화 출력(json_schema)을 지원하지 않는 서버는
  자동으로 일반 모드로 재시도합니다.

## 동작 원리 (How it works)

```
[실행 경로]  LayoutSort 노드 실행
             └─ hidden input EXTRA_PNGINFO 로 워크플로우 JSON(위치·링크 포함) 수신
             └─ Python에서 레이아웃 계산 (layout_core.py)
             └─ PromptServer.send_sync 웹소켓 이벤트로 새 좌표 전송
             └─ web/layoutSort.js 가 수신해 캔버스 노드 이동

[버튼 경로]  ✨ Sort now
             └─ app.graph.serialize() → POST /layout_sort/compute
             └─ 동일한 Python 알고리즘으로 계산 → 응답 좌표를 즉시 적용
```

레이아웃 알고리즘은 계층형(Sugiyama 스타일) 그래프 배치입니다:

1. **레이어 배정** — 링크를 따라 최장 경로 위상 정렬. 데이터가 항상 한 방향으로
   흐르도록 노드를 열(column)에 배정하고, 로더 같은 소스 노드는 처음 사용되는
   지점 바로 앞 열로 당겨 배치합니다.
2. **교차 최소화** — 레이어 내부 순서를 barycenter 휴리스틱으로 여러 번 스윕하여
   링크 교차를 줄입니다.
3. **좌표 계산** — 노드 실제 크기(접힌 노드, 타이틀바 높이 포함)를 반영해 열별로
   세로 중앙 정렬하고, 기존 그래프의 좌상단 위치에 앵커해 화면이 튀지 않게 합니다.
4. **아일랜드 처리** — 링크가 없는 노드(Note 등)는 본 흐름 아래에 따로 정리합니다.
5. **그룹 클러스터** — `cluster` 모드에서는 같은 엔진이 그룹 트리를 따라
   재귀적으로 실행됩니다: 그룹 내부 → 부모 컨테이너 → 최상위 순서로,
   자식 그룹은 크기가 확정된 블록으로 취급됩니다.

동일한 엔진을 ComfyUI 공식 템플릿(`sdxl_refiner_prompt_example` 노드 20/그룹 9,
`video_wan2_2_14B_animate` 노드 40/링크 72/그룹 11 — 3중 중첩 그룹 포함)에
돌려 자동 검증했습니다: 노드 겹침 0, 역방향 링크 0, 프레임 부분 겹침 0,
그룹 경계에 걸친 노드 0. 테스트는 `tests/`에 있으며 ComfyUI 없이 실행됩니다:

```bash
python3 tests/test_layout.py    # 레이아웃 엔진 (NaN 방어, 그룹 주차 포함)
python3 tests/test_llm_e2e.py   # 목(mock) LM Studio 서버로 LLM 경로 E2E
```

## FAQ

**Q. AI(LLM)가 꼭 필요한가요?**
아니요. 핵심 정렬은 결정적 알고리즘만으로 빠르고(수십 ms) 재현 가능하게
동작합니다. LLM은 "그룹이 전혀 없는 워크플로우의 의미 단위 클러스터
제안"이라는 부가 기능에만 옵션으로 쓰이며(`llm_clustering`, 위 섹션 참조),
LLM 서버가 없으면 언제나 일반 정렬로 폴백합니다.

**Q. 서브그래프(Subgraph) 안도 정렬되나요?**
아직 최상위 그래프만 정렬합니다. 서브그래프 내부 정렬은 로드맵에 있습니다.

**Q. 정렬하면 원래보다 옆으로 길어져요.**
의도된 트레이드오프입니다. 수작업 레이아웃은 단계를 위아래로 접어 압축하는
대신 흐름 방향이 뒤섞이지만, 자동 정렬은 "왼쪽에서 오른쪽으로 읽히는 흐름"을
우선합니다. 간격이 부담스러우면 `layer_spacing`/`node_spacing`을 줄여보세요.

## 파일 구성 (Project layout)

```
__init__.py       # 노드/웹 디렉토리 등록
layout_sort.py    # LayoutSort 노드 + /layout_sort/compute 엔드포인트 + 웹소켓 푸시
layout_core.py    # 순수 Python 레이아웃 엔진 (ComfyUI 없이 단독 테스트 가능)
llm_client.py     # LM Studio(OpenAI 호환) 클러스터 제안 클라이언트 (stdlib만 사용)
web/layoutSort.js # 좌표 적용(애니메이션), Sort now 버튼, 그룹 생성, 토스트
tests/            # 단위 테스트 + 목 LM Studio E2E 테스트
```
