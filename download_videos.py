import os
import boto3
from botocore.client import Config

# MinIO Config
MINIO_URL = "http://57.128.172.4:9000"
ACCESS_KEY = "miniof6465a36"
SECRET_KEY = "A-vvqpI36Eg9cIGJzo_Y5qu2vFvKmGQf"
BUCKET_NAME = "youtube-artifacts"
DOWNLOAD_DIR = "rendered_videos"

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1"
)

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

print(f"Checking MinIO bucket '{BUCKET_NAME}'...")

try:
    response = s3.list_objects_v2(Bucket=BUCKET_NAME)
    if "Contents" in response:
        for obj in response["Contents"]:
            key = obj["Key"]
            if key.endswith(".mp4"):
                # preserve folder structure locally if desired, but we'll just download flat for easier access
                filename = key.replace("/", "_") 
                local_path = os.path.join(DOWNLOAD_DIR, filename)
                print(f"Downloading {key} -> {local_path} ...")
                s3.download_file(BUCKET_NAME, key, local_path)
        print("Download complete! All MP4s are in the 'rendered_videos' folder.")
    else:
        print("No files found in the bucket.")
except Exception as e:
    print(f"Error accessing MinIO: {e}")
