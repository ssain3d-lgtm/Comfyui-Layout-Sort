# ComfyUI Layout Sort

워크플로우를 **데이터 흐름 순서대로 자동 정렬**해주는 ComfyUI 커스텀 노드입니다.
`Layout Sort` 노드를 워크플로우에 놓고 실행하거나 노드의 **✨ Sort now** 버튼을 누르면,
현재 캔버스의 모든 노드가 기능 흐름(로더 → 프롬프트 → 샘플링 → 디코딩 → 저장)에 따라
왼쪽에서 오른쪽으로 깔끔하게 재배치됩니다.

A ComfyUI custom node that auto-arranges the current workflow into a clean,
left-to-right layered layout based on its data flow. Trigger it by executing
the node, or instantly with the **✨ Sort now** button — no AI/LLM required.

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

### 그룹 처리 (`group_mode`)

- **`cluster` (기본)** — 재귀 컴파운드 레이아웃. 모든 그룹(중첩 그룹 포함)이
  하나의 클러스터가 되어 내부를 먼저 정렬하고, 부모 컨테이너가 자식 클러스터
  블록과 소속 노드를 다시 계층 배치합니다. 형제 프레임끼리는 어떤 깊이에서도
  절대 겹치지 않으며, 각 링크는 양 끝점의 최소공통조상 레벨에서 정확히 한 번
  배치에 반영됩니다.
- **`refit`** — 그룹을 무시하고 전체를 한 번에 정렬한 뒤, 각 그룹 프레임을
  기존 소속 노드들의 새 위치에 맞게 다시 감쌉니다. 멤버가 섞여 있던 그룹은
  프레임이 겹칠 수 있지만, 전체 링크 길이는 더 짧아지는 경향이 있습니다.

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
그룹 경계에 걸친 노드 0.

## FAQ

**Q. AI(LLM)가 필요한가요?**
핵심 정렬에는 필요 없습니다. "기능별 이해"에 필요한 정보(무엇이 무엇에
연결되는지, 어떤 노드들이 한 그룹인지)는 그래프 구조에 이미 들어 있어서,
결정적 알고리즘만으로 빠르고(수십 ms) 재현 가능하게 정렬됩니다. LLM이
가치를 더할 수 있는 지점은 부가 기능입니다:

- 그룹이 전혀 없는 스파게티 워크플로우에서 의미 단위 클러스터 제안
- 정렬 후 그룹 이름 자동 짓기
- 자연어 지시("업스케일 부분을 아래로 빼줘") 반영

이런 기능이 필요해지면 LM Studio / Ollama 같은 OpenAI 호환 로컬 API를
옵션으로 붙이는 확장이 자연스럽습니다(정렬 파이프라인 자체는 LLM 없이
동작을 유지한 채, 클러스터 제안만 LLM에게 받는 구조). 현재 버전에는
포함되어 있지 않습니다.

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
web/layoutSort.js # 좌표 적용(애니메이션), Sort now 버튼, 웹소켓 리스너
```
