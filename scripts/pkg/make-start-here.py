#!/usr/bin/env python3
"""START-HERE.pdf: one page, big text, pictures. `make-start-here.py <out.pdf> <image-file-name>`"""
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

out = sys.argv[1] if len(sys.argv) > 1 else "START-HERE.pdf"
image_name = sys.argv[2] if len(sys.argv) > 2 else "pi-traffic-tester-X.img.xz"
W, H = letter
TEAL, INK, MUTE, SOFT = colors.HexColor("#0f766e"), colors.HexColor("#14202b"), colors.HexColor("#5b6b79"), colors.HexColor("#e0f2f0")
c = canvas.Canvas(out, pagesize=letter)
c.setTitle("Pi Automation Traffic Tester - START HERE")


def text(x, y, s, size=14, bold=False, color=INK):
    c.setFillColor(color)
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, y, s)


from reportlab.pdfbase.pdfmetrics import stringWidth


def para(x, y, s, size=12.4, width=372, bold=False, color=INK, lead=None):
    """wrapped text; returns the y below the last line"""
    lead = lead or size * 1.5
    font = "Helvetica-Bold" if bold else "Helvetica"
    line = ""
    for w in s.split(" "):
        t = (line + " " + w).strip()
        if stringWidth(t, font, size) > width and line:
            text(x, y, line, size, bold, color); y -= lead; line = w
        else:
            line = t
    if line:
        text(x, y, line, size, bold, color); y -= lead
    return y


def hr(y):
    c.setStrokeColor(colors.HexColor("#d5dde5")); c.setLineWidth(1); c.line(40, y, W - 40, y)


def num(x, y, n):
    c.setFillColor(TEAL)
    c.circle(x, y, 17, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(x, y - 8, str(n))


def laptop_card(x, y):                                   # laptop with an SD card going in
    c.setStrokeColor(INK); c.setLineWidth(2.2); c.setFillColor(colors.white)
    c.roundRect(x, y + 14, 74, 48, 5, stroke=1, fill=1)
    c.setFillColor(SOFT); c.rect(x + 6, y + 20, 62, 36, stroke=0, fill=1)
    c.setFillColor(TEAL); c.setFont("Helvetica-Bold", 11); c.drawCentredString(x + 37, y + 34, "Imager")
    c.setStrokeColor(INK); c.setFillColor(INK); c.roundRect(x - 8, y + 6, 90, 8, 3, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#1d4ed8")); c.setStrokeColor(INK)
    c.roundRect(x + 88, y + 18, 22, 28, 3, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#f5c542")); [c.rect(x + 92 + i * 4, y + 38, 2.5, 6, stroke=0, fill=1) for i in range(4)]


def pi_board(x, y):
    c.setStrokeColor(INK); c.setLineWidth(2.2); c.setFillColor(colors.HexColor("#1c8c5a"))
    c.roundRect(x, y, 96, 62, 6, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#c8cdd2")); c.rect(x + 36, y + 22, 24, 24, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#9aa3ab")); [c.rect(x + 74, y + 8 + i * 14, 18, 11, stroke=1, fill=1) for i in range(3)]
    c.setFillColor(colors.HexColor("#f5c542")); c.rect(x + 6, y + 6, 14, 8, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#ef4444")); c.circle(x + 12, y + 52, 3.5, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#22c55e")); c.circle(x + 24, y + 52, 3.5, stroke=0, fill=1)


def phone(x, y, label="pi1"):
    c.setStrokeColor(INK); c.setLineWidth(2.4); c.setFillColor(colors.white)
    c.roundRect(x, y, 48, 88, 8, stroke=1, fill=1)
    c.setFillColor(SOFT); c.roundRect(x + 5, y + 12, 38, 66, 3, stroke=0, fill=1)
    c.setFillColor(INK); c.setFont("Helvetica-Bold", 7); c.drawCentredString(x + 24, y + 66, "Set up this Pi")
    c.setFillColor(colors.white); c.setStrokeColor(MUTE); c.setLineWidth(.8); c.roundRect(x + 9, y + 46, 30, 11, 2, stroke=1, fill=1)
    c.setFillColor(INK); c.setFont("Helvetica", 7); c.drawString(x + 12, y + 49.5, label)
    c.setFillColor(TEAL); c.roundRect(x + 9, y + 24, 30, 13, 3, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 5.6); c.drawCentredString(x + 24, y + 28, "Name this Pi")


def wifi(x, y):
    c.setStrokeColor(TEAL); c.setLineWidth(3.2); c.setLineCap(1)
    for r in (8, 16, 24):
        c.arc(x - r, y - r, x + r, y + r, 45, 90)
    c.setFillColor(TEAL); c.circle(x, y - 1, 3, stroke=0, fill=1)


# ---- header
c.setFillColor(TEAL); c.rect(0, H - 70, W, 70, stroke=0, fill=1)
text(40, H - 34, "Pi Automation Traffic Tester", 28, True, colors.white)
text(40, H - 56, "Start here: flash a card, power on, name it on your phone.  No monitor, keyboard or cable needed.", 12.5, False, colors.white)

# ---- step 1
y = H - 112
num(52, y + 8, 1)
text(84, y + 8, "Write the card (laptop)", 20, True)
laptop_card(W - 150, y - 34)
yy = y - 20
for t in ["Open Raspberry Pi Imager. Device: Raspberry Pi 5. Operating system: Use custom.",
          f"Pick the file {image_name} (do not unpack it).",
          "Choose the SD card (16 GB or bigger). Optional: Edit settings, your Wi-Fi name and password.",
          "Press Write. Wait until it says it is done."]:
    yy = para(84, yy, t, 12.4, 372, lead=17)

# ---- step 2
y -= 142
hr(y + 22)
num(52, y - 14, 2)
text(84, y - 14, "Power on", 20, True)
pi_board(W - 150, y - 50)
yy = para(84, y - 38, "Put the card in the Pi and plug in the power (the official 5 V / 5 A USB-C).", 12.4, 372, lead=17)
para(84, yy, "Wait 2 minutes. The first start is slower than the next ones.", 12.4, 372, lead=17)

# ---- step 3
y -= 108
hr(y + 24)
num(52, y - 8, 3)
text(84, y - 8, "Name it (phone)", 20, True)
phone(W - 140, y - 74); wifi(W - 76, y - 4)
text(84, y - 34, "Join the Wi-Fi", 13.5, True); text(186, y - 34, "pi-setup", 18, True, TEAL)
text(84, y - 56, "Password", 13.5, True); text(150, y - 56, "password", 18, True, TEAL)
yy = para(84, y - 76, "If you gave Imager your own Wi-Fi, the Pi joined it: use that network instead.", 11, 372, color=MUTE, lead=14)
text(84, yy - 8, "Open", 13.5, True); text(124, yy - 8, "http://10.42.0.1:8080", 18, True, TEAL)
yy = para(84, yy - 28, "or pitester.local:8080. The phone may open this page by itself.", 11, 372, color=MUTE, lead=14)
para(84, yy - 6, "Type a name (pi1 is offered), then tap Name this Pi. Done.", 13.5, 372, bold=True, lead=18)

# ---- step 4 + facts
y -= 178
hr(y + 24)
num(52, y - 8, 4)
text(84, y - 8, "More Pis", 20, True)
yy = para(84, y - 32, "Do the same for each card, one at a time (every new card starts as pi-setup).", 12.4, 450, lead=17)
para(84, yy, "The next Pi finds the others by itself and offers the next name: pi2, pi3 ...", 12.4, 450, lead=17)

y -= 76
c.setFillColor(SOFT); c.roundRect(40, y - 108, W - 80, 118, 8, stroke=0, fill=1)
text(56, y - 6, "After naming", 15, True, TEAL)
yy = y - 28
for t in ["Each Pi's own Wi-Fi is its name (pi1, pi2 ...), password: password. App: http://10.42.0.1:8080 or pi1.local:8080",
          "Ethernet: plug it into your network for internet and updates, or into the equipment you test.",
          "Updates: Settings > Software update > Check for updates (the Pi needs a network with internet).",
          "Change the Wi-Fi name or password, or join your own Wi-Fi: Settings > Network."]:
    yy = para(56, yy, t, 11, W - 130, lead=14.5)
text(40, 44, "Safe by default: no login password on the card; SSH is off unless you turned it on in Imager.", 10.6, False, MUTE)
text(40, 28, "Lost the Pi? With no network for 3 minutes it starts its own Wi-Fi again, so a phone can always reach it.", 10.6, False, MUTE)
c.showPage()
c.save()
print(out)
