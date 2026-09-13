"""로컬 개발용 실행 스크립트.

운영 서버에서는 Web_server.py(GPT-based-building-simulator-master)가 이 앱을
블루프린트로 통합해 실행하지만, 로컬에서는 이 파일 하나로 단독 실행한다.

    python run_local.py                # http://localhost:8000
    python run_local.py --port 5000
    python run_local.py --with-scheduler   # 주간 배치 스케줄러까지 시작 (주의!)

주의: --with-scheduler 를 켜면 운영과 동일한 배치(Scopus 수집, Claude 요약,
이메일 발송)가 예정 시각에 실제로 실행된다. 로컬 개발 중에는 끄는 것을 권장.
"""

import argparse

from app import create_app, socketio


def main() -> None:
    parser = argparse.ArgumentParser(description="Literature Review Assistant (local)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--with-scheduler",
        action="store_true",
        help="APScheduler 배치 잡 시작 (이메일 발송 등 실제 동작 주의)",
    )
    args = parser.parse_args()

    app = create_app("development")

    if args.with_scheduler:
        from app.litreview.scheduler.jobs import init_scheduler

        init_scheduler(app)

    print(f"* Literature Review Assistant: http://{args.host}:{args.port}/home")
    socketio.run(
        app,
        host=args.host,
        port=args.port,
        debug=True,
        use_reloader=False,
        # 로컬 개발 전용 실행이므로 Werkzeug 서버 사용을 허용
        allow_unsafe_werkzeug=True,
    )


if __name__ == "__main__":
    main()
