import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
import io

def process_pdf(file_path):
    try:
        pdf_document = fitz.open(file_path)
        images = []
        for page_num in range(len(pdf_document)):
            page = pdf_document.load_page(page_num)
            pix = page.get_pixmap()
            img = Image.open(io.BytesIO(pix.tobytes()))
            images.append(img)
        return images
    except Exception as e:
        print(f"Error processing PDF: {e}")
        raise

def save_pdf_with_annotations(filename, annotations):
    try:
        pdf_document = fitz.open(f"temp/{filename}")
        pdf_writer = fitz.open()
        for page_num, page in enumerate(pdf_document):
            pixmap = page.get_pixmap()
            image = Image.open(io.BytesIO(pixmap.tobytes()))
            draw = ImageDraw.Draw(image)
            for annotation in annotations.get(str(page_num), []):
                text, x, y = annotation['text'], annotation['x'], annotation['y']
                font = ImageFont.load_default()
                draw.text((x, y), text, font=font, fill="black")
            pdf_bytes = io.BytesIO()
            image.save(pdf_bytes, format="PNG")
            pdf_image = fitz.open(stream=pdf_bytes.getvalue(), filetype="png")
            pdf_writer.insert_pdf(pdf_image)
        pdf_writer.save(f"temp/edited_{filename}")
    except Exception as e:
        print(f"Error saving PDF: {e}")
        raise
