import subprocess, tempfile, os, re, textwrap
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import Color
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

app = FastAPI(title="PDF Sign Stamp (SIG → visual)")

# ---- настройки ----
STAMP_BLUE = Color(0/255, 70/255, 173/255)  # синий «печать»
STAMP_TZ = os.environ.get("STAMP_TZ", "Europe/Moscow")

# ---- шрифт ----
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    FONT_NAME = "DejaVuSans"
except Exception:
    FONT_NAME = "Helvetica"

# ---- утилиты ----
def run(cmd, input_bytes=None):
    p = subprocess.run(cmd, input=input_bytes, capture_output=True, check=False)
    out = (p.stdout or b"").decode(errors="ignore")
    err = (p.stderr or b"").decode(errors="ignore")
    return p.returncode, out, err

def _extract_fio_from_subject(subj: str):
    m = re.search(r"CN\s*=\s*([^,+/]+)", subj)
    if m and m.group(1).strip():
        return m.group(1).strip()
    sn = re.search(r"SURNAME\s*=\s*([^,+/]+)", subj)
    gn = re.search(r"(?:GIVENNAME|G)\s*=\s*([^,+/]+)", subj)
    if sn or gn:
        return " ".join([x.group(1).strip() for x in [sn, gn] if x]).strip() or None
    m = re.search(r"SN\s*=\s*([^,+/]+)", subj)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None

def _fmt_local(dt_utc: datetime) -> str:
    try:
        tz = ZoneInfo(STAMP_TZ)
        return dt_utc.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_utc.strftime("%d.%m.%Y %H:%M")

def _parse_signing_time_pretty(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=ZoneInfo("UTC"))
        return _fmt_local(dt)
    except Exception:
        pass
    for fmt in ("%b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S",
                "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=ZoneInfo("UTC"))
            return _fmt_local(dt)
        except Exception:
            continue
    m = re.search(r"(\d{4})[-.](\d{2})[-.](\d{2}).*?(\d{2}):(\d{2})", s)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("UTC"))
            return _fmt_local(dt)
        except Exception:
            pass
    return None

def _parse_signing_time_from_cms_print(sig_path: str) -> str | None:
    """Ищем блок 'object: signingTime' и читаем UTCTIME/GENERALIZEDTIME рядом."""
    for fmt in ("DER", "PEM"):
        rc, out, err = run(["openssl", "cms", "-cmsout", "-print", "-inform", fmt, "-in", sig_path])
        if rc != 0:
            continue
        lines = out.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"\bobject:\s*signingTime\b", line, re.IGNORECASE):
                tail = "\n".join(lines[i:i+8])
                m = re.search(r"GENERALIZEDTIME\s*:\s*([0-9]{14}Z)", tail)
                if m:
                    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
                    return _fmt_local(dt)
                m = re.search(r"UTCTIME\s*:\s*([0-9]{12}Z)", tail)
                if m:
                    dt = datetime.strptime(m.group(1), "%y%m%d%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
                    return _fmt_local(dt)
                m = re.search(r"UTCTIME\s*:\s*([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}\s+GMT)", tail)
                if m:
                    return _parse_signing_time_pretty(m.group(1))
    return None

def _parse_signing_time_from_asn1(sig_path: str) -> str | None:
    """Разбор `openssl asn1parse` (учитываем пробелы перед двоеточием)."""
    for fmt in ("DER", "PEM"):
        rc, out, err = run(["openssl", "asn1parse", "-inform", fmt, "-in", sig_path])
        if rc != 0:
            continue
        m = re.search(r"GENERALIZEDTIME\s*:\s*([0-9]{14}Z)", out)
        if m:
            dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
            return _fmt_local(dt)
        m = re.search(r"UTCTIME\s*:\s*([0-9]{12}Z)", out)
        if m:
            dt = datetime.strptime(m.group(1), "%y%m%d%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
            return _fmt_local(dt)
    return None

# ---- основная логика ----
def try_verify_and_extract(pdf_path, sig_path):
    info = {
        "matched": False,
        "signingTime": None,
        "subject": None,
        "issuer": None,
        "serial": None,
        "notBefore": None,
        "notAfter": None,
        "format": "unknown",
        "cn": None,
    }

    with open(sig_path, "rb") as f:
        head = f.read(256)
    head_txt = head.decode(errors="ignore")
    is_pgp = "BEGIN PGP SIGNATURE" in head_txt
    info["format"] = "pgp" if is_pgp else "cms"
    if is_pgp:
        return info

    # соответствие подписи контенту
    for fmt in ("DER", "PEM"):
        rc, out, err = run([
            "openssl", "cms", "-verify", "-noverify",
            "-inform", fmt, "-in", sig_path,
            "-content", pdf_path, "-out", os.devnull
        ])
        if rc == 0:
            info["matched"] = True
            break

    # signingTime
    st = _parse_signing_time_from_cms_print(sig_path)
    if not st:
        for fmt in ("DER", "PEM"):
            rc, out, err = run(["openssl", "cms", "-cmsout", "-print", "-inform", fmt, "-in", sig_path])
            if rc == 0 and ("signingTime" in out or "Signing Time" in out):
                m = re.search(r"(?:signingTime|Signing Time)\s*:\s*(.+)", out)
                if m:
                    st = _parse_signing_time_pretty(m.group(1).strip())
                    if st:
                        break
    if not st:
        st = _parse_signing_time_from_asn1(sig_path)
    info["signingTime"] = st or "—"

    # первый сертификат из подписи
    certs_pem = None
    for fmt in ("DER", "PEM"):
        rc, out, err = run(["openssl", "pkcs7", "-inform", fmt, "-in", sig_path, "-print_certs"])
        if rc == 0 and "BEGIN CERTIFICATE" in out:
            certs_pem = out
            break
    signer_pem = None
    if certs_pem:
        parts = certs_pem.split("-----END CERTIFICATE-----")
        signer_pem = parts[0] + "-----END CERTIFICATE-----"

    if signer_pem:
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".pem") as f:
            f.write(signer_pem); signer_path = f.name
        try:
            rc, out, err = run([
                "openssl", "x509", "-in", signer_path, "-noout",
                "-subject", "-issuer", "-serial", "-startdate", "-enddate",
                "-nameopt", "utf8,sep_comma_plus,space_eq"
            ])
            if rc == 0:
                for line in out.splitlines():
                    if line.startswith("subject="):
                        subj = line.split("subject=", 1)[1].strip()
                        info["subject"] = subj
                        info["cn"] = _extract_fio_from_subject(subj)
                    elif line.startswith("issuer="):
                        info["issuer"] = line.split("issuer=", 1)[1].strip()
                    elif line.startswith("serial="):
                        info["serial"] = line.split("serial=", 1)[1].strip()
                    elif line.startswith("notBefore="):
                        info["notBefore"] = line.split("notBefore=", 1)[1].strip()
                    elif line.startswith("notAfter="):
                        info["notAfter"] = line.split("notAfter=", 1)[1].strip()
        finally:
            os.unlink(signer_path)

    return info

def build_stamp_lines(info: dict) -> list[tuple[str, str]]:
    serial = info.get("serial") or "—"
    fio = info.get("cn") or (info.get("subject") or "—")
    when = info.get("signingTime") or "—"
    return [
        ("ДОКУМЕНТ ПОДПИСАН", "title"),
        ("Сведения об ЭП", "ribbon"),  # центр + внутренняя узкая рамка
        (f"Сертификат: {serial}", "data"),
        (f"Подписал: {fio}", "data"),
        (f"Дата подписания: {when}", "data"),
    ]

def overlay_stamp(pdf_bytes: bytes, lines: list[tuple[str,str]], pages="last", box_height_mm=28, font_size=9) -> bytes:
    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        if pages == "first" and i != 0:
            writer.add_page(page); continue
        if pages == "last" and i != len(reader.pages) - 1:
            writer.add_page(page); continue

        w = float(page.mediabox.width)
        h = float(page.mediabox.height)

        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        margin = 12 * mm

        # шире общий блок: 50% полезной ширины
        box_w = (w - 2 * margin) * 0.50
        box_h = box_height_mm * mm
        x = w - margin - box_w
        y = margin

        c.setStrokeColor(STAMP_BLUE)
        c.setFillColor(STAMP_BLUE)
        c.roundRect(x, y, box_w, box_h, 3 * mm, stroke=1, fill=0)

        # размеры шрифта
        size_title   = max(10, font_size + 1)
        size_ribbon  = font_size
        size_data    = max(6, font_size - 1)

        side_pad = 3 * mm
        top_pad  = 3 * mm
        line_gap = 1

        # 1) Заголовок по центру
        title = next(t for t,k in lines if k == "title")
        c.setFont(FONT_NAME, size_title)
        title_w = c.stringWidth(title, FONT_NAME, size_title)
        c.drawString(x + (box_w - title_w) / 2.0, y + box_h - top_pad - size_title, title)

        # 2) Узкая внутренняя лента (ещё уже)
        ribbon_text = next(t for t,k in lines if k == "ribbon")
        ribbon_h = 7 * mm
        inner_margin = 9 * mm      # БЫЛО 6 мм -> стало уже
        ribbon_y_top = y + box_h - top_pad - size_title - 2*mm
        inner_x = x + inner_margin
        inner_w = box_w - 2 * inner_margin
        inner_y = ribbon_y_top - ribbon_h
        c.roundRect(inner_x, inner_y, inner_w, ribbon_h, 2 * mm, stroke=1, fill=0)

        # текст в ленте — по центру
        c.setFont(FONT_NAME, size_ribbon)
        tw = c.stringWidth(ribbon_text, FONT_NAME, size_ribbon)
        text_x = inner_x + (inner_w - tw) / 2.0
        text_y = inner_y + (ribbon_h - size_ribbon) / 2.0 + 0.7*mm
        c.drawString(text_x, text_y, ribbon_text)

        # 3) Данные — ниже ленты
        ty = inner_y - 2*mm - size_data
        c.setFont(FONT_NAME, size_data)

        avg_char_w = 0.52 * size_data
        max_chars = max(16, int((inner_w) / avg_char_w))
        for text, kind in lines:
            if kind != "data":
                continue
            for wrapped in (textwrap.wrap(text, width=max_chars) or [" "]):
                c.drawString(inner_x, ty, wrapped)
                ty -= (size_data + line_gap)
                if ty < y + 3 * mm:
                    break

        c.save()
        buf.seek(0)
        overlay_page = PdfReader(buf).pages[0]
        page.merge_page(overlay_page)
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()

# ---- HTTP ----
@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html><body>
    <h3>PDF Sign Stamp (SIG → visual)</h3>
    <form action="/stamp" method="post" enctype="multipart/form-data">
      PDF: <input type="file" name="pdf" accept="application/pdf" required><br>
      SIG: <input type="file" name="sig" required><br>
      Pages:
      <select name="pages">
        <option value="all">all</option>
        <option value="first">first</option>
        <option value="last" selected>last</option>
      </select>
      Box height (mm): <input type="number" name="box_height_mm" value="28">
      Font size: <input type="number" name="font_size" value="9">
      <button type="submit">Сделать штамп</button>
    </form>
    </body></html>
    """

@app.post("/verify")
async def verify(sig: UploadFile = File(...), pdf: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False) as fpdf, tempfile.NamedTemporaryFile(delete=False) as fsig:
        fpdf.write(await pdf.read()); fsig.write(await sig.read())
        pdf_path, sig_path = fpdf.name, fsig.name
    try:
        info = try_verify_and_extract(pdf_path, sig_path)
        return JSONResponse(info)
    finally:
        os.unlink(pdf_path); os.unlink(sig_path)

@app.post("/stamp")
async def stamp(sig: UploadFile = File(...),
                pdf: UploadFile = File(...),
                pages: str = Form("last"),
                box_height_mm: int = Form(28),
                font_size: int = Form(9)):
    if pages not in ("all", "first", "last"):
        raise HTTPException(400, "pages must be one of: all|first|last")
    pdf_bytes = await pdf.read()
    with tempfile.NamedTemporaryFile(delete=False) as fpdf, tempfile.NamedTemporaryFile(delete=False) as fsig:
        fpdf.write(pdf_bytes); fsig.write(await sig.read())
        pdf_path, sig_path = fpdf.name, fsig.name
    try:
        info = try_verify_and_extract(pdf_path, sig_path)
        lines = build_stamp_lines(info)
        stamped = overlay_stamp(pdf_bytes, lines, pages=pages, box_height_mm=box_height_mm, font_size=font_size)
        return StreamingResponse(BytesIO(stamped), media_type="application/pdf",
                                 headers={"Content-Disposition": 'attachment; filename="stamped.pdf"'})
    finally:
        os.unlink(pdf_path); os.unlink(sig_path)
