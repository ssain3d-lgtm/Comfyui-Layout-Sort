# ComfyUI Layout Sort

워크플로우를 **데이터 흐름 순서대로 자동 정렬**해주는 ComfyUI 커스텀 노드입니다.
`Layout Sort` 노드를 워크플로우에 놓고 실행하거나 노드의 **✨ Sort now** 버튼을 누르면,
현재 캔버스의 모든 노드가 기능 흐름(로더 → 프롬프트 → 샘플링 → 디코딩 → 저장)에 따라
왼쪽에서 오른쪽으로 깔끔하게 재배치됩니다.

A ComfyUI custom node that auto-arranges the current workflow into a clean,
left-to-right layered layout based on its data flow. Trigger it by executing
the node, or instantly with the **✨ Sort now** button. No AI required —
optionally, type a natural-language request into `llm_prompt` ("vertical,
keep my groups, tighter spacing, group the VAE nodes...") and an LLM
(LM Studio/Ollama/OpenAI/Anthropic) translates it into the sorter's own
controls; geometry always stays deterministic.

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
| `style` | `flow` | 정렬 스타일 (아래 참조) |
| `animate` | `true` | 노드 이동 애니메이션 |
| `llm_prompt` | (빈칸) | 자연어 정렬 지시. 비워두면 LLM을 전혀 쓰지 않음 (아래 참조) |
| `llm_provider` | `lmstudio` | `lmstudio` / `ollama` / `openai`(ChatGPT) / `anthropic`(Claude) / `custom` |
| `llm_base_url` | `http://127.0.0.1:1234/v1` | `custom`일 때 사용할 엔드포인트 |
| `llm_model` | `auto` | 사용할 모델. `auto`면 서버의 첫 로드 모델 자동 선택 |
| `llm_max_tokens` | `4096` | LLM 출력 토큰 한도 (256 ~ 262144). thinking 모델은 크게 |

노드의 **🔌 Connect (load models)** 버튼을 누르면 서버의 모델 목록을
받아와 `llm_model`이 드롭다운으로 바뀝니다. API 토큰은 보안상 위젯이
아니라 노드의 **🔑 LLM API key** 버튼으로 관리합니다 (아래 참조).

### 그룹 처리 (`group_mode`)

- **`cluster` (기본)** — 재귀 컴파운드 레이아웃. 모든 그룹(중첩 그룹 포함)이
  하나의 클러스터가 되어 내부를 먼저 정렬하고, 부모 컨테이너가 자식 클러스터
  블록과 소속 노드를 다시 계층 배치합니다. 형제 프레임끼리는 어떤 깊이에서도
  절대 겹치지 않으며, 각 링크는 양 끝점의 최소공통조상 레벨에서 정확히 한 번
  배치에 반영됩니다.
- **`inner`** — **매크로 배치 유지 모드.** 사람이 잡아둔 그룹의 위치
  (좌상단 모서리)는 그대로 두고, 각 그룹 **내부의 노드만** 정렬한 뒤
  프레임 크기를 내용에 맞춥니다. 그룹 밖 노드는 건드리지 않는 것이
  원칙이며, 프레임이 자라나 이웃 그룹·노드와 겹치게 되는 경우에만
  겹친 이웃을 오른쪽/아래로 **최소한만 밀어내** 겹침을 해소합니다.
  큰 그림은 이미 마음에 들고 내부만 지저분할 때 쓰세요.
- **`refit`** — 그룹을 무시하고 전체를 한 번에 정렬한 뒤, 각 그룹 프레임을
  기존 소속 노드들의 새 위치에 맞게 다시 감쌉니다. 멤버가 섞여 있던 그룹은
  프레임이 겹칠 수 있지만, 전체 링크 길이는 더 짧아지는 경향이 있습니다.
  대형 워크플로우에는 비추천입니다.

두 모드 모두, 노드가 하나도 없는 빈 그룹 프레임은 새 레이아웃 위에 방치되지
않도록 원래 크기 그대로 레이아웃 아래쪽에 따로 옮겨둡니다.

대형 워크플로우 대응: for-loop 구조처럼 그룹 사이에 순환 참조가 있으면
탐욕적 feedback-arc 절단으로 **루프백 링크만** 뒤로 향하게 배치하고(순환
전체가 한 열에 뭉개지지 않음), 한 레이어가 너무 길어지면(기본 2600px)
같은 레이어의 노드들을 인접한 여러 열로 나눠 배치합니다 — 같은 레이어끼리는
연결이 없으므로 흐름 방향은 유지됩니다. 연결 없는 하위 그룹 묶음은
정사각형에 가깝게 선반 배치됩니다.

**Set/Get 무선 연결 인식**: KJNodes의 `SetNode`/`GetNode`(및 유사 노드)는
JSON에 케이블이 없지만, 정렬기가 같은 키의 Set → Get을 **레이아웃 전용
가상 링크**로 인식해 논리적 흐름대로 배치합니다 (실제 선이 생기지는
않습니다). 이 기능이 없으면 모든 GetNode가 소스처럼 보여 매크로 순서가
뒤섞입니다.

### 정렬 스타일 (`style`)

- **`flow` (기본)** — 열을 세로 중앙축에 정렬합니다. 실측 결과 큰
  워크플로우에서 링크 교차가 눈에 띄게 적어(205노드 실전 기준 grid 대비
  약 1.5배 차이) 기본값입니다.
- **`grid`** — 열들이 위쪽 모서리를 공유합니다(그룹 내부는 그룹의
  좌상단 기준). 소형 워크플로우에서 딱 떨어지는 느낌을 원할 때.

두 스타일 모두 좌표를 10px 캔버스 그리드에 스냅하며, **노드 크기는 절대
변경하지 않습니다** — 정렬기는 위치만 옮깁니다.

## 프롬프트로 정렬 지시하기 (LLM 연동, 선택)

`llm_prompt`에 원하는 정렬을 **평소 말하듯** 적으면 됩니다 (한국어 등
어떤 언어든). 예시:

- "세로로 정렬해줘" → `direction: top_to_bottom`
- "그룹 위치는 그대로 두고 안쪽만 정리해" → `group_mode: inner`
- "간격을 더 좁게, 딱 떨어지는 느낌으로" → 간격 축소 + `style: grid`
- "VAE 관련 노드끼리 묶어줘" → 이름 붙은 클러스터 프레임 생성

LLM은 실제 워크플로우 요약(노드 타입·제목·링크·기존 그룹·현재 설정)을
읽고, 요청을 **이 노드가 원래 가진 컨트롤**(방향·간격·그룹 모드·스타일
+ 미그룹 노드 클러스터)로 번역한 JSON 계획만 돌려줍니다. 번역된 옵션은
그 실행 1회에 한해 위젯 값보다 우선하고, **좌표 계산은 언제나 결정론
엔진이 수행**합니다 — LLM이 노드를 직접 배치하는 일은 없습니다.

정직 고지: 엔진 밖의 요청(특정 픽셀 위치 지정, 노드 크기·색 변경, 연결
재배선 등)은 흉내내지 않고 "지원 안 됨" 토스트로 알려줍니다. 계획이
무엇을 설정했는지는 성공 토스트 한 줄로 요약됩니다. `llm_prompt`가
비어 있으면 **LLM은 단 한 번도 호출되지 않습니다.**

사용법:

1. [LM Studio](https://lmstudio.ai)를 실행하고 모델(예: Qwen 계열)을 로드한 뒤
   **Local Server**를 켭니다 (기본 `http://127.0.0.1:1234`).
2. Layout Sort 노드의 `llm_prompt`에 원하는 지시를 적고 정렬을 실행합니다.
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

**오리진 바인딩**: 저장된 키는 저장 시점의 `llm_base_url` 오리진에
묶입니다. 로컬(loopback) 주소로는 항상 전송되지만, 그 외 주소로는
바인딩된 오리진과 일치할 때만 전송됩니다. 따라서 누군가 공유한
워크플로우의 `llm_base_url`이 악성 서버로 바뀌어 있어도 저장된 키가
그쪽으로 새지 않습니다. 원격 서버에서 키를 쓰려면 그 서버를 가리킨
상태에서 키를 저장하세요. 환경변수 키의 허용 오리진은
`LAYOUT_SORT_LLM_ALLOWED_ORIGIN`으로 지정합니다 (미지정 시 로컬 전용).

**클라우드 API 사용 (선택)**: `llm_provider` 드롭다운에서 고르면 됩니다.

- **ChatGPT**: `llm_provider = openai` → 🔑 버튼으로 API 키 저장(해당
  오리진에 자동 바인딩) → 🔌 Connect로 모델 선택. OpenRouter 등 다른
  OpenAI 호환 클라우드는 `custom` + `llm_base_url`로 연결합니다.
- **Claude**: `llm_provider = anthropic` → 🔑 버튼으로 키 저장 → 사용.
  Anthropic Messages API 형식으로 자동 전환되며(`x-api-key` 인증 포함),
  `auto`는 `claude-opus-5`를 사용합니다. 🔌 Connect로 다른 모델(더 저렴한
  Haiku 등)을 고를 수 있습니다.
- 프로바이더를 바꾸면 `llm_base_url`에 해당 엔드포인트가 자동으로
  표시됩니다 (`custom`일 때만 직접 입력한 값이 사용됩니다).
- **CLI 도구(claude/codex CLI 등) 연동은 지원하지 않습니다** — 의도된
  결정입니다. 이 작업은 단발 JSON 생성이라 API 호출이 정확한 도구이고,
  서버가 로컬 CLI를 실행하는 구조는 ComfyUI 포트 접근자가 구독을 소모
  시키거나 에이전트 CLI를 통해 더 큰 권한을 얻을 수 있는 보안 문제가
  있습니다.

알아둘 점: ComfyUI 포트에 접근할 수 있는 사람은 (ComfyUI 특성상 원래
모든 기능을 쓸 수 있으므로) 키를 새로 덮어쓰거나 LLM 호출에 사용할 수는
있습니다. 다만 저장된 키를 읽어갈 수는 없습니다.

추가 보호 장치: 키는 원자적으로 소유자 전용(600) 권한으로 생성되고,
제어문자가 섞인 키는 저장·전송 전에 거부되며(오류 메시지에 키가 절대
포함되지 않음), 서버가 다른 오리진으로 리다이렉트하면 `Authorization`
헤더를 제거한 채 따라가므로 악의적 엔드포인트가 토큰을 가로챌 수
없습니다.

동작 규칙:

- 계획의 옵션은 **화이트리스트 검증**을 거칩니다: 허용된 5개 컨트롤 외의
  키는 버려지고, 간격은 10~600px로 클램프됩니다 — 모델이 혼란스러워도
  엔진 내부 옵션을 건드릴 수 없습니다.
- **기존 그룹이 항상 우선입니다.** 클러스터는 어떤 그룹에도 속하지 않은
  노드만 가져갈 수 있습니다. 이미 정리된 부분은 절대 건드리지 않습니다.
- 멤버가 2개 미만인 클러스터, 존재하지 않는 노드 ID는 자동으로 걸러지고,
  클러스터 생성은 `group_mode: cluster`에서만 반영됩니다(아니면 "지원 안
  됨"으로 알림).
- LLM 서버가 꺼져 있거나 응답이 이상하면 **위젯 설정 그대로 일반 정렬로
  자동 폴백**하고 우측 상단 토스트로 사유를 알려줍니다. 정렬 자체는 항상
  동작합니다.
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
python3 tests/test_llm_e2e.py   # 목(mock) 서버로 프롬프트 계획 경로 E2E
python3 tests/test_routes.py    # aiohttp 라우트 계층 (키 저장/검증 포함)
```

## FAQ

**Q. AI(LLM)가 꼭 필요한가요?**
아니요. 핵심 정렬은 결정적 알고리즘만으로 빠르고(수십 ms) 재현 가능하게
동작합니다. LLM의 역할은 딱 하나 — `llm_prompt`에 적은 자연어 지시를
이 노드의 컨트롤로 번역하는 것(위 섹션 참조)입니다. 프롬프트가 비어
있으면 호출 자체가 없고, LLM 서버가 없으면 언제나 일반 정렬로 폴백합니다.

**Q. "reply contains no JSON" 오류가 떠요.**
대부분 **thinking 모델**(qwen3 등)이 원인입니다. 이런 모델은 답을 내기
전에 `<think>` 블록 안에서 토큰을 소모하는데, 출력 한도에 걸리면 JSON을
시작하기도 전에 잘립니다. 잘린 `<think>` 응답을 감지하면 "token limit"
안내가 담긴 구체적인 오류를 보여줍니다. 해결: `llm_max_tokens`를 올리거나
(모델 컨텍스트가 허용하는 만큼, 최대 262144), 같은 모델의
**non-thinking/instruct 변형**을 쓰거나, LM Studio에서 컨텍스트 길이를
늘려보세요. 정렬 자체는 항상 폴백으로 동작합니다.

**Q. 정렬하면 Reroute(경유점)는 어떻게 되나요?**
최신 ComfyUI의 네이티브 reroute 포인트도 함께 재배치됩니다. 노드 이동
후 각 reroute 체인을 출발 노드 → 도착 노드의 새 경로 위에 순서대로
고르게 재배치합니다.

**Q. 정렬 결과가 마음에 안 들면요?**
**Ctrl+Z** 한 번이면 정렬 직전 상태로 완전히 돌아갑니다 (정렬 1회 =
언두 1단계).

**Q. Connect를 누르면 `getaddrinfo failed` 오류가 떠요.**
예전 버전 노드로 저장된 워크플로우를 열면 위젯 값이 한 칸씩 밀려
(`llm_base_url` 자리에 "auto"가 들어가는 식) 가짜 호스트를 조회하다
실패하는 문제였습니다. 현재 버전은 로드 시 밀린 값을 자동 교정하고,
서버 쪽에서도 잘못된 값을 기본 엔드포인트로 치환합니다. 그래도 문제가
있으면 Layout Sort 노드를 삭제하고 새로 추가하면 확실합니다.

**Q. Layout Sort 노드 자신은 어디로 가나요?**
`trigger` 입력에 뭔가 연결되어 있어도 그 링크는 실행 순서용일 뿐,
레이아웃 계산에서는 제외됩니다. Layout Sort 노드는 항상 본 흐름 아래의
유틸리티 영역에 정리됩니다.

**Q. 서브그래프(Subgraph) 안도 정렬되나요?**
아직 최상위 그래프만 정렬합니다. 로드맵: 서브그래프 내부 재귀 정렬,
긴 링크의 가상 정점(dummy vertex) 기반 교차 최소화.

**Q. 정렬하면 원래보다 옆으로 길어져요.**
의도된 트레이드오프입니다. 수작업 레이아웃은 단계를 위아래로 접어 압축하는
대신 흐름 방향이 뒤섞이지만, 자동 정렬은 "왼쪽에서 오른쪽으로 읽히는 흐름"을
우선합니다. 간격이 부담스러우면 `layer_spacing`/`node_spacing`을 줄여보세요.

## 파일 구성 (Project layout)

```
__init__.py       # 노드/웹 디렉토리 등록
layout_sort.py    # LayoutSort 노드 + /layout_sort/compute 엔드포인트 + 웹소켓 푸시
layout_core.py    # 순수 Python 레이아웃 엔진 (ComfyUI 없이 단독 테스트 가능)
llm_client.py     # 프롬프트→정렬 계획 번역 LLM 클라이언트 (stdlib만 사용)
web/layoutSort.js # 좌표 적용(애니메이션), Sort now 버튼, 그룹 생성, 토스트
tests/            # 단위 테스트 + 목 LM Studio E2E 테스트
```
