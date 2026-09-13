"""관심 저자 검색·등록·추적 패키지.

저자는 이름이 아니라 OpenAlex Author ID로 식별한다.
  openalex.py : OpenAlex API 클라이언트 (검색/조회/논문 목록)
  service.py  : 로컬 DB 저장(upsert), 팔로우 관계, 신규 논문 추적
"""
