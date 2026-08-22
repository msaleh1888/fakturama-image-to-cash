from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from openai import OpenAI
from PIL import Image

from models import OrderData


MODEL = "gpt-5.6"
MIME_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
EXTRACTION_INSTRUCTIONS = """Extract the order data from the supplied image.

- Extract only values that are visible in the image.
- Never invent missing data; return null for absent or unreadable values.
- Preserve references, customer IDs, aliases, SKUs, spelling, punctuation, and item order exactly as shown.
- Return dates as YYYY-MM-DD.
- Keep unit net, discount, VAT, visible line net, and totals as distinct source values.
- Return Decimal fields as bare numeric values without currency codes, currency symbols, or percent signs.
- Return only the structured result.
"""


def encode_image(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    mime_type = MIME_TYPES.get(suffix)
    if mime_type is None:
        raise ValueError("image must be a PNG, JPEG, or WEBP file")
    if not image_path.is_file():
        raise ValueError(f"image file does not exist: {image_path}")

    image_bytes = image_path.read_bytes()
    if len(image_bytes) > 250_000:
        with Image.open(BytesIO(image_bytes)) as source:
            source.thumbnail((700, 900), Image.Resampling.LANCZOS)
            if source.mode != "RGB":
                converted = Image.new("RGB", source.size, "white")
                if "A" in source.getbands():
                    converted.paste(source, mask=source.getchannel("A"))
                else:
                    converted.paste(source)
                source = converted
            buffer = BytesIO()
            source.save(buffer, format="JPEG", quality=85, optimize=True)
            image_bytes = buffer.getvalue()
            mime_type = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def extract_order(
    client: OpenAI,
    image_path: Path,
) -> tuple[OrderData, object | None]:
    image_url = encode_image(image_path)
    response = client.responses.parse(
        model=MODEL,
        reasoning={"effort": "none"},
        instructions=EXTRACTION_INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Extract the order from this image."},
                    {
                        "type": "input_image",
                        "image_url": image_url,
                        "detail": "original",
                    },
                ],
            }
        ],
        text_format=OrderData,
    )
    if response.output_parsed is None:
        raise ValueError("the API returned no parsed OrderData")
    return response.output_parsed, response.usage


def format_usage(usage: object | None) -> str:
    if usage is None:
        return ""
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if None in (input_tokens, output_tokens, total_tokens):
        return ""
    return (
        f" (usage: input={input_tokens}, output={output_tokens}, "
        f"total={total_tokens} tokens)"
    )
