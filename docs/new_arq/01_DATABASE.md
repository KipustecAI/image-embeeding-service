# Step 1: PostgreSQL + SQLAlchemy + Alembic Setup

## Objective

Add async PostgreSQL as the local persistence layer. This is the foundation everything else builds on.

## New Dependencies

```
# Add to requirements.txt
SQLAlchemy>=2.0.0
asyncpg                    # Async PostgreSQL driver
alembic                    # Schema migrations
psycopg2-binary            # Sync driver (for Alembic migrations only)
```

## Database Module

**New file:** `src/infrastructure/database.py`

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from contextlib import asynccontextmanager

engine = create_async_engine(
    settings.database_url,                # postgresql+asyncpg://...
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,                   # Detect dead connections
    pool_recycle=120,                     # Recycle before idle timeout
    pool_timeout=30,
    connect_args={
        "command_timeout": 30,
        "server_settings": {"statement_timeout": "30000"},
    },
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)
```

**Session context manager** (matches deepface pattern):

```python
@asynccontextmanager
async def get_session():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
```

**FastAPI dependency:**

```python
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
```

## Base Model

**New file:** `src/db/base.py`

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

## Alembic Setup

```bash
# Initialize (creates alembic/ directory + alembic.ini)
alembic init alembic
```

**Edit `alembic/env.py`:**

```python
from src.db.base import Base
from src.db.models import embedding_request, evidence_embedding, search_request  # Import all models
from src.infrastructure.config import get_settings

settings = get_settings()
target_metadata = Base.metadata

def run_migrations_online():
    # Convert async URL to sync for Alembic
    sync_url = settings.database_url.replace("postgresql+asyncpg", "postgresql")
    configuration["sqlalchemy.url"] = sync_url
    # ... standard Alembic pattern
```

## Config Additions

**Add to `src/infrastructure/config.py`:**

```python
# Database
database_url: str = "postgresql+asyncpg://embed_user:embed_pass@localhost:5432/embedding_service"
```

**Add to `.env`:**

```env
DATABASE_URL=postgresql+asyncpg://embed_user:embed_pass@localhost:5432/embedding_service
```

## Docker Compose Addition

```yaml
services:
  postgres:
    image: postgres:16-alpine
    container_name: embedding-postgres
    ports:
      - "5433:5432"          # 5433 to avoid conflict with other services
    environment:
      POSTGRES_DB: embedding_service
      POSTGRES_USER: embed_user
      POSTGRES_PASSWORD: embed_pass
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - embedding-network

volumes:
  postgres_data:
```

## File Structure After This Step

```
src/
├── db/
│   ├── __init__.py
│   ├── base.py              # DeclarativeBase
│   └── models/
│       └── __init__.py      # (empty, populated in Step 2)
├── infrastructure/
│   ├── database.py          # Engine, session factory, get_session()
│   └── config.py            # + DATABASE_URL
alembic/
├── env.py                   # Configured for async URL swap
├── versions/                # Migration files
alembic.ini
```

## Validation

```bash
# Start PostgreSQL
make docker-up

# Run first migration (after Step 2 creates models)
alembic upgrade head

# Verify connection
python -c "
import asyncio
from src.infrastructure.database import engine
async def check():
    async with engine.connect() as conn:
        result = await conn.execute(text('SELECT 1'))
        print('DB connected:', result.scalar())
asyncio.run(check())
"
```
