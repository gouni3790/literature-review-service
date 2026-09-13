"""연구실 온톨로지(TTL) 기반 LLM 챗 에이전트.

구조:
    1. 모듈 로드 시 TTL 두 파일(bist_ontology.ttl + bist_data.ttl)을 읽어둠
    2. 사용자 질문 → Claude Haiku 4.5 호출
    3. 시스템 프롬프트 = 스키마 + 데이터 (cache_control로 캐싱하여 재호출 비용 ↓)

사용 모델: claude-haiku-4-5 — 빠르고 저렴.
spec("LLM은 core 논문 요약에만") 예외 — 사용자 명시 요청으로 챗에도 도입.
"""
from __future__ import annotations

import logging
import os
from threading import Lock

import anthropic
from flask import current_app

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1024

# TTL 파일 경로 — literature_v2/ontology/
_HERE = os.path.dirname(os.path.abspath(__file__))
_ONTO_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "ontology"))
SCHEMA_PATH = os.path.join(_ONTO_DIR, "bist_ontology.ttl")
DATA_PATH = os.path.join(_ONTO_DIR, "bist_data.ttl")

_ttl_cache: dict[str, str] = {}
_ttl_lock = Lock()


def _load_ttl() -> tuple[str, str]:
    """TTL 두 파일을 읽어 메모리에 캐싱. 동일 프로세스에서는 1회만 로드."""
    with _ttl_lock:
        if "schema" in _ttl_cache and "data" in _ttl_cache:
            return _ttl_cache["schema"], _ttl_cache["data"]
        try:
            with open(SCHEMA_PATH, encoding="utf-8") as f:
                _ttl_cache["schema"] = f.read()
            with open(DATA_PATH, encoding="utf-8") as f:
                _ttl_cache["data"] = f.read()
            logger.info(
                "TTL loaded: schema=%d chars, data=%d chars",
                len(_ttl_cache["schema"]), len(_ttl_cache["data"]),
            )
        except FileNotFoundError as e:
            logger.exception("TTL file missing: %s", e)
            _ttl_cache["schema"] = ""
            _ttl_cache["data"] = ""
        return _ttl_cache["schema"], _ttl_cache["data"]


SYSTEM_INSTRUCTION = """당신은 BIST Lab의 연구실 온톨로지를 읽고 한국어로 답하는 어시스턴트입니다.

다음 두 개의 Turtle(TTL) 그래프가 주어집니다:
- 스키마: 클래스(ResearchLab, Researcher, ResearchGroup, ResearchKeyword, KeywordUsage)와 관계(hasMember, inGroup, hasKeyword, usageOf, usageBy, frequency)를 정의.
- 데이터: BIST Lab의 실제 인스턴스(연구원 17명, 그룹 5개, 키워드 372개, KeywordUsage 빈도).

답변 원칙:
- **모든 출력은 한국어로 작성합니다.** 전문 용어는 한국어 + 괄호 영어 표기 허용 (예: "디지털 트윈(Digital twin)").
- **TTL에 있는 사실만으로 답하세요.** 추측·일반 지식 사용 금지. TTL에 정보가 없으면 "그래프에 해당 정보가 없습니다"라고 답하세요.
- **연구원 이름**은 가능하면 한국어 + 괄호 영문 (예: "구자범 (Koo Jabeom)") 형태로 표기. TTL의 foaf:name 값을 그대로 사용해도 됩니다.
- **빈도/숫자**는 KeywordUsage의 bist:frequency를 활용. "X편" 단위로 표기.
- **간결하게.** 챗 박스에 들어갈 짧은 답변(3~10줄). 표/리스트는 글머리 기호(•) 사용. 마크다운 굵게(**)·인라인코드(`) 가능.
- 질문이 모호하면 가장 합리적인 해석으로 답하되, 다른 해석 가능성을 마지막에 한 줄로 덧붙이세요.

자주 들어올 질문 예시:
- "디지털 트윈 하는 사람" → keywordRaw가 "Digital twin"/"Digital twins" 등인 키워드를 사용한 연구원 + 빈도 정렬
- "구자범의 키워드" → 해당 연구원의 KeywordUsage 빈도 상위 N개
- "구자범과 이제윤의 공통 키워드" → 두 연구원이 모두 hasKeyword 관계로 연결된 키워드
- "BIST 전체 키워드" → 모든 연구원의 KeywordUsage frequency 합산 상위 N개
- "구자범과 비슷한 사람" → 구자범의 키워드 집합과 가장 많이 겹치는 다른 연구원
"""


def _build_system_blocks(schema: str, data: str) -> list[dict]:
    """system 프롬프트 블록 — 큰 TTL 부분에 cache_control 적용."""
    blocks = [{
        "type": "text",
        "text": SYSTEM_INSTRUCTION,
    }]
    if schema:
        blocks.append({
            "type": "text",
            "text": f"# Schema TTL (bist_ontology.ttl)\n\n```turtle\n{schema}\n```",
        })
    if data:
        # 가장 큰 블록(170KB) — 자주 안 바뀌므로 캐싱
        blocks.append({
            "type": "text",
            "text": f"# Data TTL (bist_data.ttl)\n\n```turtle\n{data}\n```",
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


def _client() -> anthropic.Anthropic:
    api_key = current_app.config.get("ANTHROPIC_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY", ""
    )
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=api_key)


def answer(message: str) -> dict:
    """사용자 메시지 → LLM 응답.

    Returns:
        {"answer": str, "intent": "llm", "data": {"usage": {...}}}
    """
    text = (message or "").strip()
    if not text:
        return {"answer": "질문을 입력해 주세요.", "intent": "empty", "data": {}}

    schema, data = _load_ttl()
    if not schema or not data:
        return {
            "answer": "온톨로지 파일을 찾을 수 없습니다. `literature_v2/ontology/`를 확인해 주세요.",
            "intent": "error",
            "data": {},
        }

    try:
        client = _client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_build_system_blocks(schema, data),
            messages=[{"role": "user", "content": text}],
        )
        out = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                out += block.text

        usage = {}
        if hasattr(resp, "usage"):
            u = resp.usage
            usage = {
                "input_tokens": getattr(u, "input_tokens", None),
                "output_tokens": getattr(u, "output_tokens", None),
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
            }
        logger.info("LabAgent LLM usage: %s", usage)

        return {"answer": out.strip() or "(응답 없음)", "intent": "llm",
                "data": {"usage": usage, "model": MODEL}}

    except anthropic.APIError as e:
        logger.exception("Claude API failed for lab agent")
        return {
            "answer": f"LLM 호출에 실패했습니다: {type(e).__name__}",
            "intent": "error",
            "data": {},
        }
    except Exception as e:
        logger.exception("Unexpected error in lab agent")
        return {
            "answer": "응답 생성 중 예기치 못한 오류가 발생했습니다.",
            "intent": "error",
            "data": {},
        }
