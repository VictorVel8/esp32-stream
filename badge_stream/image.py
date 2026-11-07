import math
from PIL import Image, ImageOps


def transform(image, crop=None, size=240, fit="contain", zoom=1):
    if not math.isfinite(zoom) or not 1 <= zoom <= 4:
        raise ValueError("Zoom must be between 1 and 4")
    if crop is not None:
        x0, y0, x1, y1 = crop
        if not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
            raise ValueError("Crop must be a nonempty normalized rectangle")
        w, h = image.size
        box = (round(x0*w), round(y0*h), round(x1*w), round(y1*h))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("Crop is too small")
        image = image.crop(box)
    method = Image.Resampling.LANCZOS
    if fit == "cover":
        image = ImageOps.fit(image, (size, size), method=method)
    elif fit == "contain":
        image = ImageOps.pad(image, (size, size), method=method, color="black")
    else:
        raise ValueError("Unknown fitting mode")
    if zoom != 1:
        enlarged = round(size * zoom)
        image = image.resize((enlarged, enlarged), method)
        offset = (enlarged - size) // 2
        image = image.crop((offset, offset, offset + size, offset + size))
    return image
