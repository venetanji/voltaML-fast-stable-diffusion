import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from diffusers.utils import (
    CONFIG_NAME,
    DIFFUSERS_CACHE,
    FLAX_WEIGHTS_NAME,
    HF_HUB_OFFLINE,
    ONNX_WEIGHTS_NAME,
    PIL_INTERPOLATION,
    WEIGHTS_NAME,
    http_user_agent,
    is_safetensors_available,
)
from huggingface_hub import model_info  # type: ignore
from huggingface_hub._snapshot_download import snapshot_download
from PIL import Image

from api import websocket_manager
from api.websockets.data import Data
from core import shared
from core.errors import InferenceInterruptedError
from core.types import ImageMetadata
from core.utils import convert_image_to_base64

logger = logging.getLogger(__name__)


def cheap_approximation(sample: torch.Tensor):
    "Convert a tensor of latents to RGB"

    # Credit to Automatic111 stable-diffusion-webui
    # https://discuss.huggingface.co/t/decoding-latents-to-rgb-without-upscaling/23204/2

    coefs = (
        torch.tensor(
            [
                [0.298, 0.207, 0.208],
                [0.187, 0.286, 0.173],
                [-0.158, 0.189, 0.264],
                [-0.184, -0.271, -0.473],
            ]
        )
        .to(torch.float16)
        .to(sample.device)
    )

    cast_sample = sample.to(torch.float16).to(sample.device)

    x_sample = torch.einsum("lxy,lr -> rxy", cast_sample, coefs)

    return x_sample


def pytorch_callback(data: dict):
    "Send a websocket message to the client with the progress percentage and partial image"

    if shared.interrupt:
        shared.interrupt = False
        raise InferenceInterruptedError

    _x: torch.Tensor = data["x"][0]
    step = int(data["i"]) + 1
    send_image = step % shared.image_decode_steps == 0
    image: str = ""

    if send_image:
        decoded_rgb = cheap_approximation(_x)
        decoded_rgb = torch.clamp((decoded_rgb + 1.0) / 2.0, min=0.0, max=1.0)
        decoded_rgb = 255.0 * np.moveaxis(decoded_rgb.cpu().numpy(), 0, 2)
        decoded_rgb = decoded_rgb.astype(np.uint8)
        image = convert_image_to_base64(Image.fromarray(decoded_rgb))

    websocket_manager.broadcast_sync(
        data=Data(
            data_type="txt2img",
            data={
                "progress": int((step / shared.current_steps) * 100),
                "current_step": step,
                "total_steps": shared.current_steps,
                "image": image,
            },
        )
    )


def image_meta_from_file(path: Path) -> ImageMetadata:
    "Return image metadata from a file"

    with path.open("rb") as f:
        image = Image.open(f)
        text = image.text  # type: ignore

        try:
            metadata = ImageMetadata(
                prompt=text["prompt"],
                negative_prompt=text["negative_prompt"],
                height=int(text["height"]),
                width=int(text["width"]),
                seed=text["seed"],
                guidance_scale=float(text["guidance_scale"]),
                steps=int(text["steps"]),
                model=text["model"],
            )
        except KeyError:
            metadata = ImageMetadata(
                prompt="",
                negative_prompt="",
                height=0,
                width=0,
                seed="",
                guidance_scale=0.0,
                steps=0,
                model="",
            )

    return metadata


def preprocess_image(image):
    "Preprocess an image for the model"

    if isinstance(image, torch.Tensor):
        return image
    elif isinstance(image, Image.Image):
        image = [image]

    if isinstance(image[0], Image.Image):
        w, h = image[0].size
        w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32

        image = [
            np.array(i.resize((w, h), resample=PIL_INTERPOLATION["lanczos"]))[None, :]  # type: ignore
            for i in image
        ]
        image = np.concatenate(image, axis=0)
        image = np.array(image).astype(np.float32) / 255.0
        image = image.transpose(0, 3, 1, 2)
        image = 2.0 * image - 1.0  # type: ignore
        image = torch.from_numpy(image)
    elif isinstance(image[0], torch.Tensor):
        image = torch.cat(image, dim=0)  # type: ignore
    return image