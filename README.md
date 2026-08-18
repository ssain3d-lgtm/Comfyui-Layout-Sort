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
| `animate` | `true` | 노드 이동 애니메이션 |

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
5. **그룹 유지** — 그룹 프레임은 정렬 전 소속 노드들을 기준으로 새 위치에 맞게
   다시 감싸줍니다.

## FAQ

**Q. AI(LLM)가 필요한가요?**
아니요. "기능별 이해"에 필요한 정보(무엇이 무엇에 연결되는지)는 그래프 구조에
이미 들어 있어서, 결정적 알고리즘만으로 빠르고 재현 가능하게 정렬됩니다.
LLM은 그룹 이름 자동 짓기, 의미 단위 클러스터링 같은 부가 기능에만 고려할
가치가 있습니다.

**Q. 겹쳐 있는 그룹이 이상해져요.**
정렬은 링크 기준이므로, 서로 멤버가 섞여 있던 그룹은 정렬 후 프레임이 겹칠 수
있습니다. 그룹 단위 클러스터 배치는 로드맵에 있습니다.

## 파일 구성 (Project layout)

```
__init__.py       # 노드/웹 디렉토리 등록
layout_sort.py    # LayoutSort 노드 + /layout_sort/compute 엔드포인트 + 웹소켓 푸시
layout_core.py    # 순수 Python 레이아웃 엔진 (ComfyUI 없이 단독 테스트 가능)
web/layoutSort.js # 좌표 적용(애니메이션), Sort now 버튼, 웹소켓 리스너
```
