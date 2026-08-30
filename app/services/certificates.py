import io
from PIL import Image
from pypdf import PdfReader,PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

def generate_certificate_pdf(template_bytes,file_type,mapping,student_name,usn,college_name,sport_name,category,certificate_number,event_date):
    w=float(mapping.get('page_width',842)); h=float(mapping.get('page_height',595))
    if file_type=='pdf': bg=template_bytes
    else:
        image=Image.open(io.BytesIO(template_bytes)).convert('RGB'); ib=io.BytesIO(); image.save(ib,'JPEG',quality=95); ib.seek(0); pb=io.BytesIO(); c=canvas.Canvas(pb,pagesize=(w,h)); c.drawImage(ImageReader(ib),0,0,width=w,height=h); c.showPage(); c.save(); bg=pb.getvalue()
    ob=io.BytesIO(); c=canvas.Canvas(ob,pagesize=(w,h)); c.setFillColor(HexColor(mapping.get('text_color','#000000'))); c.setFont('Helvetica-Bold',float(mapping.get('name_font_size',26))); c.drawCentredString(float(mapping.get('name_x',w/2)),float(mapping.get('name_y',310)),student_name); c.setFont('Helvetica',float(mapping.get('details_font_size',13))); date=event_date.strftime('%d %B %Y') if event_date else 'BNMIT ODYSSEY'; c.drawCentredString(float(mapping.get('details_x',w/2)),float(mapping.get('details_y',265)),f'{usn} • {college_name} • {sport_name} • {category} • {date}'); c.setFont('Helvetica',float(mapping.get('number_font_size',9))); c.drawString(float(mapping.get('number_x',70)),float(mapping.get('number_y',40)),certificate_number); c.showPage(); c.save(); ob.seek(0)
    page=PdfReader(io.BytesIO(bg)).pages[0]; page.merge_page(PdfReader(ob).pages[0]); out=io.BytesIO(); wr=PdfWriter(); wr.add_page(page); wr.write(out); return out.getvalue()
