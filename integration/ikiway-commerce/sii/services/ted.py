import base64
import re
from io import BytesIO

from pdf417gen import encode, render_image


TED_PATTERN = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?TED\b[^>]*>.*?</(?:[A-Za-z_][\w.-]*:)?TED\s*>",
    re.IGNORECASE | re.DOTALL,
)


def extract_ted_xml(document):
    response = document.sii_response or {}
    direct_payload = response.get("ted") or response.get("timbre") or document.qr_payload
    if direct_payload:
        return str(direct_payload).strip()

    for xml_value in (
        document.xml_signed,
        document.xml_received,
        document.xml_sent,
        document.xml_unsigned,
    ):
        match = TED_PATTERN.search(xml_value or "")
        if match:
            return match.group(0).strip()
    return ""


def render_pdf417_png(payload, *, columns=12, security_level=5, scale=3, ratio=2, padding=8):
    if not payload:
        return b""
    try:
        codes = encode(payload, columns=columns, security_level=security_level, encoding="iso-8859-1")
    except UnicodeEncodeError:
        codes = encode(payload, columns=columns, security_level=security_level, encoding="utf-8")
    image = render_image(codes, scale=scale, ratio=ratio, padding=padding)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_pdf417_data_uri(payload, **render_options):
    png = render_pdf417_png(payload, **render_options)
    if not png:
        return ""
    encoded = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{encoded}"
