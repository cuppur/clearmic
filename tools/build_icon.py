"""Render the Muter waveform mark as a PNG and multi-size Windows icon."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def main():
    scale = 4
    size = 256 * scale
    yy, xx = np.mgrid[:size, :size].astype(np.float32) / size
    glow = .75 * np.exp(-4 * ((xx - .22) ** 2 + (yy - .10) ** 2))
    base = np.array([11, 23, 41]) + glow[..., None] * np.array([16, 43, 48])
    icon = Image.fromarray(np.uint8(base)).convert("RGBA")
    mask = Image.new("L", icon.size)
    draw = ImageDraw.Draw(mask)

    def box(coords):
        return tuple(int(x * scale) for x in coords)

    draw.rounded_rectangle(box((7, 7, 248, 248)), radius=57 * scale, fill=255)
    icon.putalpha(mask)
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(box((9, 9, 246, 246)), radius=55 * scale,
                           outline="#325569", width=2 * scale)

    # Quiet outer bars frame the brighter waveform at the center.
    for x, height, color in ((69, 25, "#5C99A2"), (98, 57, "#78DCCD"),
                             (128, 105, "#A5F8E0"), (158, 72, "#81E9D3"),
                             (187, 33, "#5C99A2")):
        draw.rounded_rectangle(box((x - 8, 128 - height / 2, x + 8, 128 + height / 2)),
                               radius=8 * scale, fill=color)

    # Four corner marks evoke a focus frame around the selected voice.
    for x, y, horizontal, vertical in ((47, 63, 1, 1), (209, 63, -1, 1),
                                       (47, 193, 1, -1), (209, 193, -1, -1)):
        draw.line([box((x + 17 * horizontal, y)), box((x, y)), box((x, y + 17 * vertical))],
                  fill="#6FA0AF", width=5 * scale, joint="curve")

    folder = Path(__file__).resolve().parent.parent / "muter" / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    icon.resize((96, 96), Image.Resampling.LANCZOS).save(folder / "muter.png")
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(
        folder / "muter.ico", sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


if __name__ == "__main__":
    main()
