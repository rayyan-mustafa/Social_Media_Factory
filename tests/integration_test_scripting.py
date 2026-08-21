import asyncio
import os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from src.db import Base, Job
from src.workers.stages import StageContext
from src.workers.stages.scripting import ScriptingStage
from src.core.config import get_settings

async def main():
    # Setup test database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    settings = get_settings()
    # We must ensure there's an API key for the LLM
    if not settings.wavespeed_api_key:
        print("Warning: WAVESPEED_API_KEY is not set. The integration test may fail if it attempts to call the API.")

    # Create a test job
    job = Job(
        topic="How to make a viral YouTube short",
        niche="YouTube Growth",
        business_model="YouTube_Shorts",
        stage="queued",
        status="queued"
    )

    async with async_session() as session:
        session.add(job)
        await session.commit()
        await session.refresh(job)

    print(f"Created Job ID: {job.id}")

    # Set up StageContext
    work_dir = Path("work_dir_test")
    work_dir.mkdir(exist_ok=True)
    
    # We use a dummy storage for now or a real one if available.
    class DummyStorage:
        pass
        
    storage = DummyStorage()

    async with async_session() as session:
        ctx = StageContext(
            job=job,
            session=session,
            settings=settings,
            storage=storage,
            work_dir=work_dir,
            save_files=False  # We just want to test generation
        )

        stage = ScriptingStage()
        print("Executing ScriptingStage...")
        result = await stage.execute(ctx)
        
        print("\nScript Generation Result:")
        print("=" * 40)
        script = result["script"]
        print(f"Title: {script.title}")
        print(f"Description: {script.description}")
        print(f"Scenes count: {len(script.scenes)}")
        for scene in script.scenes:
            print(f"  Scene {scene.index}: {scene.text[:50]}... | Visual: {scene.visual_query}")
        
        await session.commit()
        await session.refresh(job)
        print("=" * 40)
        print(f"Job updated stage: {job.stage}")

if __name__ == "__main__":
    asyncio.run(main())
