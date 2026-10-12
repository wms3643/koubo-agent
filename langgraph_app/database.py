"""运行存储管理入口：python -m langgraph_app.database init|check。"""

import argparse

from .config import AppConfig
from .storage import check_database, initialize_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage PostgreSQL runtime storage")
    parser.add_argument("action", choices=("init", "check"))
    args = parser.parse_args()
    config = AppConfig.from_env()
    if not config.database_url:
        parser.error("set DATABASE_URL before using database commands")
    if args.action == "init":
        initialize_database(config)
    check_database(config)
    print(f"PostgreSQL {args.action}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
