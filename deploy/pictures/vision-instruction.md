# Making pictures

This box makes real pictures with Qwen-Image. When the person asks you to draw, paint, generate, create or make a
picture, photo, image, illustration, portrait, logo or wallpaper, don't draw it by hand in SVG, HTML or Python — run
`qwen-image` with the command tool, with a timeout of 300 (the first picture after a while takes about 80 seconds,
later ones about 20):

    qwen-image "<one detailed English description: subject, setting, style, lighting, composition, any words to show>" --out ~/.openclaw/workspace/pictures/<short-name>.png

Add `--width W --height H` for another shape (multiples of 32, up to 2048; 1024×1024 by default) and `--seed N` to
repeat one. It prints one line of JSON: the picture's `path` and how long it took, or an `error`. Then hand the picture
over with `present_file` and say a sentence or two about it — don't paste the JSON. When the request was spoken, say
the picture is in the chat. If it prints an error (pictures are turned off in Spark Swapper, or not enough memory is
free), tell the person what it says instead of drawing by hand. SVG, charts and code drawings are still right for
diagrams, or when the person asks for one.
