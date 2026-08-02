"""Audio-Reactive Avatar Animator (Zero-Budget Lip Sync).

Uses librosa to extract volume envelopes from an audio file and 
dynamically swaps avatar mouth frames via FFmpeg concat demuxer.
"""

import subprocess
from pathlib import Path
import librosa
import numpy as np

from src.core.logging import get_logger

logger = get_logger(__name__)

class AudioReactiveAvatar:
    def __init__(self, fps: int = 25):
        self.fps = fps

    def analyze_audio_volume(self, audio_path: Path) -> np.ndarray:
        """Extracts the RMS volume envelope from the audio."""
        logger.info("avatar_analyzing_audio", extra={"audio_path": str(audio_path)})
        y, sr = librosa.load(str(audio_path), sr=None)
        
        # Calculate hop length to match target FPS
        hop_length = sr // self.fps
        
        # Compute RMS energy
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
        
        # Normalize RMS to 0.0 - 1.0
        if np.max(rms) > 0:
            rms_norm = rms / np.max(rms)
        else:
            rms_norm = rms
            
        return rms_norm

    def build_concat_list(self, 
                          rms_norm: np.ndarray, 
                          closed_img: Path, 
                          half_img: Path, 
                          open_img: Path, 
                          output_txt: Path):
        """Builds the FFmpeg concat demuxer file based on volume thresholds."""
        duration_per_frame = 1.0 / self.fps
        lines = []
        
        for volume in rms_norm:
            # Map volume thresholds to mouth states
            if volume < 0.1:
                img_path = closed_img
            elif volume < 0.35:
                img_path = half_img
            else:
                img_path = open_img
                
            lines.append(f"file '{img_path.resolve().as_posix()}'")
            lines.append(f"duration {duration_per_frame:.3f}")
            
        # FFmpeg requires the last file to be repeated without duration
        if lines:
            # Re-append the last file path
            last_file = lines[-2] 
            lines.append(last_file)
            
        output_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_txt

    def render_avatar_video(self, 
                            audio_path: Path, 
                            closed_img: Path, 
                            half_img: Path, 
                            open_img: Path, 
                            output_mp4: Path) -> Path:
        """Fully renders the lip-synced avatar video using FFmpeg."""
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        
        # 1. Analyze Audio
        rms_norm = self.analyze_audio_volume(audio_path)
        
        # 2. Build FFmpeg Concat List
        concat_txt = output_mp4.parent / "avatar_concat.txt"
        self.build_concat_list(rms_norm, closed_img, half_img, open_img, concat_txt)
        
        # 3. Render via FFmpeg
        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_txt),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output_mp4)
        ]
        
        logger.info("avatar_render_start", extra={"cmd": " ".join(cmd)})
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error("ffmpeg_avatar_failed", extra={"stderr": result.stderr[-2000:]})
            raise RuntimeError(f"FFmpeg avatar render failed: {result.stderr[-500:]}")
            
        logger.info("avatar_render_success", extra={"output": str(output_mp4)})
        return output_mp4

if __name__ == "__main__":
    # Test script stub
    animator = AudioReactiveAvatar()
    print("AudioReactiveAvatar initialized.")
