"""Text production stage for blog posts and ebooks."""

import asyncio
import time
from pathlib import Path
from typing import Any

from fpdf import FPDF

from src.core.logging import get_logger
from src.core.paths import artifact_key
from src.db.repository import add_artifact, transition_stage, get_artifacts
from src.domain import ArtifactKind, JobStage, JobStatus, ScriptPayload
from src.utils.metrics import STAGE_DURATION
from src.workers.stages import StageContext
from src.workers.stages.routing import get_next_stage_for_job, should_skip_stage

logger = get_logger(__name__)

class TextProductionStage:
    """Produces Markdown and PDF documents from the script."""
    
    async def execute(self, ctx: StageContext, artifacts: dict[str, Any]) -> dict[str, Any]:
        if should_skip_stage(ctx.job.stage, JobStage.TEXT_PRODUCTION, ctx.business_model):
            logger.info("stage_text_production_skipped", extra={"job_id": ctx.job.id, "reason": f"stage is already {ctx.job.stage}"})
            # Reconstruct outputs
            db_artifacts = await get_artifacts(ctx.session, ctx.job.id)
            result = {}
            for db_art in db_artifacts:
                if db_art.kind in (ArtifactKind.TEXT.value,):
                    local_path = ctx.work_dir / db_art.s3_key.split("/")[-1]
                    if not local_path.exists():
                        ctx.storage.download_file(db_art.s3_key, local_path)
                    if local_path.suffix == ".md":
                        result["markdown_path"] = str(local_path)
                    elif local_path.suffix == ".pdf":
                        result["pdf_path"] = str(local_path)
            return result

        logger.info("stage_text_production_start", extra={"job_id": ctx.job.id})
        
        try:
            t0 = time.perf_counter()
            
            # 1. Load Prerequisites
            script: ScriptPayload | None = artifacts.get("script")
            if not script and ctx.job.script_json:
                script = ScriptPayload(**ctx.job.script_json)

            if not script:
                raise ValueError("TextProduction stage requires 'script' artifact")

            title = script.title or ctx.job.topic
            
            doc_dir = ctx.work_dir / "documents"
            doc_dir.mkdir(parents=True, exist_ok=True)
            
            md_path = doc_dir / "content.md"
            pdf_path = doc_dir / "content.pdf"
            
            # 2. Generate Markdown
            md_content = [f"# {title}\n"]
            for scene in script.scenes:
                md_content.append(f"{scene.text}\n")
                
            md_text = "\n".join(md_content)
            md_path.write_text(md_text, encoding="utf-8")
            
            # 2b. Generate PDF
            try:
                await asyncio.to_thread(self._generate_pdf, title, script.scenes, pdf_path)
            except Exception as e:
                logger.error("pdf_generation_failed", extra={"error": str(e)})
                
            # 3. Persist Artifacts
            if ctx.save_files:
                md_key = artifact_key(ctx.business_model, ctx.job.id, "content.md")
                ctx.storage.upload_file(md_path, md_key, content_type="text/markdown")
                await add_artifact(
                    ctx.session,
                    ctx.job.id,
                    ArtifactKind.TEXT,
                    md_key,
                )
                
                if pdf_path.exists():
                    pdf_key = artifact_key(ctx.business_model, ctx.job.id, "content.pdf")
                    ctx.storage.upload_file(pdf_path, pdf_key, content_type="application/pdf")
                    await add_artifact(
                        ctx.session,
                        ctx.job.id,
                        ArtifactKind.TEXT,
                        pdf_key,
                    )

            STAGE_DURATION.labels(stage="text_production").observe(time.perf_counter() - t0)
            
            # 4. Format-Aware Transition
            next_stg = get_next_stage_for_job(ctx.business_model, JobStage.TEXT_PRODUCTION)
            if next_stg is None:
                raise RuntimeError(f"Routing bug: Job {ctx.job.id} at stage {JobStage.TEXT_PRODUCTION} has no next stage in route.")
                
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=next_stg, 
                status=JobStatus.RUNNING, 
                message="Text Production complete."
            )
            logger.info("stage_text_production_complete", extra={"job_id": ctx.job.id})
            
            result = {"markdown_path": str(md_path)}
            if pdf_path.exists():
                result["pdf_path"] = str(pdf_path)
                
            return result

        except Exception as e:
            logger.exception("stage_text_production_failed", extra={"job_id": ctx.job.id})
            await transition_stage(
                ctx.session, ctx.job, 
                to_stage=ctx.job.stage,
                status=JobStatus.FAILED, 
                message=f"TextProduction error: {str(e)}"
            )
            raise
            
    def _generate_pdf(self, title: str, scenes: list, output_path: Path):
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=16)
        pdf.cell(200, 10, txt=title.encode("latin-1", "replace").decode("latin-1"), ln=True, align='C')
        pdf.ln(10)
        
        pdf.set_font("Helvetica", size=12)
        for scene in scenes:
            clean_text = scene.text.encode("latin-1", "replace").decode("latin-1")
            pdf.multi_cell(0, 10, txt=clean_text)
            pdf.ln(5)
            
        pdf.output(str(output_path))
