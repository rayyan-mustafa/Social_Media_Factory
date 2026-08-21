"""Audio production stage for podcasts and audiobooks."""

import asyncio
import time
from pathlib import Path
from typing import Any

import soundfile as sf
import numpy as np

from src.core.logging import get_logger
from src.core.paths import artifact_key
from src.db.repository import add_artifact, transition_stage, get_artifacts
from src.domain import ArtifactKind, JobStage, JobStatus
from src.utils.metrics import STAGE_DURATION
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage

logger = get_logger(__name__)

class AudioProductionStage:
    """Concatenates individual scene audio files into a single master audio file."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        if should_skip_stage(ctx.job.stage, JobStage.AUDIO_PRODUCTION, ctx.business_model):
            logger.info("stage_audio_production_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            # Reconstruct outputs
            db_artifacts = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.PODCAST_AUDIO)
            if db_artifacts:
                local_path = ctx.work_dir / db_artifacts[0].s3_key.split("/")[-1]
                if not local_path.exists():
                    ctx.storage.download_file(db_artifacts[0].s3_key, local_path)
                return {"master_audio_path": str(local_path)}
            return {}

        logger.info("stage_audio_production_start", extra={"job_id": ctx.job.id})
        
        try:
            t0 = time.perf_counter()
            
            # 1. Load Prerequisites
            audio_paths = artifacts.get("audio_paths", [])
            if not audio_paths:
                db_audio = await get_artifacts(ctx.session, ctx.job.id, ArtifactKind.AUDIO)
                for db_art in db_audio:
                    local_path = ctx.work_dir / db_art.s3_key.split("/")[-1]
                    if not local_path.exists():
                        ctx.storage.download_file(db_art.s3_key, local_path)
                    audio_paths.append(local_path)
                audio_paths.sort()
                
            if not audio_paths:
                raise ValueError("AudioProduction stage requires audio artifacts.")
                
            # 2. Execute Business Logic
            audio_dir = ctx.work_dir / "master_audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            master_path = audio_dir / "final.wav"
            
            try:
                await asyncio.to_thread(self._concatenate_audio, audio_paths, master_path)
            except Exception as e:
                logger.error("audio_concat_failed", extra={"error": str(e)})
                raise RuntimeError(f"Failed to concatenate audio files: {e}")
                
            # 3. Persist Artifacts
            if ctx.save_files:
                key = artifact_key(ctx.business_model, ctx.job.id, "final.wav")
                checksum = ctx.storage.checksum_file(master_path)
                ctx.storage.upload_file(master_path, key, content_type="audio/wav")
                await add_artifact(
                    ctx.session,
                    ctx.job.id,
                    ArtifactKind.PODCAST_AUDIO,
                    key,
                    checksum=checksum,
                )

            STAGE_DURATION.labels(stage="audio_production").observe(time.perf_counter() - t0)
            
            # 4. Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.AUDIO_PRODUCTION)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.AUDIO_PRODUCTION} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="Audio Production complete."
            )
            logger.info("stage_audio_production_complete", extra={"job_id": ctx.job.id})
            
            return {"master_audio_path": str(master_path)}

        except Exception as e:
            logger.exception("stage_audio_production_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,
                status=JobStatus.FAILED, 
                message=f"AudioProduction error: {str(e)}"
            )
            raise

    def _concatenate_audio(self, audio_paths: list[Path] | list[str], output_path: Path):
        """Concatenate multiple wav files into one using soundfile."""
        all_data = []
        samplerate = None
        
        for path in audio_paths:
            data, sr = sf.read(str(path))
            if samplerate is None:
                samplerate = sr
            elif sr != samplerate:
                logger.warning("audio_samplerate_mismatch", extra={"expected": samplerate, "got": sr, "file": str(path)})
                
            all_data.append(data)
            
        if not all_data:
            raise ValueError("No valid audio data found to concatenate")
            
        master_data = np.concatenate(all_data, axis=0)
        sf.write(str(output_path), master_data, samplerate)
