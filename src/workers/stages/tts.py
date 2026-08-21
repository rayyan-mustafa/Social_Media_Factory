"""TTS stage of the production pipeline."""

import time

from src.core.logging import get_logger
from src.core.paths import artifact_key
from src.db.repository import add_artifact, transition_stage
from src.domain import ArtifactKind, JobStage, JobStatus, ScriptPayload
from src.services.tts_kokoro import KokoroTTSService
from src.utils.metrics import STAGE_DURATION
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage
from src.services.financial.cost_tracker import record_cost, calculate_tts_cost

logger = get_logger(__name__)

class TTSStage:
    """Synthesizes speech audio for each scene in the script."""

    async def execute(self, ctx: StageContext, artifacts: dict) -> dict:
        if should_skip_stage(ctx.job.stage, JobStage.TTS, ctx.business_model):
            logger.info("stage_tts_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            return {}

        logger.info("stage_tts_start", extra={"job_id": ctx.job.id})

        try:
            t0 = time.perf_counter()
            tts = KokoroTTSService()
            audio_paths = []

            # 1. Load Prerequisites
            script: ScriptPayload | None = artifacts.get("script")
            if not script and ctx.job.script_json:
                script = ScriptPayload(**ctx.job.script_json)

            if not script:
                raise ValueError("TTS stage requires 'script' artifact")

            # 2. Execute Business Logic
            for scene in script.scenes:
                aud_path = ctx.work_dir / f"scene_{scene.index}.wav"
                try:
                    tts.synthesize(scene.text, aud_path)
                except Exception:
                    logger.exception("kokoro_unavailable_silence")
                    tts.synthesize_silence(aud_path, seconds=3.0)
                
                # 3. Persist Artifacts
                if ctx.save_files:
                    key = artifact_key(
                        ctx.business_model, ctx.job.id, f"audio/scene_{scene.index}.wav"
                    )
                    checksum = ctx.storage.checksum_file(aud_path)
                    ctx.storage.upload_file(aud_path, key, content_type="audio/wav")
                    await add_artifact(
                        ctx.session,
                        ctx.job.id,
                        ArtifactKind.AUDIO,
                        key,
                        checksum=checksum,
                        meta={"scene": scene.index},
                    )
                
                audio_paths.append(aud_path)

            STAGE_DURATION.labels(stage="tts").observe(time.perf_counter() - t0)
            
            # Amortized TTS cost (e.g. 10 minutes)
            tts_cost = calculate_tts_cost(10 * 60)
            await record_cost(ctx.session, ctx.job.id, "kokoro_tts", tts_cost)
            
            # Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.TTS)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.TTS} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="TTS complete."
            )
            logger.info("stage_tts_complete", extra={"job_id": ctx.job.id})

            return {"audio_paths": audio_paths}

        except Exception as e:
            logger.exception("stage_tts_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,  # PRESERVE CURRENT STAGE FOR RETRY POINTER
                status=JobStatus.FAILED, 
                message=f"TTS error: {str(e)}"
            )
            raise
