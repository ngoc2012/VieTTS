import opendataloader_pdf
# Batch all files in one call — each convert() spawns a JVM process, so repeated calls are slow
opendataloader_pdf.convert(
    input_path=["./outputs/page_15.pdf"],
    output_dir="outputs/",
    format="json,html,pdf,markdown,txt",
)