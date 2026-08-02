import subprocess, sys, os

def install_deps():
    print("📦 Installing dependencies... this may take a minute.")
    pkgs = ["kokoro>=0.9.2", "soundfile", "openai", "librosa", "chromadb", "sentence-transformers", "ftfy", "git+https://github.com/openai/CLIP.git"]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + pkgs, check=True)
    subprocess.run(["apt-get", "-qq", "-y", "install", "espeak-ng", "ffmpeg"], check=True)
    print("✅ Environment Ready.")

try:
    import clip
    import chromadb
    import kokoro
except ImportError:
    install_deps()
    import clip
    import chromadb
    import kokoro

import asyncio, aiohttp, requests, torch, uuid, json, librosa, logging
import numpy as np
import soundfile as sf
from PIL import Image
from io import BytesIO
from openai import OpenAI
from kokoro import KPipeline

from chromadb.utils import embedding_functions
import nest_asyncio

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)

# --- CONFIG ---
BASE_PATH = '/content/drive/MyDrive/VideoFactory'
PATHS = {k: f"{BASE_PATH}/{v}" for k, v in {"audio": "audio", "images": "images", "video": "video", "cache": "cache"}.items()}
for p in PATHS.values(): os.makedirs(p, exist_ok=True)

wavespeed_key = os.environ.get('WAVESPEED_API_KEY')
client = OpenAI(api_key=wavespeed_key, base_url="https://llm.wavespeed.ai/v1")
MODEL_NAME = "google/gemini-2.5-flash"

device = "cuda" if torch.cuda.is_available() else "cpu"
clip_model, clip_preprocess = clip.load("ViT-B/32", device=device)
chroma_client = chromadb.PersistentClient(path=PATHS['cache'])
embedding_func = embedding_functions.SentenceTransformerEmbeddingFunction(model_name='all-MiniLM-L6-v2')
collection = chroma_client.get_or_create_collection(name="mega_v4", embedding_function=embedding_func)

# --- ENGINE LOGIC ---
async def fetch_media(query, context, limit=5):
    res = collection.query(query_texts=[query], n_results=1)
    if res['ids'][0] and res['distances'][0][0] < 0.15: return res['metadatas'][0][0]['url']

    async with aiohttp.ClientSession() as session:
        url = "https://commons.wikimedia.org/w/api.php"
        params = {"action":"query","format":"json","generator":"search","gsrsearch":f"filetype:bitmap {query}","gsrlimit":limit,"prop":"imageinfo","iiprop":"url"}
        async with session.get(url, params=params) as r:
            data = await r.json()
            pages = data.get("query", {}).get("pages", {})
            candidates = [v['imageinfo'][0]['url'] for k,v in pages.items() if 'imageinfo' in v]

    if not candidates: return None

    scored = []
    txt = clip.tokenize([query]).to(device)
    for c_url in candidates:
        try:
            img_r = requests.get(c_url, timeout=5, headers={'User-Agent': 'VideoFactory/1.0'})
            img = clip_preprocess(Image.open(BytesIO(img_r.content)).convert("RGB")).unsqueeze(0).to(device)
            with torch.no_grad():
                score = torch.cosine_similarity(clip_model.encode_image(img), clip_model.encode_text(txt)).item()
                scored.append({"url": c_url, "score": score})
        except: continue

    best = sorted(scored, key=lambda x: x['score'], reverse=True)[0]['url'] if scored else candidates[0]
    collection.add(ids=[str(uuid.uuid4())], documents=[query], metadatas=[{"url": best}])
    return best

async def produce_video(topic, niche):
    if True:
        print(f"🎬 Launching {niche} Production: {topic}")
        prompt = f"Write a 3-scene documentary script about {topic} for a {niche} niche. Separate scenes with [SCENE]."
        resp = client.chat.completions.create(model=MODEL_NAME, messages=[{"role":"user","content":prompt}])
        scenes = [s.strip() for s in resp.choices[0].message.content.split("[SCENE]") if len(s.strip()) > 10][:3]

        tts = KPipeline(lang_code='a')
        video_segments = []
        for i, text in enumerate(scenes):
            print(f"   ⚙️ Scene {i+1}...")
            aud_p, img_p, vid_p = f"s_{i}.wav", f"s_{i}.jpg", f"s_{i}.mp4"
            img_url = await fetch_media(topic, topic)
            sf.write(aud_p, np.concatenate([s for _,_,s in tts(text, voice='am_michael')]), 24000)
            with open(img_p, 'wb') as f: f.write(requests.get(img_url).content if img_url else Image.new('RGB', (1280,720)).tobytes())
            dur = librosa.get_duration(path=aud_p)
            subprocess.run(["ffmpeg","-y","-loop","1","-i",img_p,"-i",aud_p,"-vf",f"scale=1280:720,zoompan=z='min(zoom+0.001,1.2)':d={int(dur*25)}:s=1280x720","-c:v","libx264","-t",str(dur),"-pix_fmt","yuv420p",vid_p], capture_output=True)
            video_segments.append(vid_p)

        final_name = f"{topic.replace(' ','_')}_Final.mp4"
        with open("list.txt", "w") as f:
            for v in video_segments: f.write(f"file '{os.path.abspath(v)}'\n")
        subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i","list.txt","-c","copy", final_name], capture_output=True)
        print(f"\n🏆 SUCCESS: {final_name} generated.")


import os, sys, subprocess

def setup_mega_env():
    print("📦 Installing Mega-Pipeline dependencies (Gradio, Kokoro, CLIP, etc.)...")
    pkgs = [
        "gradio", "kokoro>=0.9.2", "soundfile", "openai", "librosa",
        "chromadb", "sentence-transformers", "ftfy",
        "git+https://github.com/openai/CLIP.git"
    ]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + pkgs, check=True)
    subprocess.run(["apt-get", "-qq", "-y", "install", "espeak-ng", "ffmpeg"], check=True)
    print("✅ Environment Ready.")

setup_mega_env()

import asyncio, aiohttp, requests, torch, clip, numpy as np, soundfile as sf, uuid, librosa, gradio as gr
from PIL import Image
from io import BytesIO
from openai import OpenAI
from kokoro import KPipeline

import chromadb
from chromadb.utils import embedding_functions
import nest_asyncio

nest_asyncio.apply()

# --- 1. CONFIGURATION ---
BASE_PATH = '/content/drive/MyDrive/VideoFactory'
PATHS = {k: f"{BASE_PATH}/{v}" for k, v in {"audio": "audio", "images": "images", "video": "video", "cache": "cache"}.items()}
for p in PATHS.values(): os.makedirs(p, exist_ok=True)

# API Setup
wavespeed_key = os.environ.get('WAVESPEED_API_KEY')
client = OpenAI(api_key=wavespeed_key, base_url="https://llm.wavespeed.ai/v1")
MODEL_NAME = "google/gemini-2.5-flash"

# CLIP & Vector Cache
device = "cuda" if torch.cuda.is_available() else "cpu"
clip_model, clip_preprocess = clip.load("ViT-B/32", device=device)
collection = chromadb.PersistentClient(path=PATHS['cache']).get_or_create_collection(
    name="mega_v3",
    embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name='all-MiniLM-L6-v2')
)

# --- 2. ENGINE LOGIC ---
async def fetch_media(query, limit=5):
    async with aiohttp.ClientSession() as session:
        wiki_url = "https://commons.wikimedia.org/w/api.php"
        wiki_params = {"action":"query","format":"json","generator":"search","gsrsearch":f"filetype:bitmap {query}","gsrlimit":limit,"prop":"imageinfo","iiprop":"url"}
        met_search = f"https://collectionapi.metmuseum.org/public/collection/v1/search?hasImages=true&q={query}"

        async def get_wiki():
            async with session.get(wiki_url, params=wiki_params) as r:
                data = await r.json()
                pages = data.get("query", {}).get("pages", {})
                return [{"url": v['imageinfo'][0]['url']} for k,v in pages.items() if 'imageinfo' in v]

        async def get_met():
            async with session.get(met_search) as r:
                ids = (await r.json()).get("objectIDs", [])[:limit]
                met_res = []
                for oid in ids:
                    async with session.get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}") as orer:
                        odata = await orer.json()
                        if odata.get("primaryImage"): met_res.append({"url": odata["primaryImage"]})
                return met_res

        res = await asyncio.gather(get_wiki(), get_met())
        candidates = [item for sub in res for item in sub]
        if not candidates: return None

        scored = []
        txt = clip.tokenize([query]).to(device)
        for c in candidates:
            try:
                img_r = requests.get(c['url'], timeout=5, headers={'User-Agent': 'MegaDoc/1.0'})
                img = clip_preprocess(Image.open(BytesIO(img_r.content)).convert("RGB")).unsqueeze(0).to(device)
                with torch.no_grad():
                    score = torch.cosine_similarity(clip_model.encode_image(img), clip_model.encode_text(txt)).item()
                    scored.append({"url": c['url'], "score": score})
            except: continue
        return sorted(scored, key=lambda x: x['score'], reverse=True)[0]['url'] if scored else candidates[0]['url']

async def produce_doc(topic, niche, progress=gr.Progress()):
    progress(0.1, desc="Writing Script...")
    prompt = f"Write a 3-scene documentary script about {topic} for a {niche} niche. Separate scenes with [SCENE]"
    resp = client.chat.completions.create(model=MODEL_NAME, messages=[{"role":"user","content":prompt}])
    scenes = [s.strip() for s in resp.choices[0].message.content.split("[SCENE]") if len(s.strip()) > 10][:3]

    tts = KPipeline(lang_code='a')
    vids = []
    for i, text in enumerate(scenes):
        progress((i+1)/4, desc=f"Processing Scene {i+1}...")
        aud_p, img_p, vid_p = f"aud_{i}.wav", f"img_{i}.jpg", f"vid_{i}.mp4"

        # Audio & Image
        sf.write(aud_p, np.concatenate([s for _,_,s in tts(text, voice='am_michael')]), 24000)
        url = await fetch_media(topic)
        with open(img_p, 'wb') as f: f.write(requests.get(url).content if url else Image.new('RGB', (1280,720)).tobytes())

        # FFmpeg
        dur = librosa.get_duration(path=aud_p)
        subprocess.run(["ffmpeg","-y","-loop","1","-i",img_p,"-i",aud_p,"-vf",f"scale=1280:720,zoompan=z='min(zoom+0.001,1.2)':d={int(dur*25)}:s=1280x720","-c:v","libx264","-t",str(dur),"-pix_fmt","yuv420p",vid_p], capture_output=True)
        vids.append(vid_p)

    progress(0.9, desc="Final Stitching...")
    out_p = f"{topic.replace(' ','_')}_Doc.mp4"
    with open("c.txt", "w") as f:
        for v in vids: f.write(f"file '{os.path.abspath(v)}'\n")
    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i","c.txt","-c","copy", out_p], capture_output=True)
    return out_p

# --- 3. INTERFACE ---
with gr.Blocks(title="Mega VideoFactory") as demo:
    gr.Markdown("# 🎬 Mega VideoFactory Dashboard\nOne-cell production using Gemini, Met API, Wikimedia, and Kokoro TTS.")
    with gr.Row():
        t = gr.Textbox(label="Topic", placeholder="e.g. The Viking Age")
        n = gr.Dropdown(["Testing", "Speculative Historian", "Napping Historian"], label="Niche", value="Testing")
    btn = gr.Button("🚀 Generate Documentary", variant="primary")
    out_vid = gr.Video(label="Final Output")
    btn.click(fn=produce_doc, inputs=[t, n], outputs=out_vid)

demo.launch(inline=True, share=True, debug=False)
print("✅ All Mega-Pipeline dependencies installed.")
import os, json, asyncio, aiohttp, requests, torch, clip, numpy as np, soundfile as sf, subprocess, uuid, librosa, gradio as gr
from PIL import Image
from io import BytesIO
from openai import OpenAI
from kokoro import KPipeline

import chromadb
from chromadb.utils import embedding_functions
import nest_asyncio

nest_asyncio.apply()

# --- 1. GLOBAL SETUP ---
BASE_PATH = '/content/drive/MyDrive/VideoFactory'
PROJECT_NAME = "MegaDoc_Production"
PATHS = {k: f"{BASE_PATH}/{v}" for k, v in {"audio": "audio", "images": "images", "video": "video", "cache": "image_vector_cache"}.items()}
for p in PATHS.values(): os.makedirs(p, exist_ok=True)

# API & Models
wavespeed_key = os.environ.get('WAVESPEED_API_KEY')
client = OpenAI(api_key=wavespeed_key, base_url="https://llm.wavespeed.ai/v1")
MODEL_NAME = "google/gemini-2.5-flash"

device = "cuda" if torch.cuda.is_available() else "cpu"
clip_model, clip_preprocess = clip.load("ViT-B/32", device=device)
collection = chromadb.PersistentClient(path=PATHS['cache']).get_or_create_collection(
    name="mega_cache",
    embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name='all-MiniLM-L6-v2')
)

# --- 2. LOGIC COMPONENTS ---
async def fetch_media(query, limit=5):
    async with aiohttp.ClientSession() as session:
        # Wikimedia
        wiki_url = "https://commons.wikimedia.org/w/api.php"
        wiki_params = {"action":"query","format":"json","generator":"search","gsrsearch":f"filetype:bitmap {query}","gsrlimit":limit,"prop":"imageinfo","iiprop":"url"}

        # Met API
        met_search = f"https://collectionapi.metmuseum.org/public/collection/v1/search?hasImages=true&q={query}"

        async def get_wiki():
            async with session.get(wiki_url, params=wiki_params) as r:
                data = await r.json()
                pages = data.get("query", {}).get("pages", {})
                return [{"url": v['imageinfo'][0]['url']} for k,v in pages.items() if 'imageinfo' in v]

        async def get_met():
            async with session.get(met_search) as r:
                ids = (await r.json()).get("objectIDs", [])[:limit]
                met_res = []
                for oid in ids:
                    async with session.get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}") as orer:
                        odata = await orer.json()
                        if odata.get("primaryImage"): met_res.append({"url": odata["primaryImage"]})
                return met_res

        res = await asyncio.gather(get_wiki(), get_met())
        candidates = [item for sub in res for item in sub]

        if not candidates: return None

        # CLIP Ranking
        scored = []
        txt = clip.tokenize([query]).to(device)
        for c in candidates:
            try:
                img_r = requests.get(c['url'], timeout=5)
                img = clip_preprocess(Image.open(BytesIO(img_r.content)).convert("RGB")).unsqueeze(0).to(device)
                with torch.no_grad():
                    score = torch.cosine_similarity(clip_model.encode_image(img), clip_model.encode_text(txt)).item()
                    scored.append({"url": c['url'], "score": score})
            except: continue
        return sorted(scored, key=lambda x: x['score'], reverse=True)[0]['url'] if scored else candidates[0]['url']

class VideoEngine:
    def __init__(self):
        self.tts = KPipeline(lang_code='a')

    async def make_scene(self, idx, text, topic):
        aud_p = f"{PATHS['audio']}/s_{idx}.wav"
        img_p = f"{PATHS['images']}/s_{idx}.jpg"
        vid_p = f"{PATHS['video']}/s_{idx}.mp4"

        # Audio
        gen = self.tts(text, voice='am_michael')
        sf.write(aud_p, np.concatenate([s for _,_,s in gen]), 24000)

        # Image
        url = await fetch_media(topic)
        if url:
            with open(img_p, 'wb') as f: f.write(requests.get(url).content)
        else:
            Image.new('RGB', (1280,720), (40,40,40)).save(img_p)

        # Render
        dur = librosa.get_duration(path=aud_p)
        subprocess.run(["ffmpeg","-y","-loop","1","-i",img_p,"-i",aud_p,"-vf","scale=1280:720,zoompan=z='min(zoom+0.001,1.2)':d=125:s=1280x720","-c:v","libx264","-t",str(dur),"-pix_fmt","yuv420p",vid_p], capture_output=True)
        return vid_p

# --- 3. GRADIO INTERFACE ---
async def run_mega_pipeline(topic, niche, progress=gr.Progress()):
    engine = VideoEngine()
    progress(0.1, desc="Generating Script...")

    # Simplified Scripting for Mega Cell Demo
    resp = client.chat.completions.create(model=MODEL_NAME, messages=[{"role":"user","content":f"Write a 3-scene documentary script about {topic} for a {niche} niche. Separate scenes with [SCENE]"}])
    scenes = [s.strip() for s in resp.choices[0].message.content.split("[SCENE]") if len(s.strip()) > 10]

    vids = []
    for i, s_text in enumerate(scenes):
        progress((i+1)/len(scenes), desc=f"Processing Scene {i+1}...")
        vids.append(await engine.make_scene(i, s_text, topic))

    progress(0.9, desc="Stitching Master Video...")
    master_output = f"{PATHS['video']}/{topic.replace(' ','_')}_Final.mp4"
    with open("concat.txt", "w") as f:
        for v in vids: f.write(f"file '{os.path.abspath(v)}'\n")

    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i","concat.txt","-c","copy", master_output], capture_output=True)
    return master_output

demo = gr.Interface(
    fn=lambda t, n: asyncio.run(run_mega_pipeline(t, n)),
    inputs=[gr.Textbox(label="Topic"), gr.Dropdown(["Testing", "Speculative Historian", "Napping Historian"], label="Niche")],
    outputs=gr.Video(label="Generated Documentary"),
    title="VideoFactory Mega Dashboard",
    description="Generates a full documentary using Gemini, Wikimedia, Met API, Kokoro TTS, and FFmpeg."
)

demo.launch(debug=True, share=True)
import os, json, logging, asyncio, aiohttp, requests, torch, clip, numpy as np, soundfile as sf
from PIL import Image
from io import BytesIO
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from kokoro import KPipeline
import nest_asyncio

# Apply nest_asyncio to allow nested event loops (required for Colab/Jupyter)
nest_asyncio.apply()

# --- 1. ENVIRONMENT & PATH STANDARDIZATION ---
BASE_PATH = "/workspace/VideoFactory" if os.path.exists("/workspace") else "/content/drive/MyDrive/VideoFactory"
PROJECT_NAME = "Tudor_Documentary_001"

PATHS = {
    "scripts": f"{BASE_PATH}/scripts/{PROJECT_NAME}",
    "audio": f"{BASE_PATH}/projects/{PROJECT_NAME}/audio",
    "images": f"{BASE_PATH}/projects/{PROJECT_NAME}/images",
    "video": f"{BASE_PATH}/projects/{PROJECT_NAME}/video"
}
for p in PATHS.values(): os.makedirs(p, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VideoFactory")

# API Configuration
WAVESPEED_KEY = os.environ.get('WAVESPEED_API_KEY') or "EMPTY"
client = OpenAI(api_key=WAVESPEED_KEY, base_url="https://llm.wavespeed.ai/v1")

# CLIP Initialization
device = "cuda" if torch.cuda.is_available() else "cpu"
clip_model, clip_preprocess = clip.load("ViT-B/32", device=device)

# --- 2. HISTORICAL ENGINE ---
def rank_historical_assets(urls):
    pos = clip.tokenize(["16th century Tudor era oil painting"]).to(device)
    neg = clip.tokenize(["modern photo, 3d render"]).to(device)
    scored = []
    for url in urls:
        try:
            r = requests.get(url, timeout=5, headers={'User-Agent': 'VideoFactory/1.0'})
            img = clip_preprocess(Image.open(BytesIO(r.content)).convert("RGB")).unsqueeze(0).to(device)
            with torch.no_grad():
                feats = clip_model.encode_image(img)
                s = torch.cosine_similarity(feats, clip_model.encode_text(pos)).item() - (torch.cosine_similarity(feats, clip_model.encode_text(neg)).item() * 0.5)
                scored.append({"url": url, "score": s})
        except: continue
    return sorted(scored, key=lambda x: x['score'], reverse=True)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 4, 10))
async def fetch_media(query):
    async with aiohttp.ClientSession() as s:
        api = "https://commons.wikimedia.org/w/api.php"
        p = {"action":"query","format":"json","generator":"search","gsrsearch":query,"gsrlimit":5,"prop":"imageinfo","iiprop":"url"}
        async with s.get(api, params=p) as r:
            data = await r.json()
            urls = [v['imageinfo'][0]['url'] for k,v in data.get("query",{}).get("pages",{}).items() if 'imageinfo' in v]
    return rank_historical_assets(urls)

# --- 3. PRODUCTION MANAGER ---
class ProductionManager:
    def __init__(self):
        self.tts = KPipeline(lang_code='a')

    async def process_scene(self, idx, text, topic):
        audio_p = f"{PATHS['audio']}/s_{idx:03d}.wav"
        img_p = f"{PATHS['images']}/s_{idx:03d}.jpg"

        async def tts_task():
            gen = self.tts(text, voice='am_michael')
            aud = np.concatenate([s for _,_,s in gen])
            sf.write(audio_p, aud, 24000)

        async def img_task():
            ranked = await fetch_media(topic)
            url = ranked[0]['url'] if ranked else None
            if url:
                with open(img_p, 'wb') as f: f.write(requests.get(url).content)
            else:
                Image.new('RGB', (1280, 720), (44, 40, 34)).save(img_p)

        await asyncio.gather(tts_task(), img_task())
        return audio_p, img_p

# --- 4. UI ENTRY POINT ---

async def run_flow(b):
    if True:
        mgr = ProductionManager()
        print(f"🎬 STARTING: {niche_drp.value} mode for '{topic_in.value}'")
        a, i = await mgr.process_scene(1, f"Test scene for {topic_in.value}", topic_in.value)
        print(f"✅ Phase 1 Sync Complete: {a}, {i}")

print("VideoFactory Dashboard Ready (Niche: Testing)")

from google.colab import drive
import os

# Mount Google Drive
drive.mount('/content/drive')

# Define the base path
BASE_PATH = '/content/drive/MyDrive/VideoFactory'

# Create the folder structure
folders = [
    f'{BASE_PATH}/scripts',
    f'{BASE_PATH}/projects'
]

for folder in folders:
    os.makedirs(folder, exist_ok=True)
    print(f"Verified folder: {folder}")
from google.colab import drive
drive.mount('/content/drive')
print("✅ Dependencies installed.")
import os
import json
from openai import OpenAI


# Configure Wavespeed Client
try:
    wavespeed_key = os.environ.get('WAVESPEED_API_KEY')
    client = OpenAI(
        api_key=wavespeed_key,
        base_url="https://llm.wavespeed.ai/v1",
        timeout=120.0,
        max_retries=2,
    )
    MODEL_NAME = "google/gemini-2.5-flash"
    print(f"✅ Wavespeed API configured with {MODEL_NAME}")
except Exception as e:
    print(f"❌ Error: {e}. Please add WAVESPEED_API_KEY to Colab Secrets.")

NICHE_CONFIGS = {
    "Testing": {"chapters": 2},
    "Speculative Historian": {"chapters": 10},
    "Napping Historian": {"chapters": 40}
}

def generate_scene_plan(topic, scene_text):
    """
    LLM as Director: Generates prioritized queries and camera motion in JSON.
    """
    prompt = f"""As a Documentary Director for a film on '{topic}', analyze this scene:
    '{scene_text}'

    Output a JSON object with:
    1. 'searchQueries': List of 3 queries (Specific, Broad, Generic) for Wikimedia/Met.
    2. 'cameraMotion': Choose one: PAN_UP, PAN_DOWN, PAN_LEFT, PAN_RIGHT, ZOOM_IN, ZOOM_OUT.

    Return ONLY the raw JSON."""

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        return json.loads(response.choices[0].message.content)
    except:
        return {"searchQueries": [topic], "cameraMotion": "ZOOM_IN"}

# UI Elements with 'Testing' niche included and set as default

def run_production_sequence(b):
    if True:
        topic = topic_input.value
        niche = niche_dropdown.value

        # NICHE_CONFIGS must be defined by running cell 860a6358
        if 'NICHE_CONFIGS' not in globals():
            print("❌ ERROR: Please run the Logic Cell (860a6358) first to load configurations!")
            return

        total_chaps = NICHE_CONFIGS[niche]['chapters']
        progress_bar.max = total_chaps + 3

        print(f"🎬 Starting production for: {topic} ({niche} Niche)")
        progress_bar.value = 0

        print("⏳ Step 1: Generating outline...")
        outline = generate_outline(topic, niche)
        print("✅ Outline generated.")
        progress_bar.value = 1

        print(f"⏳ Step 2: Expanding {total_chaps} chapters...")
        full_script = ""
        prev_summary = "Initial setup."

        for i in range(1, total_chaps + 1):
            print(f"   📝 Expanding Section {i}/{total_chaps}...")
            chapter_text = generate_chapter(topic, f'Section {i}', 'Documentary Segment', outline, prev_summary, i, total_chaps, niche)
            full_script += '\n\n' + chapter_text
            progress_bar.value = 1 + i

        print("⏳ Step 3: Chunking script into scenes...")
        final_scenes = chunk_script_to_scenes(full_script)
        progress_bar.value += 1

        print("⏳ Step 4: Architecting Visual Plan...")
        project_data = {'topic': topic, 'niche': niche, 'full_script': full_script, 'scenes': final_scenes}
        generate_visual_plan(project_data)

        progress_bar.value = progress_bar.max
        print(f"\n🎉 SUCCESS! Generated {len(final_scenes)} scenes for validation.")


def generate_visual_queries(scene_text, topic):
    """
    Uses Gemini to transform atmospheric script text into optimized search queries.
    """
    prompt = f"""As a Visual Architect for a historical documentary on '{topic}',
    convert the following scene text into a high-probability search query for Wikimedia Commons.

    Rules:
    1. Focus on historical accuracy (portraits, artifacts, locations).
    2. Include artistic styles like 'oil painting', 'contemporary engraving', or '16th century portrait'.
    3. Keep it under 10 words.

    Scene Text: {scene_text}

    Return ONLY the optimized query string."""

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}]
        )
        query = response.choices[0].message.content.strip().strip('"')
        return query
    except Exception as e:
        return topic # Fallback to general topic

# Integrated Logic: Processing the actual project scenes
def generate_visual_plan(project_data):
    print(f"🎨 Architecting visual queries for {len(project_data['scenes'])} scenes...")
    visual_plan = []
    for idx, scene_text in enumerate(project_data['scenes']):
        query = generate_visual_queries(scene_text, project_data['topic'])
        visual_plan.append({
            "id": idx + 1,
            "text": scene_text,
            "query": query
        })
        if (idx + 1) % 10 == 0: print(f"   ✅ Processed {idx + 1} queries...")

    project_data['visual_plan'] = visual_plan
    save_script(f"{project_data['niche'].replace(' ', '_')}_visual_plan.json", project_data)
    print("🚀 Visual Plan finalized and saved to script directory.")
    return visual_plan
import subprocess
import librosa

def create_scene_video(image_path, audio_path, output_mp4):
    # Get exact duration of audio
    duration = librosa.get_duration(path=audio_path)

    # FFmpeg command for smooth Zoom In effect (zoompan) + attaching audio
    command = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,    # Loop the single image
        "-i", audio_path,                  # Add the audio
        "-vf", f"scale=1920:1080,zoompan=z='min(zoom+0.0015,1.5)':d={int(duration*25)}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',framerate=25",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-t", str(duration),               # Cut video exactly at audio length
        "-pix_fmt", "yuv420p",
        output_mp4
    ]

    subprocess.run(command, check=True, capture_output=True)
    print(f"✅ Rendered: {output_mp4}")
def concatenate_scenes(scene_paths, output_filename):
    """
    Stitches multiple scene MP4s into a single master documentary file.
    """
    list_path = os.path.join(PATHS['video'], "concat_list.txt")
    master_path = os.path.join(PATHS['video'], output_filename)

    with open(list_path, 'w') as f:
        for path in scene_paths:
            # FFmpeg concat demuxer requires 'file' prefix and absolute paths
            f.write(f"file '{os.path.abspath(path)}'\n")

    print(f"🎬 Stitching {len(scene_paths)} scenes into master file...")
    command = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy", # Stream copy for speed as codecs are identical
        master_path
    ]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✅ Master Documentary Created: {master_path}")
        return master_path
    else:
        print(f"❌ Concatenation failed: {result.stderr}")
        return None
class VideoProductionManager:
    def __init__(self, project_name, niche):
        self.project_name = project_name
        self.niche = niche
        self.base_dir = f"{BASE_PATH}/projects/{project_name}"
        self.audio_dir = f"{self.base_dir}/audio"
        self.video_dir = f"{self.base_dir}/video"
        self.img_dir = f"{self.base_dir}/images"

        for d in [self.audio_dir, self.video_dir, self.img_dir]:
            os.makedirs(d, exist_ok=True)

        self.tts_pipeline = KPipeline(lang_code='a')

    async def process_scene(self, scene_id, scene_text, topic):
        print(f"[Scene {scene_id}] 🎙️ Generating Audio & 🖼️ Sourcing Media...")

        audio_path = os.path.join(self.audio_dir, f"scene_{scene_id:03d}.wav")
        img_path = os.path.join(self.img_dir, f"scene_{scene_id:03d}.jpg")
        out_mp4 = os.path.join(self.video_dir, f"scene_{scene_id:03d}.mp4")

        # Parallelize TTS and Retrieval
        async def run_tts():
            generator = self.tts_pipeline(scene_text, voice='am_michael')
            audio_segments = [s for _, _, s in generator]
            if audio_segments:
                sf.write(audio_path, np.concatenate(audio_segments), 24000)

        query = generate_visual_queries(scene_text, topic)
        img_url_task = smart_media_retrieval(query, topic)

        await asyncio.gather(run_tts(), img_url_task)
        img_url = await img_url_task

        # Final validation and download
        try:
            headers = {'User-Agent': 'VideoFactoryBot/1.0'}
            resp = requests.get(img_url, headers=headers, timeout=10) if img_url else None
            if resp and resp.status_code == 200:
                with open(img_path, 'wb') as f: f.write(resp.content)
            else:
                raise ValueError("No valid image")
        except:
            # Resilient Fallback: Historical Paper Texture Background
            fallback_img = Image.new('RGB', (1920, 1080), color=(44, 40, 34))
            fallback_img.save(img_path)

        # Sync Video Duration to Audio Duration
        create_scene_video(img_path, audio_path, out_mp4)
        return out_mp4
import asyncio
import nest_asyncio

nest_asyncio.apply()

# --- Final Production Dashboard with Master Concatenation ---
#     options=['Testing', 'Speculative Historian', 'Napping Historian'],
#     value='Testing',
#     description='Niche:',
# )

def on_go_clicked(b):
    asyncio.ensure_future(run_master_workflow(topic_in.value, niche_drp.value))

print("🎬 VideoFactory Master Orchestrator Ready")

async def run_master_workflow(topic, niche):
    if True:
        manager = VideoProductionManager(topic.replace(' ', '_'), niche)

        print(f"🚀 [LOG] STARTING MASTER PRODUCTION WORKFLOW")
        print(f"📌 Topic: {topic} | Niche: {niche}")

        # 1. Scripting Phase Logging
        print("⏳ [LOG] Phase 1: Scripting started.")
        print(f"   Generating outline for '{topic}'...")
        outline = generate_outline(topic, niche)
        print("   ✅ Outline successfully generated.")

        full_script = ""
        total_chaps = NICHE_CONFIGS[niche]['chapters']
        print(f"   Starting expansion of {total_chaps} chapters...")

        for i in range(1, total_chaps + 1):
            print(f"   📝 [LOG] Writing Chapter {i}/{total_chaps}...")
            chapter_text = generate_chapter(topic, f'Chapter {i}', 'Segment', outline, '', i, total_chaps, niche)
            full_script += '\n\n' + chapter_text

        print("✅ [LOG] Scripting phase complete.")

        # 2. Scene Rendering (Basic logging preserved, to be enhanced next)
        scenes = chunk_script_to_scenes(full_script)
        print(f"⏳ [LOG] Phase 2: Scene Rendering started ({len(scenes)} scenes detected).")
        scene_video_paths = []
        for idx, scene_text in enumerate(scenes):
            print(f"   ⚙️ Rendering Scene {idx+1}/{len(scenes)}...")
            mp4_path = await manager.process_scene(idx+1, scene_text, topic)
            scene_video_paths.append(mp4_path)

        # 3. Final Concatenation
        print("⏳ [LOG] Phase 3: Final Concatenation started.")
        master_file = f'{topic.replace(" ", "_")}_Final_Doc.mp4'
        concatenate_scenes(scene_video_paths, master_file)

        print(f"\n🏆 [LOG] PRODUCTION COMPLETE: {master_file} is ready in {PATHS['video']}")
import asyncio
import nest_asyncio

nest_asyncio.apply()

# Re-initialize UI for the final master sequence

def trigger_production(b):
    if True:
        print(f"🚀 Initializing {niche_master.value} workflow for: {topic_master.value}")
        asyncio.ensure_future(run_master_workflow(topic_master.value, niche_master.value))


import os, asyncio, aiohttp, requests, subprocess, numpy as np, soundfile as sf, librosa
from PIL import Image
from io import BytesIO


async def fetch_single_query(session, query):
    """Concurrent worker for the fallback race."""
    url = "https://commons.wikimedia.org/w/api.php"
    params = {"action":"query","format":"json","generator":"search","gsrsearch":f"filetype:bitmap {query}","gsrlimit":1,"prop":"imageinfo","iiprop":"url"}
    try:
        async with session.get(url, params=params, headers=WIKI_HEADERS) as r:
            data = await r.json()
            pages = data.get("query", {}).get("pages", {})
            for k,v in pages.items():
                if 'imageinfo' in v: return v['imageinfo'][0]['url']
    except: pass
    return None

async def smart_media_retrieval(queries, topic):
    """Concurrent Fallback Race: First valid result wins."""
    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(fetch_single_query(session, q)) for q in queries]
        for completed in asyncio.as_completed(tasks):
            url = await completed
            if url:
                for t in tasks: t.cancel() # Stop the others
                return url
    return None

def get_zoompan_filter(motion_type: str, duration_sec: float) -> str:
    frames = int(duration_sec * 25)
    filters = {
        "ZOOM_IN": f"scale=1920:1080,zoompan=z='min(zoom+0.0015,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080",
        "ZOOM_OUT": f"scale=1920:1080,zoompan=z='max(1.25-0.0015*on,1.0)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080",
        "PAN_RIGHT": f"scale=1920:1080,zoompan=z='1.2':x='if(eq(on,1),0,x+2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080",
        "PAN_LEFT": f"scale=1920:1080,zoompan=z='1.2':x='if(eq(on,1),iw-iw/zoom,x-2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080",
        "PAN_UP": f"scale=1920:1080,zoompan=z='1.2':x='iw/2-(iw/zoom/2)':y='if(eq(on,1),ih-ih/zoom,y-2)':d={frames}:s=1920x1080",
        "PAN_DOWN": f"scale=1920:1080,zoompan=z='1.2':x='iw/2-(iw/zoom/2)':y='if(eq(on,1),0,y+2)':d={frames}:s=1920x1080"
    }
    return filters.get(motion_type.upper(), filters["ZOOM_IN"])

async def process_scene_v2(manager, idx, text, topic):
    """Optimized scene processor with racing and dynamic filters."""
    plan = generate_scene_plan(topic, text)
    aud_p, img_p, vid_p = f"s_{idx}.wav", f"s_{idx}.jpg", f"s_{idx}.mp4"

    # Concurrent Audio and Racing Image Search
    img_url_task = asyncio.create_task(smart_media_retrieval(plan['searchQueries'], topic))

    generator = manager.tts_pipeline(text, voice='am_michael')
    sf.write(aud_p, np.concatenate([s for _, _, s in generator]), 24000)

    img_url = await img_url_task
    img_data = requests.get(img_url, headers=WIKI_HEADERS).content if img_url else Image.new('RGB', (1920, 1080), (44, 40, 34))
    with open(img_p, 'wb') as f: f.write(img_data if isinstance(img_data, bytes) else b'')

    # Render with specific motion
    dur = librosa.get_duration(path=aud_p)
    v_filter = get_zoompan_filter(plan['cameraMotion'], dur)
    cmd = ["ffmpeg","-y","-loop","1","-i",img_p,"-i",aud_p,"-vf",v_filter,"-c:v","libx264","-t",str(dur),"-pix_fmt","yuv420p",vid_p]
    subprocess.run(cmd, capture_output=True)
    return vid_p
from kokoro import KPipeline
import soundfile as sf
import numpy as np
import os

# Initialize the pipeline
pipeline = KPipeline(lang_code='a')

def generate_scene_audio(text, scene_id, project_dir, voice='am_michael'):
    """Generates audio for a specific scene and saves it to the project folder."""
    generator = pipeline(text, voice=voice)
    all_audio = []

    for i, (gs, ps, audio_segment) in enumerate(generator):
        all_audio.append(audio_segment)

    if all_audio:
        merged_audio = np.concatenate(all_audio)
        output_path = os.path.join(project_dir, '2_audio', f'scene_{scene_id:03d}.wav')
        sf.write(output_path, merged_audio, 24000)
        print(f"✅ Audio saved: {output_path}")
        return output_path
    else:
        print(f"❌ Failed to generate audio for scene {scene_id}")
        return None
import subprocess

def install_libs():
    libs = ['chromadb', 'sentence-transformers']
    for lib in libs:
        print(f"Installing {lib}...")
        subprocess.run(['pip', 'install', '-q', lib], check=True)
    print("✅ Vector database libraries installed.")

install_libs()
import chromadb
from chromadb.utils import embedding_functions
import os

# Define the cache directory path on Google Drive
CACHE_DIR = os.path.join(BASE_PATH, 'image_vector_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# Initialize Persistent Client
chroma_client = chromadb.PersistentClient(path=CACHE_DIR)

# Use a standard sentence-transformer model for embeddings
embedding_func = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

# Create or get the collection
collection = chroma_client.get_or_create_collection(
    name="image_cache",
    embedding_function=embedding_func,
    metadata={"hnsw:space": "cosine"}  # Use cosine similarity for semantic matching
)

print(f"✅ ChromaDB initialized at: {CACHE_DIR}")
print(f"📦 Collection 'image_cache' is ready.")
import chromadb
from chromadb.utils import embedding_functions
import os

# Redefine base path to ensure it is in scope
BASE_PATH = '/content/drive/MyDrive/VideoFactory'

# Define the cache directory path on Google Drive
CACHE_DIR = os.path.join(BASE_PATH, 'image_vector_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# Initialize Persistent Client
chroma_client = chromadb.PersistentClient(path=CACHE_DIR)

# Use a standard sentence-transformer model for embeddings
embedding_func = embedding_functions.SentenceTransformerEmbeddingFunction(model_name='all-MiniLM-L6-v2')

# Create or get the collection
collection = chroma_client.get_or_create_collection(
    name='image_cache',
    embedding_function=embedding_func,
    metadata={'hnsw:space': 'cosine'}
)

print(f'✅ ChromaDB initialized at: {CACHE_DIR}')
print(f'📦 Collection \'image_cache\' is ready.')
print(f"Collection Name: {collection.name}")
print(f"Current document count in cache: {collection.count()}")
print("✅ Vector cache subtask complete.")
import subprocess

def install_async_libs():
    print('Installing aiohttp...')
    subprocess.run(['pip', 'install', '-q', 'aiohttp'], check=True)
    print('✅ aiohttp installed.')

install_async_libs()
import aiohttp
import asyncio


async def fetch_wikimedia_images(session, query, limit=5):
    """Fetches images from Wikimedia Commons via API."""
    url = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap|drawing {query}",
        "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
    }
    try:
        async with session.get(url, params=params) as response:
            data = await response.json()
            pages = data.get("query", {}).get("pages", {})
            results = []
            for _, val in pages.items():
                info = val.get("imageinfo", [{}])[0]
                if info.get("url"):
                    results.append({"url": info["url"], "source": "Wikimedia", "query": query})
            return results
    except Exception as e:
        print(f"Wikimedia error: {e}")
        return []

# Removed fetch_pexels_images as per user request.
# If you want to add another source like 'metapi', define a similar async function here.

async def fetch_all_sources(query, limit=5):
    """Orchestrates parallel fetching from all sources (currently Wikimedia only)."""
    async with aiohttp.ClientSession() as session:
        tasks = [
            fetch_wikimedia_images(session, query, limit)
        ]
        results = await asyncio.gather(*tasks)
        # Flatten list of lists
        candidate_pool = [item for sublist in results for item in sublist]
        print(f"✅ Fetched {len(candidate_pool)} candidates for: {query}")
        return candidate_pool

# Example usage (commented out for step generation)
# loop = asyncio.get_event_loop()
# pool = loop.run_until_complete(fetch_all_sources("Spanish Armada naval battle", limit=3))
import subprocess

def install_clip():
    print('Installing CLIP and dependencies...')
    # Install CLIP directly from OpenAI's repository
    subprocess.run(['pip', 'install', 'git+https://github.com/openai/CLIP.git'], check=True)
    # Install additional dependencies for CLIP
    subprocess.run(['pip', 'install', 'ftfy', 'regex', 'tqdm'], check=True)
    print('✅ CLIP installed successfully.')

install_clip()
import torch
import clip
from PIL import Image
import requests
from io import BytesIO

# Load the CLIP model and transformation
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)
print(f"✅ CLIP model (ViT-B/32) loaded on {device}")

def rank_candidates(visual_prompt, candidate_urls):
    """
    Downloads images and ranks them against the visual prompt using CLIP.
    """
    if not candidate_urls:
        return []

    text_tokens = clip.tokenize([visual_prompt]).to(device)
    scored_candidates = []

    for url in candidate_urls:
        try:
            response = requests.get(url, timeout=5)
            img = Image.open(BytesIO(response.content)).convert("RGB")
            img_input = preprocess(img).unsqueeze(0).to(device)

            with torch.no_grad():
                image_features = model.encode_image(img_input)
                text_features = model.encode_text(text_tokens)

                # Calculate cosine similarity
                logits_per_image, _ = model(img_input, text_tokens)
                score = logits_per_image.item()

            scored_candidates.append({"url": url, "score": score})
        except Exception as e:
            print(f"Skipping {url} due to error: {e}")

    # Sort by score descending
    return sorted(scored_candidates, key=lambda x: x['score'], reverse=True)

print("✅ CLIP ranking function defined.")
print('Verifying ranking function...')
# Small logic check for the function signature
print(f'Model object: {type(model)}')
print(f'Device used: {device}')
print('✅ CLIP subtask logic verified.')
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10))
async def validate_image_relevance_async(image_url, visual_prompt):
    """
    Uses Gemini 2.5 Flash via Wavespeed to validate image relevance asynchronously.
    Includes a retry loop for robustness.
    """
    prompt = f"""Analyze this image against the following visual prompt:
    Visual Prompt: {visual_prompt}

    Does the image accurately represent the prompt?
    Return a JSON object with 'relevant' (bool) and 'confidence' (0.0 to 1.0)."""

    try:
        # Since OpenAI client used for Wavespeed is synchronous, we wrap it in a thread for async compatibility
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }
            ],
            response_format={ "type": "json_object" }
        ))

        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        print(f"Validation error for {image_url}: {e}")
        raise  # Trigger retry

print("✅ Asynchronous Gemini validation function with retries is ready.")
import aiohttp
import asyncio
import uuid
import json
import torch
import clip
from PIL import Image
import requests
from io import BytesIO

async def smart_media_retrieval(visual_prompt, historical_context, limit=3, retry_count=0):
    # 1. Check ChromaDB Cache
    results = collection.query(query_texts=[visual_prompt], n_results=1)
    if results['ids'][0] and results['distances'][0][0] < 0.15:
        return results['metadatas'][0][0]['url']

    # 2. Fetch from sources
    candidates = await fetch_all_sources(visual_prompt, limit=5)
    if not candidates:
        if retry_count < 1:
            refined_prompt = f"historical artifact {historical_context}"
            return await smart_media_retrieval(refined_prompt, historical_context, limit, retry_count + 1)
        return None

    # 3. Local CLIP Ranking
    ranked = rank_candidates(visual_prompt, [c['url'] for c in candidates])

    if ranked and ranked[0]['score'] >= 0.75:
        best_url = ranked[0]['url']
        collection.add(
            ids=[str(uuid.uuid4())],
            documents=[visual_prompt],
            metadatas=[{"url": best_url, "context": historical_context}]
        )
        return best_url
    elif retry_count < 1:
        # Refinement loop if score is low
        print(f"⚠️ Low CLIP score ({ranked[0]['score'] if ranked else 0}). Refining query...")
        refined_prompt = generate_visual_queries(f"Refine this for historical accuracy: {visual_prompt}", historical_context)
        return await smart_media_retrieval(refined_prompt, historical_context, limit, retry_count + 1)

    return ranked[0]['url'] if ranked else None
import requests
from io import BytesIO

def render_enhanced_dashboard(scene_data_list):
    """Updated dashboard with proper headers for Wikimedia image loading."""
    print("--- VideoFactory Production Dashboard ---")
    # Wikimedia requires a User-Agent header or they may block the request
    headers = {'User-Agent': 'VideoFactoryBot/1.0 (contact: your-email@example.com)'}

    for scene in scene_data_list:

        # Image Preview from URL
        try:
            resp = requests.get(scene['img_url'], headers=headers, timeout=10)
            resp.raise_for_status()
        except Exception as e:

        # Metadata and Controls
#         status_color = "green" if "Cache" in scene.get('status', '') else "#0078d4"



# print("✅ Enhanced Dashboard UI (with Header Fix) is ready.")
# import nest_asyncio
# import asyncio
# nest_asyncio.apply()

# async def run_production_test():
#     print("🎬 Starting Integrated Production Test: 'Spanish Armada'")
#     test_scenes = [
#         {"text": "1588. The Spanish Armada sets sail.", "query": "16th century Spanish Galleons painting", "context": "Armada"},
#         {"text": "Queen Elizabeth I at Tilbury.", "query": "Queen Elizabeth I armor portrait", "context": "Elizabeth"}
#     ]

    # Use the smart retrieval pipeline established in previous cells
#     tasks = [smart_media_retrieval(s['query'], s['context']) for s in test_scenes]
#     results = await asyncio.gather(*tasks)

#     scene_data = []
#     for i, url in enumerate(results):
#         scene_data.append({
#             "text": test_scenes[i]['text'],
#             "img_url": url or "https://via.placeholder.com/300",
#             "query": test_scenes[i]['query'],
#             "status": "Fetched & Validated"
#         })

#     render_enhanced_dashboard(scene_data)

# Execute the test
# asyncio.run(run_production_test())

# async def run_master_workflow(topic, niche):
#     if True:
        # Ensure we use the specialized manager defined in the architecture
#         manager = VideoProductionManager(topic.replace(' ', '_'), niche)

#         print(f"🚀 [LOG] STARTING MASTER PRODUCTION WORKFLOW")
#         print(f"📌 Topic: {topic} | Niche: {niche}")

        # 1. Scripting Phase
#         print("⏳ [LOG] Phase 1: Scripting started.")
#         print(f"   Generating outline for '{topic}'...")
#         outline = generate_outline(topic, niche)
#         print("   ✅ Outline successfully generated.")

#         full_script = ""
#         total_chaps = NICHE_CONFIGS[niche]['chapters']
#         print(f"   Starting expansion of {total_chaps} chapters...")

#         for i in range(1, total_chaps + 1):
            print(f"   📝 [LOG] Writing Chapter {i}/{total_chaps}...")
            chapter_text = generate_chapter(topic, f'Chapter {i}', 'Segment', outline, '', i, total_chaps, niche)
            full_script += '\n\n' + chapter_text

        print("✅ [LOG] Scripting phase complete.")

        # 2. Scene Rendering (Enhanced Logging)
        scenes = chunk_script_to_scenes(full_script)
        num_scenes = len(scenes)
        print(f"⏳ [LOG] Phase 2: Scene Rendering started ({num_scenes} scenes detected).")

        scene_video_paths = []
        for idx, scene_text in enumerate(scenes):
            scene_num = idx + 1
            print(f"   ⚙️ [SCENE {scene_num}/{num_scenes}] Processing: Audio, Visuals, and Render...")

            # The manager already handles internal parallelization, but we log the start/end here
#             mp4_path = await manager.process_scene(scene_num, scene_text, topic)

            if os.path.exists(mp4_path):
                print(f"   ✅ [SCENE {scene_num}/{num_scenes}] Render Complete: {os.path.basename(mp4_path)}")
            else:
                print(f"   ❌ [SCENE {scene_num}/{num_scenes}] Render Failed.")

            scene_video_paths.append(mp4_path)

        # 3. Final Concatenation
        print("⏳ [LOG] Phase 3: Final Concatenation started.")
        master_file = f'{topic.replace(" ", "_")}_Final_Doc.mp4'
        final_path = concatenate_scenes(scene_video_paths, master_file)

        if final_path:
            print(f"\n🏆 [LOG] PRODUCTION SUCCESSFUL: {master_file} generated.")
        else:
            print("\n⚠️ [LOG] PRODUCTION ERROR: Final stitching failed.")

print('✅ Orchestrator updated with enhanced Scene Rendering logging.')
async def run_master_workflow(topic, niche):
    # Removed 'with out' and 'clear_output' to fix the NameError and facilitate direct logging
    manager = VideoProductionManager(topic.replace(' ', '_'), niche)

    print(f"🚀 [LOG] STARTING MASTER PRODUCTION WORKFLOW")
    print(f"📌 Topic: {topic} | Niche: {niche}")

    # 1. Scripting Phase
    print("⏳ [LOG] Phase 1: Scripting started.")
    print(f"   Generating outline for '{topic}'...")
    outline = generate_outline(topic, niche)
    print("   ✅ Outline successfully generated.")

    full_script = ""
    total_chaps = NICHE_CONFIGS[niche]['chapters']
    print(f"   Starting expansion of {total_chaps} chapters...")

    for i in range(1, total_chaps + 1):
        print(f"   📝 [LOG] Writing Chapter {i}/{total_chaps}...")
        chapter_text = generate_chapter(topic, f'Chapter {i}', 'Segment', outline, '', i, total_chaps, niche)
        full_script += '\n\n' + chapter_text

    print("✅ [LOG] Scripting phase complete.")

    # 2. Scene Rendering
    scenes = chunk_script_to_scenes(full_script)
    num_scenes = len(scenes)
    print(f"⏳ [LOG] Phase 2: Scene Rendering started ({num_scenes} scenes detected).")

    scene_video_paths = []
    for idx, scene_text in enumerate(scenes):
        scene_num = idx + 1
        print(f"   ⚙️ [SCENE {scene_num}/{num_scenes}] Processing: Audio, Visuals, and Render...")
        mp4_path = await manager.process_scene(scene_num, scene_text, topic)

        if os.path.exists(mp4_path):
            print(f"   ✅ [SCENE {scene_num}/{num_scenes}] Render Complete: {os.path.basename(mp4_path)}")
        else:
            print(f"   ❌ [SCENE {scene_num}/{num_scenes}] Render Failed.")

        scene_video_paths.append(mp4_path)

    # 3. Final Concatenation (Enhanced Logging)
    print("⏳ [LOG] Phase 3: Final Concatenation started.")
    master_file = f'{topic.replace(" ", "_")}_Final_Doc.mp4'
    print(f"   Stitching {len(scene_video_paths)} scenes into {master_file}...")

    final_path = concatenate_scenes(scene_video_paths, master_file)

    if final_path and os.path.exists(final_path):
        print(f"\n🏆 [LOG] PRODUCTION SUCCESSFUL!")
        print(f"📁 Final File Path: {final_path}")
    else:
        print("\n⚠️ [LOG] PRODUCTION ERROR: Final stitching failed to produce a master file.")
class VideoProductionManager:
    def __init__(self, project_name, niche):
        self.project_name = project_name
        self.niche = niche
        self.base_dir = f"{BASE_PATH}/projects/{project_name}"
        self.audio_dir = f"{self.base_dir}/audio"
        self.video_dir = f"{self.base_dir}/video"
        self.img_dir = f"{self.base_dir}/images"

        for d in [self.audio_dir, self.video_dir, self.img_dir]:
            os.makedirs(d, exist_ok=True)

        self.tts_pipeline = KPipeline(lang_code='a')

    async def process_scene(self, scene_id, scene_text, topic):
        audio_path = os.path.join(self.audio_dir, f"scene_{scene_id:03d}.wav")
        img_path = os.path.join(self.img_dir, f"scene_{scene_id:03d}.jpg")
        out_mp4 = os.path.join(self.video_dir, f"scene_{scene_id:03d}.mp4")

        # Audio Task
        async def run_tts():
            print(f"      [SCENE {scene_id}] 🎙️ Starting TTS generation...")
            generator = self.tts_pipeline(scene_text, voice='am_michael')
            audio_segments = [s for _, _, s in generator]
            if audio_segments:
                sf.write(audio_path, np.concatenate(audio_segments), 24000)
                print(f"      [SCENE {scene_id}] ✅ Audio saved to {os.path.basename(audio_path)}.")

        # Visual Task
        async def source_visuals():
            print(f"      [SCENE {scene_id}] 🎨 Generating visual architect query...")
            query = generate_visual_queries(scene_text, topic)
            print(f"      [SCENE {scene_id}] 🔍 Searching sources for: '{query}'...")
            img_url = await smart_media_retrieval(query, topic)

            try:
                headers = {'User-Agent': 'VideoFactoryBot/1.0'}
                resp = requests.get(img_url, headers=headers, timeout=10) if img_url else None
                if resp and resp.status_code == 200:
                    with open(img_path, 'wb') as f: f.write(resp.content)
                    print(f"      [SCENE {scene_id}] ✅ Image downloaded successfully.")
                else:
                    raise ValueError("No valid image")
            except Exception as e:
                print(f"      [SCENE {scene_id}] ⚠️ Visual sourcing failed ({e}). Using historical fallback.")
                fallback_img = Image.new('RGB', (1920, 1080), color=(44, 40, 34))
                fallback_img.save(img_path)

        await asyncio.gather(run_tts(), source_visuals())

        # Render Task
        print(f"      [SCENE {scene_id}] 🎬 Triggering FFmpeg scene render...")
        create_scene_video(img_path, audio_path, out_mp4)

        return out_mp4

print('✅ VideoProductionManager updated with internal phase logging.')
import subprocess
import librosa
import os

def create_scene_video(image_path, audio_path, output_mp4):
    """
    Enhanced FFmpeg wrapper with real-time status logging for scene rendering.
    """
    try:
        print(f"      [FFMPEG] 📏 Calculating audio duration for {os.path.basename(audio_path)}...")
        duration = librosa.get_duration(path=audio_path)

        print(f"      [FFMPEG] 🎞️ Rendering scene video ({duration:.2f}s)... ")
        # FFmpeg command for smooth Zoom In effect
        command = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-vf", f"scale=1920:1080,zoompan=z='min(zoom+0.0015,1.5)':d={int(duration*25)}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',framerate=25",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            "-loglevel", "error", # Reduce noise, we use our own logs
            output_mp4
        ]

        subprocess.run(command, check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"      [FFMPEG] ❌ Render Error: {e}")
        return False

def concatenate_scenes(scene_paths, output_filename):
    """
    Enhanced FFmpeg wrapper with status logging for master video concatenation.
    """
    list_path = os.path.join(PATHS['video'], "concat_list.txt")
    master_path = os.path.join(PATHS['video'], output_filename)

    print(f"   🔗 [CONCAT] Preparing manifest for {len(scene_paths)} scenes...")
    with open(list_path, 'w') as f:
        for path in scene_paths:
            f.write(f"file '{os.path.abspath(path)}'\n")

    print(f"   🔗 [CONCAT] Executing master stitch into: {output_filename}...")
    command = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-loglevel", "error",
        master_path
    ]

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"   ✅ [CONCAT] Master stitch complete.")
        return master_path
    except subprocess.CalledProcessError as e:
        print(f"   ❌ [CONCAT] Stitching failed: {e.stderr}")
        return None

print('✅ FFmpeg wrapper functions updated with detailed status logging.')
import asyncio
import nest_asyncio

nest_asyncio.apply()

async def test_logging_pipeline():
    test_topic = "The Spanish Armada"
    test_niche = "Testing"

    print(f"🧪 Starting Comprehensive Stress Test for logging enhancement...")
    # Using the master workflow which now contains the enhanced logs
    await run_master_workflow(test_topic, test_niche)

# Run the test
asyncio.run(test_logging_pipeline())
import asyncio
import nest_asyncio

nest_asyncio.apply()

async def final_stress_test_logging():
    topic = "The Spanish Armada"
    niche = "Testing"

    print(f"🧪 [TEST] Starting End-to-End Logging Verification...")
    # Trigger the workflow defined in the previous cell (600d785d)
    await run_master_workflow(topic, niche)

# Run the verification
asyncio.run(final_stress_test_logging())
NICHE_CONFIGS = {
    'Testing': {'chapters': 2, 'word_count': 100, 'outline_prompt': 'Generate a brief 2-section outline.', 'chapter_prompt': 'Write a short paragraph.'},
    'Speculative Historian': {'chapters': 10, 'word_count': 6000, 'outline_prompt': 'Detailed outline.', 'chapter_prompt': 'Atmospheric segment.'},
    'Napping Historian': {'chapters': 40, 'word_count': 16000, 'outline_prompt': 'Long outline.', 'chapter_prompt': 'In-depth chapter.'}
}

def generate_outline(topic, niche):
    config = NICHE_CONFIGS[niche]
    prompt = f"{config['outline_prompt']}\nTOPIC: {topic}"
    response = client.chat.completions.create(model=MODEL_NAME, messages=[{'role': 'user', 'content': prompt}])
    return response.choices[0].message.content or ""

def generate_chapter(topic, title, desc, outline, prev, idx, total, niche):
    config = NICHE_CONFIGS[niche]
    prompt = f"TOPIC: {topic}. CHAPTER: {title}. PROGRESS: {idx}/{total}. {config['chapter_prompt']}"
    response = client.chat.completions.create(model=MODEL_NAME, messages=[{'role': 'user', 'content': prompt}])
    return response.choices[0].message.content or ""

def chunk_script_to_scenes(full_script):
    words = full_script.split()
    chunk_size = 25 if len(words) < 500 else 100
    return [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size) if words[i:i+chunk_size]]

print('✅ Scripting logic and NICHE_CONFIGS redefined.')
import asyncio
import nest_asyncio

nest_asyncio.apply()

async def final_verification_run():
    topic = "The Spanish Armada"
    niche = "Testing"

    print(f"🧪 [VERIFICATION] Starting Final Production Run with Enhanced Logging...")
    # Run the updated orchestrator defined in cell 600d785d
    await run_master_workflow(topic, niche)

# Run the verification cycle
asyncio.run(final_verification_run())
import asyncio
import nest_asyncio

nest_asyncio.apply()

async def verify_enhanced_logging():
    topic = "The Spanish Armada"
    niche = "Testing"

    print(f"🧪 [VERIFICATION] Starting Final Production Run with Enhanced Logging...")
    # Run the updated orchestrator which now has full Phase 1-3 logging and direct console printing
    await run_master_workflow(topic, niche)

# Execute the verification
asyncio.run(verify_enhanced_logging())
def generate_visual_queries(scene_text, topic):
    """
    Simplified visual prompt generator to create broader, high-probability search terms.
    """
    prompt = f"""As a historical documentary researcher, extract 2-3 key visual keywords from the following text related to '{topic}'.
    Combine them with a style descriptor like 'oil painting' or 'engraving'.
    Keep the total response under 6 words.

    Text: {scene_text}
    """
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{'role': 'user', 'content': prompt}]
        )
        query = response.choices[0].message.content.strip().strip('"').replace('`', '')
        return query
    except Exception:
        return f"{topic} history oil painting"

async def verify_enhanced_logging():
    topic = "The Spanish Armada"
    niche = "Testing"
    print(f"🧪 [VERIFICATION] Starting Final Production Run with Simplified Visual Queries...")
    await run_master_workflow(topic, niche)

import asyncio
import nest_asyncio
nest_asyncio.apply()
asyncio.run(verify_enhanced_logging())
import aiohttp
import asyncio
import uuid
import requests
from io import BytesIO
from PIL import Image
import torch
import clip

# Identity headers for Wikimedia compliance
WIKI_HEADERS = {'User-Agent': 'VideoFactoryBot/1.0 (https://colab.research.google.com/; production-team@example.com)'}

device = 'cuda' if torch.cuda.is_available() else 'cpu'
model, preprocess = clip.load('ViT-B/32', device=device)

async def fetch_wikimedia_images(session, query, limit=5):
    url = 'https://commons.wikimedia.org/w/api.php'
    params = {
        'action': 'query',
        'format': 'json',
        'generator': 'search',
        'gsrsearch': f'filetype:bitmap {query}',
        'gsrlimit': limit,
        'prop': 'imageinfo',
        'iiprop': 'url'
    }
    try:
        async with session.get(url, params=params, headers=WIKI_HEADERS) as r:
            if r.status != 200: return []
            data = await r.json()
            pages = data.get('query', {}).get('pages', {})
            return [{'url': v['imageinfo'][0]['url'], 'source': 'wiki'} for k,v in pages.items() if 'imageinfo' in v]
    except: return []

async def fetch_met_images(session, query, limit=3):
    # The Met API requires a search followed by object retrieval
    search_url = f'https://collectionapi.metmuseum.org/public/collection/v1/search?hasImages=true&q={query}'
    try:
        async with session.get(search_url) as r:
            search_data = await r.json()
            ids = search_data.get('objectIDs', [])[:limit]
            results = []
            # We fetch these in parallel as well
            obj_tasks = []
            for oid in ids:
                obj_tasks.append(session.get(f'https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}'))

            responses = await asyncio.gather(*obj_tasks)
            for resp in responses:
                obj_data = await resp.json()
                if obj_data.get('primaryImage'):
                    results.append({'url': obj_data['primaryImage'], 'source': 'met'})
            return results
    except: return []

def rank_candidates(visual_prompt, candidates):
    if not candidates: return []
    text_tokens = clip.tokenize([visual_prompt]).to(device)
    scored = []
    for c in candidates:
        try:
            # Download with headers
            r = requests.get(c['url'], timeout=5, headers=WIKI_HEADERS)
            img = preprocess(Image.open(BytesIO(r.content)).convert('RGB')).unsqueeze(0).to(device)
            with torch.no_grad():
                logits_per_image, _ = model(img, text_tokens)
                score = logits_per_image.item()
                scored.append({'url': c['url'], 'score': score, 'source': c['source']})
        except: continue
    return sorted(scored, key=lambda x: x['score'], reverse=True)

async def smart_media_retrieval(query, topic):
    async with aiohttp.ClientSession() as session:
        print(f"📡 Simultaneously firing requests to Wikimedia & Met for: {query}")
        # Fire both sources at once
        wiki_task = fetch_wikimedia_images(session, query)
        met_task = fetch_met_images(session, query)

        results = await asyncio.gather(wiki_task, met_task)
        candidates = [item for sublist in results for item in sublist]

        if not candidates:
            refined = f'{topic} historical oil painting'
            print(f"⚠️ No assets found. Falling back to broad search: {refined}")
            candidates = await fetch_wikimedia_images(session, refined)

        if not candidates: return None

        # CLIP compares all results and picks the best one
        ranked = rank_candidates(query, candidates)
        if ranked:
            best = ranked[0]
            print(f"✅ CLIP selected {best['source']} asset (Confidence: {best['score']:.2f})")
            return best['url']
        return candidates[0]['url']

# Trigger a test specifically for your requested scenario
asyncio.run(smart_media_retrieval('Philip II oil painting', 'The Spanish Armada'))
async def diagnostic_media_check(query):
    async with aiohttp.ClientSession() as session:
        print(f"🔍 Diagnostic: Testing query '{query}'")
        url = 'https://commons.wikimedia.org/w/api.php'
        params = {
            'action': 'query',
            'format': 'json',
            'generator': 'search',
            'gsrsearch': f'filetype:bitmap {query}',
            'gsrlimit': 5,
            'prop': 'imageinfo',
            'iiprop': 'url'
        }
        async with session.get(url, params=params) as r:
            data = await r.json()
            pages = data.get('query', {}).get('pages', {})
            if not pages:
                print("❌ No pages returned for this query.")
            for k, v in pages.items():
                print(f"✅ Found: {v.get('imageinfo', [{}])[0].get('url', 'No URL')}")

# Run a test for a typical failing query
import asyncio
asyncio.run(diagnostic_media_check('Spanish Armada 1588 oil painting'))
import asyncio
import nest_asyncio
import os

nest_asyncio.apply()

async def complete_final_verification():
    topic = "The Spanish Armada"
    niche = "Testing"

    print(f"🧪 [VERIFICATION] Starting Final Production Run for: {topic}")
    # This executes the master workflow which now uses simplified queries
    await run_master_workflow(topic, niche)

    # Post-run check for the output file
    final_filename = f"{topic.replace(' ', '_')}_Final_Doc.mp4"
    output_path = os.path.join(PATHS['video'], final_filename)
    if os.path.exists(output_path):
        print(f"\n🏁 VERIFICATION SUCCESS: Final video found at {output_path}")
    else:
        print(f"\n⚠️ VERIFICATION PENDING: Could not locate the master file at {output_path}")

# Execute the final check
asyncio.run(complete_final_verification())
import os
from google.colab import auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth import default

def upload_to_youtube(video_path, title, description):
    """
    Uploads the specified video to YouTube using the Data API v3.
    """
    auth.authenticate_user()
    creds, _ = default()
    youtube = build('youtube', 'v3', credentials=creds)

    body = {
        'snippet': {
            'title': title,
            'description': description,
            'tags': ['Documentary', 'History', 'AI'],
            'categoryId': '27'
        },
        'status': {
            'privacyStatus': 'unlisted',
            'selfDeclaredMadeForKids': False
        }
    }

    media = MediaFileUpload(
        video_path,
        mimetype='video/mp4',
        resumable=True
    )

    print(f"🚀 Uploading {video_path} to YouTube...")
    request = youtube.videos().insert(
        part='snippet,status',
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"   ⬆️ Uploaded {int(status.progress() * 100)}%")

    print(f"✅ SUCCESS! Video ID: {response.get('id')}")
    print(f"🔗 Link: https://www.youtube.com/watch?v={response.get('id')}")

# Verified file path from user selection
video_file = '/content/The_Spanish_Armada_Final_Doc (2).mp4'

if os.path.exists(video_file):
    upload_to_youtube(
        video_path=video_file,
        title="The Spanish Armada: An AI Documentary",
        description="A complete historical documentary generated using the VideoFactory AI pipeline."
    )
else:
    print(f"❌ Error: Could not find video at {video_file}.")

# ==============================================================================
# 🎬 VIDEOFACTORY V3 ARCHITECTURE: 100% SCRIPT-TO-VISUAL SYNC
# ==============================================================================

import asyncio
import aiohttp
import uuid
import json
import torch
import clip
from PIL import Image
import requests
from io import BytesIO
import librosa
import subprocess
import soundfile as sf
import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

# ── 1. MULTI-SOURCE FETCHING ──────────────────────────────────────────────────
async def fetch_wikimedia_images(session, query, limit=3):
    url = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap|drawing {query}", "gsrlimit": limit,
        "prop": "imageinfo", "iiprop": "url"
    }
    try:
        async with session.get(url, params=params) as r:
            data = await r.json()
            pages = data.get("query", {}).get("pages", {})
            return [{"url": v["imageinfo"][0]["url"], "source": "wiki"} for k, v in pages.items() if v.get("imageinfo")]
    except: return []

async def get_met(session, query, limit=2):
    try:
        search_url = f"https://collectionapi.metmuseum.org/public/collection/v1/search?hasImages=true&q={query}"
        async with session.get(search_url) as r:
            data = await r.json()
            object_ids = data.get("objectIDs", [])[:limit]
            
            results = []
            for obj_id in object_ids:
                obj_url = f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}"
                async with session.get(obj_url) as obj_r:
                    obj_data = await obj_r.json()
                    if obj_data.get("primaryImage"):
                        results.append({"url": obj_data["primaryImage"], "source": "met"})
            return results
    except: return []

async def fetch_from_all_sources(queries):
    async with aiohttp.ClientSession() as session:
        wiki_tasks = [fetch_wikimedia_images(session, q, limit=3) for q in queries]
        met_tasks = [get_met(session, q, limit=2) for q in queries]
        results = await asyncio.gather(*(wiki_tasks + met_tasks))
        pool = [item for sublist in results for item in sublist]
        return [r['url'] for r in pool]

# ── 2. CLIP RANKING ──────────────────────────────────────────────────────────
device = "cuda" if torch.cuda.is_available() else "cpu"
try:
    if 'model' not in globals() or 'preprocess' not in globals():
        print("Loading CLIP model...")
        model, preprocess = clip.load("ViT-B/32", device=device)
except Exception as e:
    print(f"Warning: CLIP load failed {e}")

def clip_rank(visual_prompt, candidate_urls):
    pos = clip.tokenize([visual_prompt]).to(device)
    neg = clip.tokenize(["modern photo, 3d render, screenshot, logo, text, watermark"]).to(device)
    
    scored = []
    for url in candidate_urls:
        try:
            r = requests.get(url, timeout=5, headers={"User-Agent": "VideoFactory/1.0"})
            img = preprocess(Image.open(BytesIO(r.content)).convert("RGB")).unsqueeze(0).to(device)
            with torch.no_grad():
                feats = model.encode_image(img)
                pos_score = torch.cosine_similarity(feats, model.encode_text(pos)).item()
                neg_score = torch.cosine_similarity(feats, model.encode_text(neg)).item()
                final_score = pos_score - (neg_score * 0.5)
            scored.append({"url": url, "score": final_score})
        except:
            continue
    return sorted(scored, key=lambda x: x["score"], reverse=True)

# ── 3. GEMINI VISION VALIDATION ──────────────────────────────────────────────
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10))
async def validate_image_relevance_async(image_url, visual_prompt):
    prompt = f"""Analyze this image against the following visual prompt:
    Visual Prompt: {visual_prompt}
    Does the image accurately represent the prompt?
    Return a JSON object with 'relevant' (bool) and 'confidence' (0.0 to 1.0)."""
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }],
            response_format={ "type": "json_object" }
        ))
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Validation error for {image_url}: {e}")
        raise

async def validate_and_select(candidates_ranked, visual_prompt):
    for candidate in candidates_ranked[:2]:
        try:
            result = await validate_image_relevance_async(candidate["url"], visual_prompt)
            if result.get("relevant") and result.get("confidence", 0) >= 0.7:
                return candidate["url"]
        except:
            continue
    return candidates_ranked[0]["url"] if candidates_ranked else None

# ── 4. CHROMA DB WRAPPERS ────────────────────────────────────────────────────
def check_cache(visual_prompt):
    try:
        results = collection.query(query_texts=[visual_prompt], n_results=1)
        if results and results.get('ids') and len(results['ids'][0]) > 0 and results['distances'][0][0] < 0.15:
            return results['metadatas'][0][0]['url']
    except: pass
    return None

def store_cache(visual_prompt, url, topic):
    try:
        collection.add(
            ids=[str(uuid.uuid4())],
            documents=[visual_prompt],
            metadatas=[{"url": url, "context": topic}]
        )
    except: pass

# ── 5. UNIFIED SCENE PROCESSOR ───────────────────────────────────────────────
def create_scene_video_v3(image_path, audio_path, output_mp4, motion_type="ZOOM_IN"):
    try:
        duration = librosa.get_duration(path=audio_path)
    except:
        duration = 5.0 # fallback
        
    vf = get_zoompan_filter(motion_type, duration)
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-i", audio_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-t", str(duration),
        "-pix_fmt", "yuv420p",
        output_mp4
    ]
    subprocess.run(cmd, check=True, capture_output=True)

async def process_scene_v3(manager, scene_id, scene_text, topic, paths):
    print(f"[Scene {scene_id}] 🧠 Step 1: AI Director analyzing scene...")
    try:
        plan = generate_scene_plan(topic, scene_text)
        queries = plan.get("searchQueries", [topic])
        motion = plan.get("cameraMotion", "ZOOM_IN")
    except Exception as e:
        print(f"Error generating plan: {e}, falling back to generic topic.")
        queries = [topic]
        motion = "ZOOM_IN"

    aud_p = paths["audio"] / f"scene_{scene_id:03d}.wav"
    img_p = paths["images"] / f"scene_{scene_id:03d}.jpg"
    vid_p = paths["video"] / f"scene_{scene_id:03d}.mp4"

    async def tts_task():
        gen = pipeline(scene_text, voice="am_michael")
        audio_segments = [s for _, _, s in gen]
        if audio_segments:
            sf.write(str(aud_p), np.concatenate(audio_segments), 24000)
            return True
        return False

    async def image_task():
        visual_prompt = queries[0]
        cache_hit = check_cache(visual_prompt)
        if cache_hit:
            print(f"[Scene {scene_id}] 💾 Cache HIT")
            return cache_hit

        print(f"[Scene {scene_id}] 🔍 Step 2: Fetching candidates...")
        pool = await fetch_from_all_sources(queries)
        if not pool:
            return None

        print(f"[Scene {scene_id}] 🤖 Step 3: CLIP ranking {len(pool)} candidates...")
        ranked = clip_rank(visual_prompt, pool)

        print(f"[Scene {scene_id}] ✅ Step 4: Gemini Vision validation...")
        best = await validate_and_select(ranked, visual_prompt)
        
        if best:
            store_cache(visual_prompt, best, topic)
        return best

    _, img_url = await asyncio.gather(tts_task(), image_task())

    if img_url:
        try:
            r = requests.get(img_url, timeout=10, headers={"User-Agent": "VideoFactory/1.0"})
            with open(img_p, "wb") as f:
                f.write(r.content)
        except Exception as e:
            print(f"[Scene {scene_id}] ⚠️ Download failed: {e}")
            Image.new("RGB", (1920, 1080), (44, 40, 34)).save(img_p)
    else:
        print(f"[Scene {scene_id}] ⚠️ No image found — using fallback texture")
        Image.new("RGB", (1920, 1080), (44, 40, 34)).save(img_p)

    print(f"[Scene {scene_id}] 🎥 Step 5: Rendering with motion={motion}")
    create_scene_video_v3(str(img_p), str(aud_p), str(vid_p), motion_type=motion)
    return str(vid_p)

print("✅ VIDEOFACTORY V3 ARCHITECTURE LOADED.")


if __name__ == "__main__":
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description="VideoFactory Terminal Runner")
    parser.add_argument('--topic', type=str, default='The Spanish Armada', help='Topic of the video')
    parser.add_argument('--niche', type=str, default='Speculative Historian', help='Niche of the video')
    args = parser.parse_args()
    
    if 'run_master_workflow_v3' in globals():
        asyncio.run(run_master_workflow_v3(args.topic, args.niche))
    elif 'run_master_workflow' in globals():
        asyncio.run(run_master_workflow(args.topic, args.niche))
    else:
        print("No workflow function found.")
