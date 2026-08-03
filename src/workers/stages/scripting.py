"""Scripting stage of the production pipeline."""

import time
from typing import Any

from src.core.logging import get_logger
from src.core.paths import artifact_key
from src.db.repository import add_artifact, transition_stage
from src.domain import ArtifactKind, JobStage, JobStatus, ScriptPayload
from src.services.script_generator import ScriptGenerator
from src.utils.metrics import STAGE_DURATION
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage
from src.services.financial.cost_tracker import record_cost, calculate_llm_cost

logger = get_logger(__name__)

class ScriptingStage:
    """Generates the content script based on topic, niche, and business model."""

    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict:
        if should_skip_stage(ctx.job.stage, JobStage.SCRIPTING, ctx.business_model):
            logger.info("stage_scripting_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            # Reconstruct the output payload for downstream stages
            script = ScriptPayload(**ctx.job.script_json) if ctx.job.script_json else None
            return {"script": script} if script else {}

        logger.info("stage_scripting_start", extra={"job_id": ctx.job.id})

        try:
            t0 = time.perf_counter()
            generator = ScriptGenerator()
            
            if ctx.settings.wavespeed_api_key:
                script = generator.generate(
                    ctx.job.topic, ctx.job.niche, ctx.business_model
                )
            else:
                logger.warning("llm_key_missing_using_fallback")
                script = generator.generate_fallback(
                    ctx.job.topic, ctx.job.niche, ctx.business_model
                )
                
            # Save to database record
            ctx.job.script_json = script.model_dump()
            await ctx.session.commit()

            # Save to object storage if required by this business model
            if ctx.save_files:
                script_key = artifact_key(ctx.business_model, ctx.job.id, "script.json")
                ctx.storage.upload_bytes(
                    script.model_dump_json(indent=2).encode("utf-8"),
                    script_key,
                    content_type="application/json",
                )
                await add_artifact(
                    ctx.session,
                    ctx.job.id,
                    ArtifactKind.SCRIPT,
                    script_key,
                    meta={
                        "scenes": len(script.scenes) if script.scenes else 0,
                        "model": ctx.business_model,
                    },
                )

            STAGE_DURATION.labels(stage="scripting").observe(time.perf_counter() - t0)
            # Assuming 1000 prompt tokens and 500 completion tokens on average for now
            llm_cost = calculate_llm_cost("gpt-4o", 1000, 500)
            await record_cost(ctx.session, ctx.job.id, "llm", llm_cost)
            
            # 2. Terminal Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.SCRIPTING)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.SCRIPTING} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="Scripting complete."
            )
            logger.info("stage_scripting_complete", extra={"job_id": ctx.job.id})

            return {"script": script}
            
        except Exception as e:
            logger.exception("stage_scripting_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,  # PRESERVE CURRENT STAGE FOR RETRY POINTER
                status=JobStatus.FAILED, 
                message=f"Scripting error: {str(e)}"
            )
            raise
