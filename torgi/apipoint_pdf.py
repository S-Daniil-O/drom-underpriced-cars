"""PDF-отчёт «История автомобиля по VIN» из данных apipoint.ru (вид как у отчётов Дрома):
сводка, данные ТС, владельцы, залог, ДТП, пробег с графиком, техосмотры, прошлые объявления с фото.
"""
import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

import requests
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONTS = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
         ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf")]
GREEN, RED, GREY, INK = colors.HexColor("#1a9b4b"), colors.HexColor("#d83a2e"), colors.HexColor("#6b7280"), colors.HexColor("#111827")
MAX_IMG = 3_000_000


def _fonts():
    for reg, bold in FONTS:
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("R", reg))
            pdfmetrics.registerFont(TTFont("B", bold))
            return True
    return False


def _img(url, w, h):
    """Картинка по ссылке (только уже проверенные адреса из apipoint.facts) → Image или None."""
    try:
        r = requests.get(url, timeout=30, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok or not r.headers.get("content-type", "").startswith("image"):
            return None
        data = r.raw.read(MAX_IMG + 1, decode_content=True)
        if len(data) > MAX_IMG:
            return None
        im = Image(io.BytesIO(data))
        k = min(w / im.imageWidth, h / im.imageHeight)
        im.drawWidth, im.drawHeight = im.imageWidth * k, im.imageHeight * k
        return im
    except Exception:  # noqa: BLE001 — битая картинка не должна ломать отчёт
        return None


def _km(x):
    return f"{x:,} км".replace(",", " ")


def build(v, f, lot_url=None):
    """bytes PDF или None (нет шрифта с кириллицей)."""
    if not _fonts():
        return None
    st = {
        "h1": ParagraphStyle("h1", fontName="B", fontSize=17, leading=21, textColor=INK),
        "sub": ParagraphStyle("sub", fontName="R", fontSize=9.5, leading=13, textColor=GREY),
        "h2": ParagraphStyle("h2", fontName="B", fontSize=13, leading=17, textColor=INK, spaceBefore=10, spaceAfter=4),
        "p": ParagraphStyle("p", fontName="R", fontSize=9.5, leading=13, textColor=INK),
        "note": ParagraphStyle("note", fontName="R", fontSize=8, leading=10.5, textColor=GREY),
        "ok": ParagraphStyle("ok", fontName="R", fontSize=10.5, leading=15, textColor=GREEN),
        "bad": ParagraphStyle("bad", fontName="B", fontSize=10.5, leading=15, textColor=RED),
        "na": ParagraphStyle("na", fontName="R", fontSize=10.5, leading=15, textColor=GREY),
    }
    e = escape
    P = lambda t, s="p": Paragraph(t, st[s])
    name = v.get("name") if not str(v.get("name", "")).startswith("http") else (f.get("vehicle") or {}).get("Марка, модель", "Автомобиль")
    out = [P(f"Отчёт об истории: {e(str(name))}", "h1"),
           P(f"Сформирован {datetime.now():%d.%m.%Y} по VIN: <b>{e(v.get('vin') or '')}</b> · лот №{v['n']}"
             + (f" · <a href='{e(lot_url)}' color='#2563eb'>страница торгов</a>" if lot_url else ""), "sub"),
           Spacer(1, 8)]

    # ---- сводка
    hist = f["history"]
    km = f.get("mileage") or (hist[-1][1] if hist else None)
    rows = []
    rows.append((f"Владельцев по ГИБДД: {len(f['owners'])}", "ok" if 0 < len(f["owners"]) <= 3 else "bad") if f["owners"]
                else ("Владельцы: нет данных", "na"))
    rows.append(("Числится в залоге", "bad") if f["zalog"] is True else ("Залогов не найдено", "ok") if f["zalog"] is False
                else ("Залог: нет данных", "na"))
    rows.append((f"Обнаружено ДТП: {len(f['dtp'])}", "bad") if f["dtp"] else ("ДТП не найдены", "ok") if f["dtp_known"]
                else ("ДТП: нет данных", "na"))
    rows.append((f"Пробег: {_km(km)}", "ok") if km else ("Пробег: нет данных", "na"))
    if f["rollback"]:
        rows.append(("Возможна скрутка пробега", "bad"))
    if f.get("to"):
        rows.append((f"Техосмотров: {len(f['to'])}", "ok"))
    rows.append((f"Объявлений о продаже: {len(f['offers'])}", "ok") if f["offers"] else ("Объявления о продаже не найдены", "na"))
    mark = {"ok": "•", "bad": "•", "na": "•"}     # точка есть в любом шрифте, цвет — из стиля строки
    summary = Table([[P(f"<font size=14>{mark[s]}</font>&nbsp;&nbsp;{e(t)}", s)] for t, s in rows], colWidths=[170 * mm])
    summary.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f4f6")),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    out += [P("Сводные данные", "h2"), summary]

    grid = TableStyle([("FONTNAME", (0, 0), (-1, -1), "R"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                       ("TEXTCOLOR", (0, 0), (-1, -1), INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
                       ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTNAME", (0, 0), (-1, 0), "B")])

    # ---- данные ТС
    if f.get("vehicle"):
        t = Table([[P(e(k), "note"), P(e(x))] for k, x in f["vehicle"].items()], colWidths=[55 * mm, 115 * mm])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb"))]))
        out += [P("Данные ТС по ГИБДД", "h2"), t]

    # ---- владельцы
    if f["owners"]:
        t = Table([["Владелец", "С", "По"]] + [[a, b, c or "н.в."] for a, b, c in f["owners"]],
                  colWidths=[80 * mm, 45 * mm, 45 * mm])
        t.setStyle(grid)
        out += [P(f"Владельцы по ГИБДД: {len(f['owners'])}", "h2"), t,
                P("Сведения о периодах владения предоставляются ГИБДД.", "note")]

    # ---- залог
    out.append(P("Залог", "h2"))
    out.append(P("<font color='#d83a2e'><b>Есть запись о залоге.</b></font> У банкротных лотов залог обычно снимается "
                 "после торгов — уточните у организатора." if f["zalog"] is True else
                 "Записей о залоге не найдено." if f["zalog"] is False else "Нет данных."))

    # ---- ДТП
    out.append(P(f"ДТП по данным ГИБДД: {len(f['dtp'])}" if f["dtp"] else "ДТП", "h2"))
    if f["dtp"]:
        t = Table([["Дата и время", "Тип происшествия", "Регион"]] + [[a["date"], P(e(a["type"])), P(e(a["region"]))] for a in f["dtp"]],
                  colWidths=[35 * mm, 80 * mm, 55 * mm])
        t.setStyle(grid)
        out.append(t)
    else:
        out.append(P("ДТП не найдены." if f["dtp_known"] else "Нет данных."))
    out.append(P("Данные ГИБДД о ДТП — с 2015 года; если сотрудников ГИБДД не вызывали, записи не будет.", "note"))

    # ---- пробег
    out.append(P(f"Записи о пробеге: {len(hist)}" if hist else "Пробег", "h2"))
    if hist:
        if f["rollback"]:
            out.append(P("<font color='#d83a2e'><b>Возможна скрутка:</b></font> позже записан меньший пробег, чем раньше."))
        t = Table([["Дата", "Пробег", "Источник"]] + [[f"{d:%d.%m.%Y}", _km(k), s] for d, k, s in hist],
                  colWidths=[35 * mm, 45 * mm, 90 * mm])
        t.setStyle(grid)
        out.append(t)
        if len(hist) >= 2:
            dr = Drawing(170 * mm, 55 * mm)
            lp = LinePlot()
            lp.x, lp.y, lp.width, lp.height = 30, 20, 170 * mm - 45, 55 * mm - 35
            lp.data = [[(d.year + (d.timetuple().tm_yday - 1) / 366, k / 1000) for d, k, _ in hist]]
            lp.lines[0].strokeColor, lp.lines[0].strokeWidth = colors.HexColor("#2563eb"), 1.5
            lp.xValueAxis.labelTextFormat = "%d"
            lp.xValueAxis.labels.fontName = lp.yValueAxis.labels.fontName = "R"
            lp.xValueAxis.labels.fontSize = lp.yValueAxis.labels.fontSize = 7
            lp.yValueAxis.valueMin = 0
            dr.add(lp)
            dr.add(String(0, 55 * mm - 10, "тыс. км", fontName="R", fontSize=7, fillColor=GREY))
            out.append(dr)
    elif km:
        out.append(P(f"Последний зафиксированный пробег: <b>{_km(km)}</b>."))
    else:
        out.append(P("Нет данных."))
    if f.get("to"):
        out.append(P(f"Техосмотров (ЕАИСТО): {len(f['to'])}, даты: " + ", ".join(f"{d:%d.%m.%Y}" for d in sorted(f["to"]))))

    # ---- объявления
    if f["offers"]:
        out.append(P(f"Объявления о продаже: {len(f['offers'])}", "h2"))
        for o in f["offers"][:6]:
            bits = [f"{o['date']:%d.%m.%Y}" if o["date"] else "", o["city"],
                    f"{o['price']:,} ₽".replace(",", " ") if o["price"] else "", _km(o["km"]) if o["km"] else "",
                    o.get("volume") or "", f"{o['hp']} л.с." if o.get("hp") else ""]
            head = ", ".join(e(b) for b in bits if b)
            if o["url"]:
                head = f"<a href='{e(o['url'])}' color='#2563eb'>{head}</a>"
            block = [P("<b>" + head + "</b>")]
            if o.get("descr"):
                block.append(P(e(o["descr"]), "note"))
            pics = [im for im in (_img(u, 40 * mm, 30 * mm) for u in o.get("images") or []) if im]
            if pics:
                block.append(Table([pics], colWidths=[42 * mm] * len(pics), hAlign="LEFT"))
            block.append(Spacer(1, 6))
            out.append(KeepTogether(block))

    # ---- фото
    photos = [im for im in (_img(u, 55 * mm, 42 * mm) for u in f["photos"][:9]) if im]
    if photos:
        out.append(P("Фотографии из прошлых объявлений", "h2"))
        rows = [photos[i:i + 3] for i in range(0, len(photos), 3)]
        rows[-1] += [""] * (3 - len(rows[-1]))
        out.append(Table(rows, colWidths=[57 * mm] * 3, hAlign="LEFT"))

    out += [Spacer(1, 10), P("Источник данных: apipoint.ru (ГИБДД, ЕАИСТО, реестр залогов, объявления). Данные актуальны "
                             "на момент запроса; перед покупкой сверяйте VIN и документы с автомобилем.", "note")]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                      title=f"Отчёт по VIN {v.get('vin', '')}", author="torgi").build(out)
    return buf.getvalue()
