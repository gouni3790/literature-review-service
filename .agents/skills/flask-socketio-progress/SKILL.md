---
name: flask-socketio-progress
description: Flask-SocketIO 로 장기 작업 진행률을 emit 하는 패턴 — Scopus 수집, 클러스터링, 배치 작업의 실시간 업데이트. socketio_handlers.py / 배치 잡 작성 시 사용.
---

# SocketIO 진행 이벤트 패턴

## 룸(room) 설계

```
researcher:{id}        # 연구원별 작업 (수집, 클러스터링)
batch:daily            # 전체 배치 진행
admin                  # 관리자 알림
```

→ join 은 client 가 자기 ID 룸으로 자동.

## 이벤트 카탈로그

| event | payload | 의미 |
|---|---|---|
| `scopus.collect.start` | `{researcher_id, expected_count}` | 시작 |
| `scopus.collect.progress` | `{researcher_id, done, total}` | 페이지마다 |
| `scopus.collect.done` | `{researcher_id, collected, errors}` | 완료 |
| `cluster.start` | `{researcher_id}` | |
| `cluster.done` | `{researcher_id, k, silhouette}` | |
| `batch.progress` | `{job, step, message}` | 배치 단계 |
| `recommend.new` | `{researcher_id, core, related, reference}` | 추천 도착 |

→ 이름은 `domain.action.state` 형식으로 통일.

## emit 헬퍼 패턴

```python
# app/litreview/socketio_handlers.py
from flask_socketio import SocketIO

socketio: SocketIO  # init_app 으로 주입

def emit_progress(event: str, payload: dict, room: str):
    """배치/수집 코드에서 호출. 실패해도 본 작업 영향 ❌."""
    try:
        socketio.emit(event, payload, to=room)
    except Exception as e:
        logger.warning("socketio emit failed: %s", e)
```

## 백그라운드 잡과의 결합

```python
# APScheduler 잡 안에서
from app import socketio

def daily_paper_pipeline():
    for r in researchers:
        socketio.emit("batch.progress", {"job": "daily", "step": "collect", "researcher": r.id}, to="admin")
        ...
```

- Flask-SocketIO 는 **app context** 안에서만 emit 가능 — 잡 시작 시 `with app.app_context():` 감싸기.
- `async_mode='threading'` 또는 `'eventlet'` — APScheduler 와 호환 위해 보통 `threading`.

## 클라이언트 측 (참고)

```js
const socket = io();
socket.on("scopus.collect.progress", ({ done, total }) => {
    setProgress(done / total);
});
socket.emit("join", { room: `researcher:${myId}` });
```

## 함정

- emit 가 메인 작업 흐름을 막지 않게 — try/except 필수.
- 배치 한 번에 수천 emit ❌ — 10% 단위 또는 시간 기반(0.5s) throttle.
- production 에서 `eventlet` 사용 시 monkey_patch 가 SQLAlchemy 풀과 충돌 — 풀 size 1 권장.
- DB 트랜잭션 내부에서 emit ❌ — 커밋 후 emit.
