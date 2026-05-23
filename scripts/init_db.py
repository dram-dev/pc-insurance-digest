"""One-shot DB initializer. Equivalent to `digest init-db`."""
from digest import db
from digest.config import settings

if __name__ == "__main__":
    db.init_db()
    print(f"DB initialized at {settings.db_path}")
