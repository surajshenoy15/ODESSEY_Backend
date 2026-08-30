import io

from PIL import Image
from reportlab.pdfgen import canvas

from app.core.security import (
    create_access_token,
    decode_token,
    generate_otp,
    hash_password,
    verify_password,
)
from app.services.certificates import generate_certificate_pdf
from app.services.helpers import qr_png_bytes, safe_filename


def test_password_and_access_token():
    digest = hash_password("StrongPassword123!")
    assert verify_password("StrongPassword123!", digest)
    assert not verify_password("wrong", digest)
    token = create_access_token("abc", "PED", "PED")
    payload = decode_token(token, "access")
    assert payload["sub"] == "abc"
    assert payload["actor_type"] == "PED"


def test_otp_and_qr():
    otp = generate_otp()
    assert len(otp) == 6 and otp.isdigit()
    image = Image.open(io.BytesIO(qr_png_bytes("sample-token")))
    assert image.width > 0 and image.height > 0


def test_certificate_generation():
    background = io.BytesIO()
    c = canvas.Canvas(background, pagesize=(842, 595))
    c.drawString(50, 550, "BNMIT ODYSSEY")
    c.showPage()
    c.save()
    output = generate_certificate_pdf(
        background.getvalue(),
        "pdf",
        {},
        "Ananya Rao",
        "1BN23CS001",
        "Sample College",
        "Table Tennis",
        "Engineering Girls",
        "BSC/TAB/2026/000001",
        None,
    )
    assert output.startswith(b"%PDF")
    assert len(output) > 500


def test_safe_filename():
    assert safe_filename("A / B: 01") == "A-B-01"
