from __future__ import annotations

import hmac
import io
import os

import torch
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, ImageOps
from transformers import AutoImageProcessor, AutoModel

MODEL_KEY = "facebook/dinov2-small"
MODEL_REVISION = "ed25f3a31f01632728cabb09d1542f84ab7b0056"
PROCESSOR_VERSION = "dinov2-small-384-v1"
MODEL_PATH = os.getenv("STAR_DINOV2_MODEL_PATH", "/opt/models/dinov2-small")
MAX_MEDIA_BYTES = 32 * 1024 * 1024

AUTH_TOKEN = os.getenv("STAR_EMBEDDING_TOKEN", "").strip()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# Embeddings stay on CPU so an optional evidence runtime cannot evict a resident
# VLM or consume accelerator memory reserved for production photo review.
device = torch.device("cpu")
processor = AutoImageProcessor.from_pretrained(MODEL_PATH, local_files_only=True)
model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True).to(device).eval()


def _require_token(request: Request) -> None:
    if not AUTH_TOKEN:
        return
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    candidate = supplied.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(candidate.encode(), AUTH_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _decode_preview(media: bytes) -> Image.Image:
    if not media or len(media) > MAX_MEDIA_BYTES:
        raise HTTPException(status_code=413, detail="bounded preview required")
    try:
        with Image.open(io.BytesIO(media)) as source:
            source.load()
            return ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="unsupported preview") from exc


@app.get("/healthz")
async def health() -> dict[str, object]:
    return {
        "ready": True,
        "model_key": MODEL_KEY,
        "model_revision": MODEL_REVISION,
        "device": device.type,
    }


@app.post("/embed")
async def embed(request: Request) -> dict[str, object]:
    _require_token(request)
    media = await request.body()
    image = _decode_preview(media)
    inputs = processor(images=image, return_tensors="pt")
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.inference_mode():
        output = model(**inputs).last_hidden_state[:, 0, :].float()
        normalized = torch.nn.functional.normalize(output, p=2, dim=-1)[0].cpu()
    vector = [round(float(value), 8) for value in normalized.tolist()]
    if len(vector) != 384:
        raise HTTPException(status_code=500, detail="unexpected embedding dimensions")
    return {
        "processor_version": PROCESSOR_VERSION,
        "model_key": MODEL_KEY,
        "model_revision": MODEL_REVISION,
        "embedding": vector,
    }
