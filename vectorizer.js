<script src="https://cdn.jsdelivr.net/npm/imagetracerjs@1.2.6/imagetracer_v1.2.6.js"></script>

<input type="file" id="file">
<a id="download" style="display:none">Download SVG</a>

<script>
const input = document.getElementById("file");
const download = document.getElementById("download");

input.addEventListener("change", () => {
    const file = input.files[0];
    if (!file) return;

    const reader = new FileReader();

    reader.onload = e => {
        const img = new Image();

        img.onload = () => {
            const options = {
                // ---- Main simplification ----
                ltres: 2.0,       // line tolerance
                qtres: 4.0,       // curve tolerance
                pathomit: 20,     // remove tiny paths

                // ---- Geometry ----
                rightangleenhance: true,
                strokewidth: 0,

                // ---- Color ----
                numberofcolors: 2,
                colorquantcycles: 3,

                // ---- Output ----
                roundcoords: 1,
                viewbox: true,
                desc: false,

                // Don't generate unnecessary strokes
                strokewidth: 0,

                // Reduce small variations
                blurradius: 0,
                blurdelta: 20
            };

            ImageTracer.imageToSVG(
                img,
                svg => {
                    const cleanSVG = optimizeSVG(svg);

                    const blob = new Blob(
                        [cleanSVG],
                        { type: "image/svg+xml" }
                    );

                    download.href = URL.createObjectURL(blob);
                    download.download = "vector.svg";
                    download.style.display = "inline-block";
                    download.textContent = "Download SVG";

                    document.body.appendChild(
                        document.createTextNode("")
                    );
                },
                options
            );
        };

        img.src = e.target.result;
    };

    reader.readAsDataURL(file);
});


function optimizeSVG(svg) {
    // Remove unnecessary metadata
    svg = svg
        .replace(/<title>.*?<\/title>/g, "")
        .replace(/<desc>.*?<\/desc>/g, "");

    // Round coordinates
    svg = svg.replace(
        /(-?\d+\.\d{2,})/g,
        n => Number(n).toFixed(1)
    );

    return svg;
}
</script>