import io
import os
from pathlib import Path
from typing import Annotated, Optional
import uuid
import aiohttp
from fastapi import FastAPI
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

from api.storage import save_image_blob

AZURE_IMAGE_ENDPOINT = os.environ.get("AZURE_IMAGE_ENDPOINT", "EMPTY").rstrip("/")
AZURE_IMAGE_API_KEY = os.environ.get("AZURE_IMAGE_API_KEY", "EMPTY")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Image Tools", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ImageResponse(BaseModel):
    image_url: str = Field(
        title="Image URL",
        description="The URL of the generated or edited image.",
        examples=["https://example.com/image.png"],
    )


@app.get("/", include_in_schema=False)
async def read_main():
    return {"message": "Hello World from main app"}


class ImageEditRequest(BaseModel):
    image: str
    edits: dict  # Define the structure of edits as needed


@app.post(
    "/edit",
    include_in_schema=False,
    summary="Edit an image",
    description="This endpoint allows you to edit an image based on a detailed description and a provided image. It is based on the GPT-Image-1 model and can edit images in various styles.",
)
async def edit_image(request: ImageEditRequest):
    return {"message": "Image editing endpoint", "data": request.model_dump()}


class ImageCreateRequest(BaseModel):
    description: Annotated[
        str,
        Field(
            title="Description",
            description="A detailed description of the image to be created.",
            examples=[
                "A serene landscape with mountains in the background, a clear blue sky, and a calm lake reflecting the scenery."
            ],
        ),
    ]
    image: Annotated[
        Optional[str],
        Field(
            title="Image",
            description="An optional url or base64-encoded image to be used as a reference for the new image. If provided, it will be used to guide the generation process.",
            examples=["https://example.com/reference_image.png"],
        ),
    ] = None


@app.post(
    "/create",
    summary="Create an image",
    description="This endpoint allows you to create a new image based on a detailed description. It is based on the GPT-Image-1 model and can generate images in various styles.",
)
async def create_image(request: ImageCreateRequest) -> ImageResponse:
    api_version = "2025-04-01-preview"
    deployment_name = "gpt-image-1"
    endpoint = f"{AZURE_IMAGE_ENDPOINT}/openai/deployments/{deployment_name}/images/edits?api-version={api_version}"
    size: str = "1024x1024"
    quality: str = "low"

    # image_dict: dict[str, io.BytesIO] = {
    #    img: io.BytesIO(open(f"{BASE_DIR}/images/{img}", "rb").read())
    #    for img in os.listdir(f"{BASE_DIR}/images")
    #    if img.endswith(".png") and not img.startswith("_")
    # }

    image_dict = {
        "base.png": io.BytesIO(open(f"{BASE_DIR}/images/base.png", "rb").read())
    }

    if isinstance(request.image, str) and request.image.startswith("http"):
        # If the image is a URL, fetch it and convert to base64
        async with aiohttp.ClientSession() as session:
            async with session.get(request.image) as resp:
                if resp.status == 200:
                    if resp.headers.get("Content-Type") == "image/png":
                        image_data = await resp.read()
                        image_dict["user_provided_image.png"] = io.BytesIO(image_data)
                else:
                    raise Exception(f"Error fetching image from URL: {request.image}")

    async with aiohttp.ClientSession() as session:
        headers = {
            "api-key": AZURE_IMAGE_API_KEY,
        }

        form_data = aiohttp.FormData()

        if len(image_dict) == 1:
            key = list(image_dict.keys())[0]
            form_data.add_field(
                "image", image_dict[key], filename=key, content_type="image/png"
            )
        elif len(image_dict) > 1:
            for i, (key, img_data) in enumerate(image_dict.items()):
                form_data.add_field(
                    f"image[{i}]",
                    img_data,
                    filename=key,
                    content_type="image/png",
                )

        # add description, size, and quality
        additional_instructions = """
        DO NOT INCLUDE ANY OTHER LOGOS, JUST THE NEW LOGO INSPIRED BY THE PROVIDED IMAGE. DO NOT UNDER ANY CIRCUMSTANCE ADD A NIKE SWOOSH OR ANY OTHER LOGO.
        """
        form_data.add_field(
            "prompt",
            request.description + additional_instructions,
            content_type="text/plain",
        )
        form_data.add_field("size", size, content_type="text/plain")
        form_data.add_field("quality", quality, content_type="text/plain")

        # add mask
        mask_data = io.BytesIO(open(f"{BASE_DIR}/images/base_mask.png", "rb").read())
        form_data.add_field(
            "mask", mask_data, filename="mask.png", content_type="image/png"
        )

        async with session.post(endpoint, headers=headers, data=form_data) as response:
            if response.status == 200:
                result = await response.json()
                if result and "data" in result and len(result["data"]) > 0:
                    image_base64 = result["data"][0]["b64_json"]
                    image_name = f"{str(uuid.uuid4())}.png"
                    image_name = await save_image_blob(image_base64, "tools")
                    return ImageResponse(image_url=f"{BASE_URL}/{image_name}")

            else:
                error_message = await response.text()
                raise Exception(f"Error generating image: {error_message}")

        raise Exception("No image data returned from the API.")
