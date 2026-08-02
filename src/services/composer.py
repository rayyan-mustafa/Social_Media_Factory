"""FFmpeg scene composition (Ken Burns / zoompan) and concat."""

from __future__ import annotations

import subprocess
from pathlib import Path

import librosa

from src.core.logging import get_logger
from src.services.avatar_animator import AudioReactiveAvatar

logger = get_logger(__name__)


class VideoComposer:
    def __init__(self):
        self.avatar_animator = AudioReactiveAvatar(fps=25)

    def create_avatar_scene(
        self,
        audio_path: Path,
        closed_img: Path,
        half_img: Path,
        open_img: Path,
        output_path: Path
    ) -> Path:
        """Renders an audio-reactive lip-sync avatar scene."""
        return self.avatar_animator.render_avatar_video(
            audio_path=audio_path,
            closed_img=closed_img,
            half_img=half_img,
            open_img=open_img,
            output_mp4=output_path
        )

    def create_scene_video(
        self,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        width: int = 1280,
        height: int = 720,
        fps: int = 25,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration = float(librosa.get_duration(path=str(audio_path)))
        frames = max(int(duration * fps), fps)
        vf = (
            f"scale={width}:{height},"
            f"zoompan=z='min(zoom+0.001,1.2)':d={frames}:s={width}x{height}:fps={fps}"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-i",
            str(audio_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-tune",
            "stillimage",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            str(duration),
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("ffmpeg_scene_failed", extra={"stderr": result.stderr[-2000:]})
            raise RuntimeError(f"FFmpeg scene render failed: {result.stderr[-500:]}")
        return output_path

    def concatenate(self, scene_paths: list[Path], output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        list_file = output_path.parent / "concat_list.txt"
        lines = [f"file '{p.resolve().as_posix()}'" for p in scene_paths]
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Re-encode fallback if stream copy fails
            cmd[-3] = "libx264"
            # rebuild properly
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg concat failed: {result.stderr[-500:]}")
        logger.info("compose_done", extra={"output": str(output_path), "scenes": len(scene_paths)})
        return output_path
