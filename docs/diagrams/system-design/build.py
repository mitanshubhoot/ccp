"""Build the CCP system-design diagram set.

Seven diagrams of CCP: the target architecture (docs/reports/ccp-target-architecture.md)
as reshaped by the build through LT9 (spec.md A-172), suggest-only remediation included.
Each is written as a light and a dark self-contained HTML file, plus an index page.

    python docs/diagrams/system-design/build.py

Geometry sits on a 4px grid; every connector is orthogonal; every label has a
paper mask with a gap from its stroke. Colours and fonts follow the default
skin of the diagram-design skill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent

SANS = "'Geist', system-ui, sans-serif"
MONO = "'Geist Mono', ui-monospace, monospace"
SERIF = "'Instrument Serif', 'Noto Serif', serif"
FONT_LINK = (
    "https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1"
    "&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600"
    "&family=Noto+Serif:ital@0;1&display=swap"
)


@dataclass(frozen=True)
class Palette:
    name: str
    paper: str
    paper2: str
    ink: str
    muted: str
    soft: str
    accent: str
    link: str
    white: str
    ink_rgb: str
    accent_rgb: str
    muted_rgb: str

    def ink_a(self, a: float) -> str:
        return f"rgba({self.ink_rgb},{a})"

    def accent_a(self, a: float) -> str:
        return f"rgba({self.accent_rgb},{a})"

    def muted_a(self, a: float) -> str:
        return f"rgba({self.muted_rgb},{a})"


LIGHT = Palette("light", "#f5f5f5", "#ececec", "#2d3142", "#4f5d75", "#5a6580",
                "#bf4520", "#2e5aa8", "#ffffff", "45,49,66", "191,69,32", "79,93,117")
DARK = Palette("dark", "#2d3142", "#393e53", "#f5f5f5", "#bfc0c0", "#949eb2",
               "#f08a59", "#739fdf", "#393e53", "245,245,245", "240,138,89", "191,192,192")


# --------------------------------------------------------------------------- primitives


def r4(v: float) -> int:
    return int(math.ceil(v / 4.0) * 4)


def mono_w(s: str, size: float = 8, ls: float = 0.06) -> float:
    return len(s) * (0.62 + ls) * size


def sans_w(s: str, size: float = 12) -> float:
    return len(s) * 0.60 * size


def num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def text(x, y, s, *, fill, size=9, family=MONO, weight=None, anchor="start",
         ls=None, italic=False) -> str:
    a = [f'x="{num(x)}"', f'y="{num(y)}"', f'fill="{fill}"', f'font-size="{size}"',
         f'font-family="{family}"']
    if weight:
        a.append(f'font-weight="{weight}"')
    if anchor != "start":
        a.append(f'text-anchor="{anchor}"')
    if ls:
        a.append(f'letter-spacing="{ls}"')
    if italic:
        a.append('font-style="italic"')
    return f'<text {" ".join(a)}>{escape(s)}</text>'


def kinds(P: Palette) -> dict[str, tuple[str, str, str, str, str]]:
    """fill, stroke, dash attr, tag stroke, tag text."""
    return {
        "focal": (P.accent_a(0.08), P.accent, "", P.accent_a(0.5), P.accent),
        "backend": (P.white, P.ink, "", P.ink_a(0.4), P.ink),
        "store": (P.ink_a(0.05), P.muted, "", P.muted_a(0.5), P.muted),
        "external": (P.ink_a(0.03), P.ink_a(0.30), "", P.ink_a(0.22), P.soft),
        "security": (P.accent_a(0.05), P.accent_a(0.5), ' stroke-dasharray="4,4"',
                     P.accent_a(0.4), P.accent),
        "optional": (P.ink_a(0.02), P.ink_a(0.35), ' stroke-dasharray="4,3"',
                     P.ink_a(0.22), P.soft),
    }


def tag_chip(P, x, y, tag, stroke, fill) -> str:
    tw = r4(mono_w(tag, 7, 0.08) + 8)
    return (f'<rect x="{x}" y="{y}" width="{tw}" height="12" rx="2" fill="none" '
            f'stroke="{stroke}" stroke-width="0.8"/>'
            + text(x + tw / 2, y + 9, tag, fill=fill, size=7, anchor="middle", ls="0.08em"))


def box(P, x, y, w, h, kind, rx=6) -> str:
    fill, stroke, dash, _, _ = kinds(P)[kind]
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{P.paper}"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1"{dash}/>')


def node(P, x, y, w, h, kind, tag, name, subs=(), rx=6) -> str:
    _, _, _, tstroke, tfill = kinds(P)[kind]
    out = [box(P, x, y, w, h, kind, rx)]
    if tag:
        out.append(tag_chip(P, x + 8, y + 6, tag, tstroke, tfill))
    top = y + 18 if tag else y
    block = 12 + 14 * len(subs)
    base = top + (y + h - top - block) / 2 + 10
    cx = x + w / 2
    out.append(text(cx, base, name, fill=P.ink, size=12, family=SANS, weight=600,
                    anchor="middle"))
    for i, s in enumerate(subs):
        out.append(text(cx, base + 15 + 13 * i, s, fill=P.muted, anchor="middle"))
    return "\n".join(out)


def node_chips(P, x, y, w, h, kind, tag, name, sub, chips, dashed=()) -> str:
    """Deployment node: header, then artifact chips (name left, version right)."""
    _, _, _, tstroke, tfill = kinds(P)[kind]
    out = [box(P, x, y, w, h, kind),
           tag_chip(P, x + 8, y + 6, tag, tstroke, tfill),
           text(x + w / 2, y + 36, name, fill=P.ink, size=12, family=SANS, weight=600,
                anchor="middle"),
           text(x + w / 2, y + 51, sub, fill=P.muted, anchor="middle")]
    for i, (cname, ver) in enumerate(chips):
        cy = y + 64 + 32 * i
        dash = ' stroke-dasharray="4,3"' if i in dashed else ""
        out.append(f'<rect x="{x + 12}" y="{cy}" width="{w - 24}" height="24" rx="4" '
                   f'fill="{P.ink_a(0.05)}" stroke="{P.muted}" stroke-width="0.8"{dash}/>')
        out.append(text(x + 20, cy + 16, cname, fill=P.ink, size=12, family=SANS))
        out.append(text(x + w - 20, cy + 16, ver, fill=P.muted, anchor="end"))
    return "\n".join(out)


def label(P, cx, cy, s, color=None) -> str:
    w = r4(mono_w(s) + 8)
    return (f'<rect x="{num(cx - w / 2)}" y="{num(cy - 6)}" width="{w}" height="12" rx="2" '
            f'fill="{P.paper}"/>'
            + text(cx, cy + 3, s, fill=color or P.muted, size=8, anchor="middle",
                   ls="0.06em"))


def lab_above(P, cx, ly, s, c=None):
    return label(P, cx, ly - 14, s, c)


def lab_below(P, cx, ly, s, c=None):
    return label(P, cx, ly + 14, s, c)


def lab_left(P, lx, cy, s, c=None):
    return label(P, lx - 8 - r4(mono_w(s) + 8) / 2, cy, s, c)


def lab_right(P, lx, cy, s, c=None):
    return label(P, lx + 8 + r4(mono_w(s) + 8) / 2, cy, s, c)


def _sgn(v: float) -> int:
    return (v > 0) - (v < 0)


def path_d(pts, r=8) -> str:
    d = [f"M{num(pts[0][0])},{num(pts[0][1])}"]
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
        a = (x1 - _sgn(x1 - x0) * r, y1 - _sgn(y1 - y0) * r)
        b = (x1 + _sgn(x2 - x1) * r, y1 + _sgn(y2 - y1) * r)
        d.append(f"L{num(a[0])},{num(a[1])} Q{num(x1)},{num(y1)} {num(b[0])},{num(b[1])}")
    d.append(f"L{num(pts[-1][0])},{num(pts[-1][1])}")
    return " ".join(d)


STYLE = {"muted": ("muted", "arrow"), "accent": ("accent", "arrow-accent"),
         "link": ("link", "arrow-link")}


def arrow(P, pts, style="muted", dashed=False, marker="filled", width=None) -> str:
    attr, mid = STYLE[style]
    color = getattr(P, attr)
    if marker == "open":
        mid = "arrow-open"
    w = width or (1.4 if style == "accent" else 1.2)
    dash = ' stroke-dasharray="5,4"' if dashed else ""
    m = f' marker-end="url(#{mid})"' if marker else ""
    common = f'fill="none" stroke="{color}" stroke-width="{w}"{dash}{m}'
    if len(pts) == 2:
        (x1, y1), (x2, y2) = pts
        return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" {common}/>'
    return f'<path d="{path_d(pts)}" {common}/>'


def plain(P, pts, color, width=1.2, dash=None) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    if len(pts) == 2:
        (x1, y1), (x2, y2) = pts
        return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                f'stroke-width="{width}"{d}/>')
    return f'<path d="{path_d(pts)}" fill="none" stroke="{color}" stroke-width="{width}"{d}/>'


def zone(P, x, y, w, h, s, dashed=False, lx=None) -> str:
    stroke = P.ink_a(0.22) if dashed else P.ink_a(0.12)
    dash = ' stroke-dasharray="4,4"' if dashed else ""
    lw = r4(mono_w(s, 7, 0.14) + 12)
    lx = x + 12 if lx is None else lx
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{P.ink_a(0.02)}" '
            f'stroke="{stroke}" stroke-width="0.8"{dash}/>'
            f'<rect x="{lx}" y="{y + 4}" width="{lw}" height="12" rx="2" fill="{P.paper}"/>'
            + text(lx + lw / 2, y + 13, s, fill=P.soft, size=7, anchor="middle",
                   ls="0.14em"))


def callout(P, x, y, lines, leader, anchor="start", accent=False) -> str:
    color = P.accent if accent else P.ink
    lead = P.accent_a(0.5) if accent else P.ink_a(0.4)
    (sx, sy), (qx, qy), (ex, ey) = leader
    out = [f'<path d="M{sx},{sy} Q{qx},{qy} {ex},{ey}" fill="none" stroke="{lead}" '
           f'stroke-width="1" stroke-dasharray="4,3"/>',
           f'<circle cx="{ex}" cy="{ey}" r="2" fill="{color}"/>']
    for i, s in enumerate(lines):
        out.append(text(x, y + 18 * i, s, fill=color, size=14, family=SERIF, italic=True,
                        anchor=anchor))
    return "\n".join(out)


def stop_bar(P, x, y, horizontal=True) -> str:
    if horizontal:
        return plain(P, [(x - 12, y), (x + 12, y)], P.accent, 2)
    return plain(P, [(x, y - 12), (x, y + 12)], P.accent, 2)


def defs(P) -> str:
    def mk(i, c):
        return (f'<marker id="{i}" markerWidth="8" markerHeight="6" refX="7" refY="3" '
                f'orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{c}"/></marker>')
    return ("<defs>" + mk("arrow", P.muted) + mk("arrow-accent", P.accent)
            + mk("arrow-link", P.link)
            + '<marker id="arrow-open" markerWidth="8" markerHeight="6" refX="7" refY="3" '
            f'orient="auto"><polyline points="0 0, 8 3, 0 6" fill="none" stroke="{P.muted}" '
            'stroke-width="1.2"/></marker></defs>')


def legend(P, y, W, items) -> str:
    out = [f'<line x1="40" y1="{y}" x2="{W - 40}" y2="{y}" stroke="{P.ink_a(0.10)}" '
           f'stroke-width="0.8"/>',
           text(40, y + 16, "LEGEND", fill=P.muted, size=8, ls="0.18em")]
    x, iy = 40, y + 36
    k = kinds(P)
    for kind, s in items:
        if kind in k:
            fill, stroke, dash, _, _ = k[kind]
            out.append(f'<rect x="{x}" y="{iy - 9}" width="14" height="10" rx="2" '
                       f'fill="{fill}" stroke="{stroke}" stroke-width="1"{dash}/>')
            tx = x + 20
        elif kind == "chip":
            out.append(f'<rect x="{x}" y="{iy - 9}" width="20" height="10" rx="3" '
                       f'fill="{P.ink_a(0.05)}" stroke="{P.muted}" stroke-width="0.8"/>')
            tx = x + 26
        elif kind == "blocked":
            out.append(plain(P, [(x, iy - 4), (x + 24, iy - 4)], P.accent, 1.4, "5,4"))
            out.append(plain(P, [(x + 26, iy - 10), (x + 26, iy + 2)], P.accent, 2))
            tx = x + 36
        elif kind.startswith("line:"):
            style, *rest = kind[5:].split(",")
            dashed = "dashed" in rest
            mk = "open" if "open" in rest else "filled"
            out.append(arrow(P, [(x, iy - 4), (x + 28, iy - 4)], style, dashed, mk))
            tx = x + 36
        elif kind.startswith("plain:"):
            style = kind[6:]
            out.append(plain(P, [(x, iy - 4), (x + 28, iy - 4)], getattr(P, style), 1.2))
            tx = x + 36
        elif kind == "text":
            tx = x
        else:
            raise ValueError(kind)
        out.append(text(tx, iy, s, fill=P.muted, size=8.5, family=SANS))
        x = r4(tx + sans_w(s, 8.5) + 28)
    return "\n".join(out)


# --------------------------------------------------------------------------- 01 architecture


def d01_architecture(P):
    W, H = 1280, 592
    o = [zone(P, 248, 104, 984, 120, "CODE DECIDES · FACTS AND STATUS"),
         zone(P, 248, 392, 472, 120, "CODE RENDERS · PEOPLE DECIDE"),
         zone(P, 760, 392, 472, 120, "MODELS PROPOSE · READ-ONLY TOOLS", lx=916)]
    # connectors, before nodes
    o += [arrow(P, [(184, 168), (272, 168)], "link"),
          arrow(P, [(440, 168), (528, 168)]),
          arrow(P, [(696, 168), (784, 168)]),
          arrow(P, [(952, 168), (1040, 168)]),
          arrow(P, [(840, 200), (840, 424)]),
          plain(P, [(896, 424), (896, 236)], P.accent, 1.4, "5,4"),
          stop_bar(P, 896, 232),
          arrow(P, [(1124, 424), (1124, 200)]),
          arrow(P, [(952, 456), (1040, 456)]),
          arrow(P, [(784, 456), (696, 456)]),
          arrow(P, [(528, 456), (440, 456)]),
          arrow(P, [(272, 456), (184, 456)], dashed=True)]
    o += [lab_above(P, 228, 168, "API READS", P.link),
          lab_above(P, 484, 168, "RAW + FACTS"),
          lab_above(P, 740, 168, "AS-OF FACTS"),
          lab_above(P, 996, 168, "VERDICT"),
          lab_left(P, 840, 300, "ON FAIL"),
          lab_right(P, 896, 332, "NEVER A STATUS", P.accent),
          lab_left(P, 1124, 312, "READ ONLY"),
          lab_above(P, 996, 456, "TOOL CALL"),
          lab_above(P, 740, 456, "PROPOSAL"),
          lab_above(P, 484, 456, "CHECKED FIX"),
          lab_above(P, 228, 456, "COPY + APPLY")]
    o.append(callout(P, 280, 300, ["CCP holds no write credential: a fix counts",
                                   "only when a later sync reads it in place."],
                     ((272, 312), (228, 318), (184, 372))))
    o += [node(P, 40, 136, 144, 352, "external", "EXT", "Customer systems",
               ["GitHub · AWS", "HRIS · IdP", "uploads"]),
          node(P, 272, 136, 168, 64, "backend", "SYNC", "Connectors",
               ["complete syncs, receipts"]),
          node(P, 528, 136, 168, 64, "store", "STORE", "Evidence store",
               ["raw write-once · facts"]),
          node(P, 784, 136, 168, 64, "focal", "RULES", "Rules engine",
               ["versioned · pinned inputs"]),
          node(P, 1040, 136, 168, 64, "store", "STATE", "Compliance state",
               ["bitemporal · rollup"]),
          node(P, 272, 424, 168, 64, "security", "GATE", "Approvals",
               ["signed-in · hash-bound"]),
          node(P, 528, 424, 168, 64, "backend", "CODE", "Fix renderer",
               ["templates · offline checks"]),
          node(P, 784, 424, 168, 64, "backend", "AGENT", "Agents", ["LangGraph + Claude"]),
          node(P, 1040, 424, 168, 64, "security", "POLICY", "Tool gateway",
               ["typed · fails closed"])]
    o.append(legend(P, 540, W, [("focal", "Decides status"), ("backend", "CCP code"),
                                ("store", "Store"), ("security", "Gate or policy"),
                                ("external", "Customer side"),
                                ("line:link", "External call"),
                                ("line:muted", "Internal flow"),
                                ("line:muted,dashed", "Done by a person"),
                                ("blocked", "Blocked path")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- 02 deployment


def d02_deployment(P):
    W, H = 1280, 644
    o = [zone(P, 40, 72, 728, 488, "CCP PRODUCTION ACCOUNT · ONE CELL (US-1 SHOWN)", True),
         zone(P, 808, 72, 432, 160, "CCP AUDIT ACCOUNT", True),
         zone(P, 808, 256, 432, 200, "CUSTOMER AWS ACCOUNT", True)]
    o += [f'<circle cx="520" cy="40" r="4" fill="{P.link}"/>',
          text(532, 43, "reviewers' browsers", fill=P.muted),
          arrow(P, [(520, 44), (520, 216)], "link"),
          arrow(P, [(448, 248), (352, 248)]),
          arrow(P, [(448, 340), (352, 340)]),
          arrow(P, [(632, 216), (632, 156), (840, 156)], "accent", dashed=True),
          arrow(P, [(728, 304), (840, 304)], "link"),
          arrow(P, [(704, 392), (704, 520), (840, 520)], "link")]
    o += [lab_right(P, 520, 124, "HTTPS:443", P.link),
          lab_above(P, 400, 248, "TLS:5432"),
          lab_below(P, 400, 340, "HTTPS · PUT"),
          lab_above(P, 740, 156, "HOURLY, SIGNED", P.accent),
          lab_above(P, 784, 304, "ASSUME ROLE", P.link),
          lab_above(P, 772, 520, "HTTPS:443", P.link)]
    o.append(callout(P, 384, 478, ["eu-1 is the same module in eu-central-1:",
                                   "Bedrock in-region, Voyage's EU endpoint."],
                     ((520, 460), (520, 428), (520, 396))))
    o += [node_chips(P, 72, 112, 280, 168, "focal", "MANAGED", "RDS PostgreSQL",
                     "Multi-AZ · forced RLS",
                     [("PostgreSQL", "17"), ("pgvector", "0.8.2+, HNSW per tenant"),
                      ("read replica", "reports, lag ≤ 30 s")]),
          node_chips(P, 72, 328, 280, 136, "store", "MANAGED", "S3 raw zone",
                     "write once · tenant-first keys",
                     [("Object Lock", "governance + event holds"),
                      ("KMS", "one key per tenant")]),
          node_chips(P, 448, 216, 280, 176, "backend", "FARGATE", "ECS on Fargate",
                     "ccp image + PgBouncer · ALB + Cognito",
                     [("api", "FastAPI + HTMX · ×2"), ("worker", "queue + agents · ×2–8"),
                      ("pooler", "PgBouncer, transaction · ×2")]),
          node_chips(P, 840, 104, 368, 104, "store", "MANAGED", "Checkpoint bucket",
                     "Object Lock compliance mode",
                     [("chain checkpoint", "hourly · signed")]),
          node_chips(P, 840, 288, 368, 136, "external", "IAM", "Customer read role",
                     "external ID · no write actions",
                     [("ccp-read stack", "SecurityAudit v94 + denies"),
                      ("lookups", "Config history · CloudTrail")]),
          node(P, 840, 480, 368, 80, "external", "SAAS", "External services",
               ["Anthropic API (us-1) · Bedrock in-region (eu-1)",
                "Voyage · Langfuse (metadata only) · GitHub (read)"])]
    o.append(legend(P, 592, W, [("focal", "System of record"), ("backend", "Compute"),
                                ("store", "Managed storage"),
                                ("external", "Outside CCP's accounts"),
                                ("chip", "Deployed artifact or setting"),
                                ("line:link", "Crosses an account"),
                                ("line:muted", "Inside one account"),
                                ("line:accent,dashed", "Integrity checkpoint")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- 03 sequence


def d03_sequence(P):
    W, H = 1280, 900
    xs = {"cust": 128, "conn": 384, "rules": 640, "look": 896, "fix": 1152}
    o = []
    # phase dividers, behind everything
    phases = [(124, "1 · DETECT"), (284, "2 · LOOK UP"), (432, "3 · SUGGEST"),
              (604, "4 · APPLY AND CONFIRM")]
    for y, _s in phases[1:]:
        o.append(plain(P, [(40, y), (1240, y)], P.ink_a(0.14), 1, "2,4"))
    for cx in xs.values():
        o.append(plain(P, [(cx, 104), (cx, 832)], P.ink_a(0.22), 1, "3,3"))
    for y, s in phases:
        lw = r4(mono_w(s, 8, 0.14) + 12)
        o.append(f'<rect x="140" y="{y - 8}" width="{lw}" height="16" rx="2" fill="{P.paper}"/>')
        o.append(text(146, y + 3, s, fill=P.soft, size=8, ls="0.14em"))
    bars = {"cust": [(152, 204), (532, 588), (620, 684)],
            "conn": [(152, 248), (352, 404), (664, 720)],
            "rules": [(232, 328), (704, 792)],
            "look": [(312, 476)],
            "fix": [(460, 592), (776, 824)]}
    for k, spans in bars.items():
        for a, b in spans:
            o.append(f'<rect x="{xs[k] - 4}" y="{a}" width="8" height="{b - a}" '
                     f'fill="{P.ink_a(0.06)}" stroke="{P.muted}" stroke-width="0.8"/>')

    def msg(a, b, y, s, style="muted", dashed=False, mk="filled", lx=None):
        x1, x2 = xs[a], xs[b]
        d = 1 if x2 > x1 else -1
        o.append(arrow(P, [(x1 + 4 * d, y), (x2 - 4 * d, y)], style, dashed, mk))
        color = {"muted": P.muted, "link": P.link, "accent": P.accent}[style]
        o.append(lab_above(P, lx if lx else (x1 + x2) / 2, y, s, color))

    msg("conn", "cust", 160, "READ BUCKET + ACCOUNT", "link")
    msg("cust", "conn", 196, "PAGES · HASHED, WRITE-ONCE", dashed=True)
    msg("conn", "rules", 240, "OBSERVATIONS", dashed=True, mk="open")
    msg("rules", "look", 320, "FAIL · LOOKUPS TOPIC", dashed=True, mk="open")
    msg("look", "conn", 360, "CONFIG · CLOUDTRAIL · TF SEARCH", lx=768)
    msg("conn", "look", 396, "RESPONSES · LANDED RAW FIRST", dashed=True, lx=512)
    msg("look", "fix", 468, "INVESTIGATION + FIX ID")
    msg("fix", "cust", 548, "CHECKED CHANGE · NEVER APPLIED", "link", lx=1024)
    msg("cust", "fix", 580, "ACCEPT · A RECORD ONLY", dashed=True, lx=256)
    msg("conn", "cust", 676, "NEXT SYNC RE-READS", "link")
    msg("conn", "rules", 716, "NEW OBSERVATIONS", "accent")
    msg("rules", "fix", 788, "LATER VERDICT · NEWER EVIDENCE", dashed=True, mk="open",
        lx=1024)

    def chip(x, y, w, head, rest, accent=False):
        fill = P.accent_a(0.08) if accent else P.paper
        stroke = P.accent if accent else P.ink_a(0.35)
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="20" rx="4" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="1"/>')
        o.append(text(x + 12, y + 14, head, fill=P.accent if accent else P.ink, size=9,
                      weight=600))
        o.append(text(x + 12 + r4(mono_w(head, 9, 0) + 8), y + 14, rest, fill=P.muted,
                      size=9))

    chip(652, 256, 200, "FAIL", "names the open bucket")
    chip(920, 484, 224, "CHECKED", "params · syntax · scope")
    chip(140, 624, 224, "APPLIED", "by the customer, not CCP")
    chip(652, 728, 200, "PASS", "supersedes the FAIL", accent=True)
    chip(920, 800, 224, "CONFIRMED", "reopens on a FAIL ≤ 30 d")
    # actors
    o += [node(P, 32, 40, 192, 64, "external", "EXT", "Customer",
               ["AWS · Terraform · engineer"]),
          node(P, 288, 40, 192, 64, "backend", "SYNC", "Connector",
               ["AWS · GitHub · raw zone"]),
          node(P, 544, 40, 192, 64, "backend", "RULES", "Rules engine",
               ["versioned rule decides"]),
          node(P, 800, 40, 192, 64, "backend", "CODE", "Lookups + investigation",
               ["code picks every read"]),
          node(P, 1056, 40, 192, 64, "security", "FIXES", "Fix suggestions",
               ["catalogue · checked offline"])]
    o.append(legend(P, 848, W, [("line:link", "Crosses to the customer side"),
                                ("line:muted", "Internal call"),
                                ("line:muted,dashed", "Return or record"),
                                ("line:muted,dashed,open", "Async event"),
                                ("line:accent", "Headline result")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- 04 verdict pipeline


def diamond(P, cx, cy, hw, hh, lines) -> str:
    pts = f"{cx},{cy - hh} {cx + hw},{cy} {cx},{cy + hh} {cx - hw},{cy}"
    out = [f'<polygon points="{pts}" fill="{P.paper}"/>',
           f'<polygon points="{pts}" fill="{P.white}" stroke="{P.ink}" stroke-width="1"/>']
    y0 = cy + 4 - 8 * (len(lines) - 1)
    for i, s in enumerate(lines):
        out.append(text(cx, y0 + 16 * i, s, fill=P.ink, size=12, family=SANS, weight=600,
                        anchor="middle"))
    return "\n".join(out)


def d04_pipeline(P):
    W, H = 1280, 588
    o = [arrow(P, [(192, 216), (240, 216)]),
         arrow(P, [(392, 216), (440, 216)]),
         arrow(P, [(616, 216), (680, 216)]),
         arrow(P, [(528, 272), (528, 368)]),
         arrow(P, [(808, 216), (880, 216)]),
         arrow(P, [(744, 260), (744, 368)]),
         arrow(P, [(840, 384), (860, 384), (860, 236), (880, 236)]),
         arrow(P, [(680, 400), (592, 400)]),
         arrow(P, [(968, 248), (968, 368)], "accent"),
         arrow(P, [(516, 432), (516, 480), (968, 480), (968, 432)]),
         arrow(P, [(1056, 400), (1104, 400)])]
    o += [lab_above(P, 648, 216, "YES"),
          lab_right(P, 528, 320, "NO"),
          lab_above(P, 844, 216, "TIER 1"),
          lab_right(P, 744, 314, "TIER 2"),
          lab_above(P, 636, 400, "CHECK FAILS"),
          lab_right(P, 968, 308, "PASS · FAIL · PARTIAL", P.accent),
          lab_below(P, 740, 480, "NEEDS_REVIEW")]
    o.append(callout(P, 240, 72, ["Absence counts only from a complete read:",
                                  "a partial or denied scope is not “nothing there”."],
                     ((440, 100), (528, 104), (528, 156))))
    o.append(callout(P, 1240, 72, ["The verdict is keyed by its rule version and",
                                   "pinned inputs, so it can be rebuilt exactly."],
                     ((1000, 100), (968, 112), (968, 180)), anchor="end"))
    o += [node(P, 40, 184, 152, 64, "external", "START", "Evidence lands",
               ["raw hashed · facts mapped"], rx=20),
          node(P, 240, 184, 152, 64, "backend", "DATA", "Impact table",
               ["which (test, subject)"]),
          diamond(P, 528, 216, 88, 56, ["Read complete,", "inputs known?"]),
          diamond(P, 744, 216, 64, 44, ["Test tier?"]),
          node(P, 880, 184, 176, 64, "focal", "CODE", "Versioned rule",
               ["pure function of facts"]),
          node(P, 440, 368, 152, 64, "backend", "ABSTAIN", "Needs review",
               ["never a PASS on a gap"]),
          node(P, 680, 368, 160, 64, "backend", "MODEL", "Extract fields",
               ["Haiku 5.5 · cited spans"]),
          node(P, 880, 368, 176, 64, "store", "WRITE", "Verdict + state row",
               ["one transaction + outbox"]),
          node(P, 1104, 368, 136, 64, "external", "END", "Rollup",
               ["coverage · freshness"], rx=20)]
    o.append(legend(P, 528, W, [("external", "Start or end"), ("backend", "Step"),
                                ("focal", "Where status is set"), ("store", "Write"),
                                ("text", "Diamond = decision"),
                                ("line:muted", "Flow"),
                                ("line:accent", "Status leaves the rule")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- 05 approval lifecycle


def state(P, x, y, w, kind, tag, name, sub, ring=False) -> str:
    out = [node(P, x, y, w, 72, kind, tag, name, [sub], rx=8)]
    if ring:
        out.append(f'<circle cx="{x + w - 20}" cy="{y + 20}" r="7" fill="none" '
                   f'stroke="{P.ink}" stroke-width="1"/>'
                   f'<circle cx="{x + w - 20}" cy="{y + 20}" r="4" fill="{P.ink}"/>')
    return "\n".join(out)


def d05_lifecycle(P):
    W, H = 1280, 604
    o = [f'<rect x="24" y="72" width="1232" height="144" rx="8" fill="{P.muted_a(0.035)}" '
         f'stroke="{P.ink_a(0.12)}"/>',
         f'<rect x="24" y="400" width="1232" height="128" rx="8" fill="{P.ink_a(0.035)}" '
         f'stroke="{P.ink_a(0.12)}"/>',
         text(40, 92, "RECOVERY", fill=P.soft, size=8, ls="0.14em"),
         text(40, 252, "PRIMARY PHASES", fill=P.soft, size=8, ls="0.14em"),
         text(40, 420, "TERMINAL OUTCOMES", fill=P.soft, size=8, ls="0.14em")]
    o += [arrow(P, [(46, 308), (88, 308)]),
          arrow(P, [(248, 308), (320, 308)]),
          arrow(P, [(480, 308), (552, 308)]),
          arrow(P, [(712, 308), (784, 308)]),
          arrow(P, [(944, 308), (1048, 308)], "accent"),
          arrow(P, [(1160, 272), (1160, 184)], dashed=True),
          arrow(P, [(1096, 184), (1096, 272)], dashed=True),
          arrow(P, [(400, 344), (400, 440)]),
          arrow(P, [(632, 344), (632, 440)]),
          arrow(P, [(864, 344), (864, 440)])]
    o += [lab_above(P, 67, 308, "FAIL"),
          lab_above(P, 284, 308, "RENDER"),
          lab_above(P, 516, 308, "CHECKED"),
          lab_above(P, 748, 308, "ACCEPT"),
          lab_above(P, 996, 308, "LATER READ", P.accent),
          lab_left(P, 1096, 228, "READ: FIXED"),
          lab_right(P, 1160, 228, "FAIL IN 30 D"),
          lab_right(P, 400, 384, "A CHECK FAILS"),
          lab_right(P, 632, 384, "DISMISS + WHY"),
          lab_right(P, 864, 384, "NEW RENDERING")]
    o.append(f'<circle cx="40" cy="308" r="6" fill="{P.ink}"/>')
    o.append(callout(P, 744, 140, ["Accepting is optional and confirms",
                                   "nothing; only a later read does."],
                     ((748, 146), (864, 150), (864, 268)), anchor="end"))
    o.append(callout(P, 984, 472, ["Superseded only by a different",
                                   "rendering; a repeat is the same."],
                     ((980, 480), (964, 478), (948, 478))))
    o += [state(P, 88, 272, 160, "backend", "PHASE 1", "Chosen", "fix id, by code or model"),
          state(P, 320, 272, 160, "backend", "PHASE 2", "Rendered", "template, checked offline"),
          state(P, 552, 272, 160, "backend", "PHASE 3", "Open", "one per resource and fix"),
          state(P, 784, 272, 160, "backend", "PHASE 4", "Accepted", "customer applies it"),
          state(P, 1048, 272, 160, "focal", "PHASE 5", "Confirmed", "a later read sees it",
                ring=True),
          state(P, 1048, 112, 160, "optional", "RECOVERY", "Reopened", "FAIL within 30 days"),
          state(P, 320, 440, 160, "store", "TERMINAL", "Not offered", "fix_check_failed"),
          state(P, 552, 440, 160, "store", "TERMINAL", "Dismissed", "a person says why"),
          state(P, 784, 440, 160, "store", "TERMINAL", "Superseded", "a new rendering")]
    o.append(legend(P, 552, W, [("backend", "Phase"), ("focal", "Done: seen by a read"),
                                ("optional", "Recovery"), ("store", "Terminal"),
                                ("line:muted", "Transition"),
                                ("line:accent", "Only a read confirms"),
                                ("line:muted,dashed", "Recovery loop")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- 06 evidence chain


def entity(P, x, y, w, kind, tag, name, fields, note=None) -> str:
    h = 44 + 16 * len(fields) + 8
    _, _, _, tstroke, tfill = kinds(P)[kind]
    out = [box(P, x, y, w, h, kind),
           tag_chip(P, x + 8, y + 6, tag, tstroke, tfill),
           text(x + 12, y + 35, name, fill=P.ink, size=12, family=SANS, weight=600),
           plain(P, [(x, y + 44), (x + w, y + 44)], P.ink_a(0.12), 0.8)]
    if note:
        out.append(text(x + w - 12, y + 15, note, fill=P.soft, size=8, anchor="end"))
    for i, f in enumerate(fields):
        color = P.ink if f.startswith("#") else (P.soft if f.startswith("+") else P.muted)
        out.append(text(x + 12, y + 61 + 16 * i, f, fill=color, size=9))
    return "\n".join(out)


def d06_evidence_chain(P):
    W, H = 1280, 700
    o = [plain(P, [(272, 148), (368, 148)], P.muted),
         plain(P, [(592, 148), (688, 148)], P.muted),
         plain(P, [(912, 148), (1008, 148)], P.muted),
         plain(P, [(840, 220), (840, 280), (1120, 280), (1120, 392)], P.muted),
         plain(P, [(1008, 468), (912, 468)], P.muted),
         plain(P, [(688, 468), (592, 468)], P.muted),
         plain(P, [(368, 468), (272, 468)], P.muted),
         plain(P, [(160, 392), (160, 328), (760, 328), (760, 220)], P.accent, 1.4)]
    o += [lab_above(P, 320, 148, "MAPS TO"), lab_above(P, 640, 148, "CITED BY"),
          lab_above(P, 960, 148, "STATED AS"), lab_above(P, 980, 280, "LOOKED UP"),
          lab_above(P, 960, 468, "READ BY"), lab_above(P, 640, 468, "SUGGESTS"),
          lab_above(P, 320, 468, "STATUS IN"),
          lab_above(P, 460, 328, "CONFIRMED BY", P.accent)]

    def card(xe, ly, s, side):  # side: +1 to the right of an edge, -1 to the left
        w = r4(mono_w(s) + 8)
        return label(P, xe + side * (8 + w / 2), ly + 14, s)

    o += [card(272, 148, "1", 1), card(368, 148, "N", -1),
          card(592, 148, "N", 1), card(688, 148, "N", -1),
          card(912, 148, "1", 1), card(1008, 148, "1..*", -1),
          card(1008, 468, "0..*", -1), card(912, 468, "0..1", 1),
          card(688, 468, "0..1", -1), card(592, 468, "0..*", 1),
          card(368, 468, "1", -1), card(272, 468, "1..*", 1),
          lab_right(P, 840, 240, "1"), lab_right(P, 1120, 372, "0..*"),
          lab_left(P, 760, 240, "1"), lab_right(P, 160, 372, "0..1")]
    o.append(callout(P, 320, 592, ["Every row on this page joins a per-tenant hash chain;",
                                   "an hourly signed checkpoint lands in a separate account."],
                     ((316, 586), (176, 584), (160, 508))))
    o += [entity(P, 48, 72, 224, "store", "RAW", "raw_events",
                 ["# source_event_id", "tenant_id · connector", "payload_sha256",
                  "stored_sha256", "raw_path · version_id", "source_ts"]),
          entity(P, 368, 72, 224, "store", "FACTS", "observations",
                 ["# entity_id", "→ source_event_id", "observed_at", "recorded_seq",
                  "+ mapper_version", "+ sync_run_id"], note="evidence, assets, …"),
          entity(P, 688, 72, 224, "focal", "ROOT", "verdicts",
                 ["# verdict_id", "evaluation_key", "test_id · subject_key", "status",
                  "rule_version", "manifest (pinned inputs)"]),
          entity(P, 1008, 72, 224, "backend", "STATE", "compliance_state",
                 ["# row_id", "→ verdict_id", "valid_from · valid_to", "recorded_seq",
                  "supersedes_row_id", "reason"]),
          entity(P, 1008, 392, 224, "backend", "LOOKUP", "lookups",
                 ["# lookup_id", "→ verdict_id", "kind · resource", "params · status",
                  "→ response_ids (raw)"]),
          entity(P, 688, 392, 224, "backend", "RUN", "investigations",
                 ["# investigation_id", "→ verdict_id", "root_cause · citations",
                  "prompt_version", "→ thread_id (model runs)"]),
          entity(P, 368, 392, 224, "security", "SUGGESTION", "fix_suggestions",
                 ["# suggestion_id", "→ investigation_id", "fix · fix_version",
                  "params · body · checks", "→ lookup_ids", "source: code | model"]),
          entity(P, 48, 392, 224, "backend", "STATUS", "fix_suggestion_events",
                 ["# event_id", "→ suggestion_id", "status · actor · note",
                  "→ verdict_id (the read)"])]
    o.append(legend(P, 648, W, [("focal", "Aggregate root"), ("store", "Evidence"),
                                ("backend", "Record"), ("security", "A person decides"),
                                ("text", "# key   → reference   + planned column"),
                                ("plain:accent", "Closes the loop")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- 07 guardrails


LAYERS = [
    ("L1 · INGEST", "Connectors", "when evidence lands", False, [
        ("CODE", "Absence only from", "a complete scope"),
        ("CODE", "Denied or partial reads", "give needs_review"),
        ("CODE", "Secrets dropped", "before landing"),
        ("PLATFORM", "Raw written once,", "hashed, Object Lock")]),
    ("L2 · TOOLS", "Tool gateway", "when an agent reads", False, [
        ("CODE", "Tenant bound in code,", "never by the model"),
        ("CODE", "policy.check fails", "closed: permit, deny, ask"),
        ("CODE", "Code picks every live", "read, never the model"),
        ("PLATFORM", "Read-only database", "role for agents")]),
    ("L3 · DECISION", "Rules engine", "when a status is set", True, [
        ("CODE", "Only a versioned rule", "sets a status"),
        ("CODE", "Code checks every", "pointer citation"),
        ("CODE", "Second reader before", "an AI-derived PASS"),
        ("CODE", "Refusal or unknown input", "gives needs_review")]),
    ("L4 · DECIDE", "Approvals, fixes", "when a person decides", False, [
        ("CODE", "Payload and precondition", "hashes, with expiry"),
        ("PERSON", "Signed-in reviewer,", "never the requester"),
        ("CODE", "Fixes rendered by code,", "checked offline"),
        ("CODE", "A fix is done only", "when a read sees it")]),
    ("L5 · RECORD", "Storage and audit", "always", False, [
        ("PLATFORM", "Forced RLS and", "tenant-first keys"),
        ("PLATFORM", "Per-tenant hash chain,", "hourly signed checkpoint"),
        ("CODE", "Postgres run log is", "the record, not a vendor"),
        ("CODE", "Cross-tenant canary", "runs in production")]),
]


def d07_guardrails(P):
    W, H = 1280, 624
    x0, x1, top, rh = 136, 1240, 72, 96
    o = [text(40, 56, "ALONG ONE FINDING", fill=P.soft, size=8, ls="0.14em"),
         arrow(P, [(88, 80), (88, 536)])]
    for i, (idx, name, when, focal, controls) in enumerate(LAYERS):
        y = top + i * rh
        fill = P.accent_a(0.06) if focal else (P.paper if i % 2 == 0 else P.white)
        o.append(f'<rect x="{x0}" y="{y}" width="{x1 - x0}" height="{rh}" fill="{fill}"/>')
        o.append(text(152, y + 30, idx, fill=P.accent if focal else P.soft, size=8,
                      ls="0.14em"))
        o.append(text(152, y + 54, name, fill=P.ink, size=14, family=SANS, weight=600))
        o.append(text(152, y + 72, when, fill=P.muted, size=9))
        for j, (actor, l1, l2) in enumerate(controls):
            cx, cy = 408 + j * 208, y + 20
            o.append(f'<rect x="{cx}" y="{cy}" width="192" height="56" rx="6" '
                     f'fill="{P.white}" stroke="{P.accent_a(0.5) if focal else P.ink_a(0.18)}" '
                     f'stroke-width="1"/>')
            tstroke = P.ink_a(0.4) if actor == "CODE" else P.ink_a(0.22)
            o.append(tag_chip(P, cx + 8, cy + 6, actor, tstroke,
                              P.ink if actor == "CODE" else P.soft))
            o.append(text(cx + 12, cy + 33, l1, fill=P.ink, size=12, family=SANS,
                          weight=600))
            o.append(text(cx + 12, cy + 48, l2, fill=P.ink, size=12, family=SANS,
                          weight=600))
        if i:
            o.append(plain(P, [(x0, y), (x1, y)], P.ink_a(0.12), 1))
    o.append(f'<rect x="{x0}" y="{top}" width="{x1 - x0}" height="{rh * 5}" fill="none" '
             f'stroke="{P.muted}" stroke-width="1"/>')
    fy = top + 2 * rh
    o.append(f'<rect x="{x0}" y="{fy}" width="{x1 - x0}" height="{rh}" fill="none" '
             f'stroke="{P.accent}" stroke-width="1.2"/>')
    o.append(legend(P, 572, W, [("text", "CODE = enforced by CCP code, with a test"),
                                ("text", "PLATFORM = enforced by Postgres or AWS"),
                                ("text", "PERSON = a signed-in human decision"),
                                ("focal", "Where status is set")]))
    return W, H, "\n".join(o)


# --------------------------------------------------------------------------- pages

DIAGRAMS = [
    dict(slug="01-architecture", y0=72, fn=d01_architecture, kind="Architecture",
         title="Code decides, models propose, people approve",
         short="System architecture",
         sub="Evidence flows left to right through code that sets every status. Agents read "
             "through one gateway and can only propose; code renders each fix, the customer "
             "applies it, and only the next sync confirms it.",
         desc="Architecture of CCP: connectors, evidence store, rules engine and compliance "
              "state in a code-only zone; agents behind a read-only tool gateway that can "
              "only propose; a fix renderer and signed-in approvals. CCP writes nothing to "
              "the customer's systems; a person applies a suggested fix.",
         cols="1.1fr 1fr 0.9fr",
         cards=[
             ("CODE DECIDES", "accent", "Every status comes from a rule", [
                 "A versioned rule over pinned facts sets PASS, FAIL or PARTIAL. Tier-2 "
                 "models only extract typed fields, with pointer citations that code checks.",
                 "Absence counts only from a complete sync; a partial or denied read gives "
                 "needs_review.",
                 "Verdict, state row and announcement commit in one transaction."]),
             ("MODELS PROPOSE", "ink", "Agents read, then propose", [
                 "Agents run as bounded queue jobs (turns, tokens, cost, time), at most one "
                 "run per evidence state.",
                 "Every tool is typed, bound to the tenant in code and checked by a "
                 "fail-closed policy.check. Code, not the model, chooses every live read.",
                 "A run ends with a draft or a fix chosen by id from the catalogue, never "
                 "a write."]),
             ("PEOPLE DECIDE", "muted", "Suggest, never apply", [
                 "Code renders the Terraform change, commands and console steps from a "
                 "versioned template and checks them offline before anyone sees them.",
                 "A signed-in member approves drafts by payload hash, never the requester; "
                 "accepting a fix is a record only.",
                 "No pull request, executor or write credential: a fix is confirmed only "
                 "when a later read sees it."])]),
    dict(slug="02-deployment", y0=16, fn=d02_deployment, kind="Deployment",
         title="Where CCP runs on AWS",
         short="AWS deployment",
         sub="One cell per region from the same Terraform: one image as api and worker "
             "behind PgBouncer, RDS PostgreSQL with a read replica as the record, a "
             "write-once raw zone, and checkpoints in an account CCP cannot write to.",
         desc="Deployment of one CCP cell on AWS: ECS Fargate api, worker and PgBouncer "
              "services, RDS PostgreSQL 17 with a read replica and an S3 raw zone in the "
              "production account, a checkpoint bucket in a separate audit account, a "
              "read-only role in the customer's account, and external model and SaaS APIs. "
              "An EU cell is the same module in eu-central-1.",
         cols="1fr 0.9fr 1.1fr",
         cards=[
             ("INVENTORY", "ink", "Small on purpose", [
                 "Per cell: RDS PostgreSQL 17 (Multi-AZ) and a read replica, PgBouncer in "
                 "transaction mode, S3 in two accounts, KMS keys, one image run as api and "
                 "worker, a load balancer, Cognito, CloudWatch.",
                 "Outside services: Anthropic (US cell), Bedrock (EU cell), Voyage, "
                 "Langfuse Cloud, GitHub (read only)."]),
             ("NOT ADDED", "muted", "Deliberately left out", [
                 "Kafka, Redis, Temporal, Kubernetes, a vector database, self-hosted "
                 "observability, an MCP gateway, a code sandbox.",
                 "Any write role, executor or GitHub App in a customer's account: CCP "
                 "suggests fixes and applies none."]),
             ("SCALE", "accent", "Cells, a pooler, a replica", [
                 "The readiness report reads the replica only while its lag is under 30 "
                 "seconds; one large tenant can get a partition of its own.",
                 "A tenant lives in one cell and a process serves only its cell. eu-1 "
                 "calls models through Bedrock in its region; a dedicated cell is the same "
                 "module."])]),
    dict(slug="03-sequence-s3", y0=16, fn=d03_sequence, kind="Sequence",
         title="A public S3 bucket, from first read to a confirmed fix",
         short="End-to-end sequence",
         sub="The AWS worked example, suggest-only: a rule finds the bucket open, code looks "
             "up who changed it and where it lives in Terraform, renders a checked fix, the "
             "customer applies it, and the next read confirms it.",
         desc="Sequence of the S3 public-bucket example across Customer, Connector, Rules "
              "engine, Lookups and Fix suggestions: read, FAIL, code-chosen lookups landed "
              "raw first, a code-written investigation, a rendered and checked fix, the "
              "customer applying it, a re-read, a PASS that supersedes the FAIL and the "
              "suggestion confirmed.",
         cols="1.1fr 0.9fr 1fr",
         cards=[
             ("DETECT", "ink", "Code finds the FAIL", [
                 "Every page lands write-once with a hash and a completeness record before "
                 "it is mapped.",
                 "The failing rule names the bucket, so the rule itself explains the "
                 "failure: no model is needed and nothing parks."]),
             ("LOOK UP", "muted", "Code picks every read", [
                 "Config history in 7-day windows, the CloudTrail events that can change "
                 "the setting, and a search of the organization's Terraform; 10 resources "
                 "and 30 days at most.",
                 "Each response lands raw before its typed row. A model never chooses a "
                 "live read or its parameters."]),
             ("SUGGEST AND CONFIRM", "accent", "Applied by the customer", [
                 "Code fills the fix's template and checks parameters, commands, syntax and "
                 "scope; a fix that fails a check is never offered.",
                 "Accepting is a record. Only a verdict on evidence observed after the "
                 "suggestion confirms it; a FAIL within 30 days reopens it."])]),
    dict(slug="04-verdict-pipeline", y0=40, fn=d04_pipeline, kind="Flowchart",
         title="How evidence becomes a verdict",
         short="Verdict pipeline",
         sub="Every path ends in a verdict that code wrote. Gaps in the data abstain as "
             "needs_review; a model's only job is to fill typed fields that code checks "
             "before a rule reads them.",
         desc="Flowchart of CCP's evaluation path: evidence lands, the impact table picks "
              "affected tests, incomplete reads abstain as needs_review, tier-1 tests go "
              "straight to a versioned rule and tier-2 tests extract cited fields first, and "
              "the verdict and state row are written together before the rollup.",
         cols="1fr 1.1fr 0.9fr",
         cards=[
             ("TIER 1", "ink", "Rules over facts", [
                 "Pure functions over observations, keyed by rule version and pinned "
                 "inputs.",
                 "Tier 1 never waits on a model budget."]),
             ("TIER 2", "accent", "Extract, then rule", [
                 "Haiku 5.5 fills a typed schema with pointer citations (segment ids, JSON "
                 "Pointers). Code checks each one, then a rule decides.",
                 "A refusal or failed check gives needs_review. A second reader runs "
                 "before any AI-derived PASS."]),
             ("ROLLUP", "muted", "Over who should be there", [
                 "The rollup counts the expected population, not only rows that exist, "
                 "and marks stale or unassessed subjects.",
                 "No PASS over a partial or stale population."])]),
    dict(slug="05-fix-lifecycle", y0=48, fn=d05_lifecycle, kind="State machine",
         title="The life of a fix suggestion",
         short="Fix lifecycle",
         sub="CCP suggests fixes and applies none. Code renders and checks each one; a "
             "person may accept or dismiss it, but only a later read of the customer's "
             "systems confirms it, and a FAIL within 30 days reopens it.",
         desc="Lifecycle of a fix suggestion: Chosen, Rendered, Open, Accepted and Confirmed "
              "on the primary rail; Reopened as the recovery state; Not offered, Dismissed "
              "and Superseded as terminal outcomes.",
         cols="1.1fr 1fr 0.9fr",
         cards=[
             ("RENDERED", "ink", "Code writes what is shown", [
                 "The model, or code when the rule explains the failure, picks a fix by id "
                 "and its parameters. It never writes a command or a diff.",
                 "Code fills the versioned template and checks params, commands, syntax, "
                 "scope and placeholders. A failure is recorded, never offered."]),
             ("NO EXECUTION", "muted", "The customer applies it", [
                 "No pull request, write credential or executor. The suggestion is a "
                 "Terraform change, commands and console steps to copy.",
                 "Accept and dismiss are records; a dismissal says why."]),
             ("DONE MEANS READ", "accent", "Confirmed by evidence", [
                 "A verdict on evidence observed after the suggestion that no longer names "
                 "the resource confirms it.",
                 "ccp fixes metrics reports time to confirm and the 30-day reopen "
                 "rate."])]),
    dict(slug="06-evidence-chain", y0=40, fn=d06_evidence_chain, kind="ER / data model",
         title="The evidence chain behind every verdict",
         short="Evidence chain",
         sub="One chain from a raw provider page to the read that confirmed a fix: evidence, "
             "FAIL, lookups, investigation, suggestion, confirming verdict. Rows are appended "
             "and superseded, never edited.",
         desc="Entity model of CCP's audit chain: raw_events map to observations, which "
              "verdicts cite; verdicts are stated as compliance_state rows and looked up in "
              "lookups, which investigations read; investigations suggest fix_suggestions, "
              "whose fix_suggestion_events link to the later verdict that confirmed the "
              "fix.",
         cols="1.1fr 0.9fr 1fr",
         cards=[
             ("APPEND ONLY", "ink", "History is kept", [
                 "Verdicts and state rows are insert-only; a correction supersedes with a "
                 "reason and never rewrites.",
                 "As-of queries show what was true, and what CCP believed, at any time."]),
             ("REBUILDABLE", "accent", "Verdicts carry their inputs", [
                 "rule_version and the manifest of pinned inputs identify each verdict.",
                 "Model requests and responses are stored, so an AI-derived verdict is "
                 "rebuilt from stored outputs, never re-sampled."]),
             ("VERIFIABLE", "muted", "Without trusting the database", [
                 "A per-tenant hash chain covers raw objects and every row above.",
                 "Hourly signed checkpoints sit in a compliance-mode bucket in a separate "
                 "account; ccp audit verify checks them.",
                 "As-of audit packages carry raw pointers and chain proofs and verify "
                 "offline with a public key."])]),
    dict(slug="07-guardrails", y0=32, fn=d07_guardrails, kind="Layer stack",
         title="Guardrails, by where they are enforced",
         short="Guardrail catalogue",
         sub="Twenty controls on the five surfaces a finding crosses. Each one is enforced "
             "outside the model: by CCP code with a test, by Postgres or AWS, or by a "
             "signed-in person.",
         desc="Catalogue of CCP guardrails grouped by enforcement surface: ingest, tool "
              "gateway, decision, approval and record, with four controls each and the actor "
              "that enforces it.",
         cols="1fr 1fr 1fr",
         cards=[
             ("FAILS CLOSED", "accent", "Default deny", [
                 "An unknown tool, tenant, decision or model price is refused, not "
                 "guessed.",
                 "Kill switches per workflow, per tool and per tenant, plus one that parks "
                 "all model work."]),
             ("MEASURED", "ink", "Autonomy is earned", [
                 "Five runs per case, pass^k, false PASS and false FAIL reported apart.",
                 "A draft skips review only in a reversible, low-impact class whose "
                 "published snapshot meets the bar, within a daily budget and a 10% "
                 "sample, and is demoted automatically."]),
             ("PAGED", "muted", "What wakes someone", [
                 "Chain verification fails, a replay mismatch, the cross-tenant canary "
                 "fails, or AI work parks for 30 minutes."])]),
]


def css(P: Palette, W: int) -> str:
    return f"""
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{ --paper: {P.paper}; --ink: {P.ink}; --muted: {P.muted}; --soft: {P.soft};
      --accent: {P.accent}; --link: {P.link}; --card: {P.white}; --rule: {P.ink_a(0.12)}; }}
    body {{ font-family: {SANS}; background: var(--paper); color: var(--ink);
      padding: 2.5rem clamp(1rem, 3vw, 2.5rem) 3rem; }}
    .frame {{ max-width: 1320px; margin: 0 auto; }}
    .nav {{ display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap;
      font-family: {MONO}; font-size: 0.68rem; letter-spacing: 0.08em; margin-bottom: 2rem; }}
    .nav a {{ color: var(--muted); text-decoration: none; border-bottom: 1px solid var(--rule); }}
    .nav a:hover {{ color: var(--ink); }}
    .eyebrow {{ font-family: {MONO}; font-size: 0.66rem; font-weight: 500; letter-spacing: 0.18em;
      text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }}
    h1 {{ font-family: {SERIF}; font-size: clamp(1.6rem, 2.4vw + 0.8rem, 2.4rem); font-weight: 400;
      letter-spacing: -0.02em; line-height: 1.12; margin-bottom: 0.75rem; }}
    .subtitle {{ color: var(--muted); font-size: 0.95rem; line-height: 1.55; max-width: 76ch;
      margin-bottom: 2rem; }}
    .diagram-container {{ width: 100%; overflow-x: auto; }}
    .diagram-container svg {{ width: 100%; min-width: {W}px; display: block; }}
    .cards {{ display: grid; gap: 1rem; margin-top: 2rem; }}
    .card {{ background: var(--card); border: 1px solid var(--rule); border-radius: 6px;
      padding: 1.25rem; min-width: 0; }}
    .card .eyebrow {{ margin-bottom: 0.6rem; }}
    .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem; }}
    .card h3 {{ font-size: 0.98rem; font-weight: 600; }}
    .dot {{ width: 7px; height: 7px; border-radius: 50%; flex: none; }}
    .dot.accent {{ background: var(--accent); }} .dot.ink {{ background: var(--ink); }}
    .dot.muted {{ background: var(--muted); }}
    .card ul {{ padding-left: 1rem; display: grid; gap: 0.4rem; }}
    .card li {{ color: var(--muted); font-size: 0.86rem; line-height: 1.5; }}
    footer {{ margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--rule);
      font-family: {MONO}; font-size: 0.68rem; color: var(--muted); line-height: 1.6; }}
    @media (max-width: 880px) {{ .cards {{ grid-template-columns: 1fr !important; }} }}
    @media print {{
      .diagram-container {{ overflow-x: visible; }}
      .diagram-container svg {{ min-width: 0; }}
    }}
"""


def file_name(slug: str, P: Palette) -> str:
    return f"{slug}{'-dark' if P.name == 'dark' else ''}.html"


def svg_and_cards(d: dict, P: Palette) -> tuple[int, str, str]:
    """The diagram's SVG (marker ids prefixed, so several can share a page) and its cards."""
    W, H, body = d["fn"](P)
    y0 = d["y0"]
    sid = d["slug"] + ("-dark" if P.name == "dark" else "")
    cards = "\n".join(
        f'<div class="card"><p class="eyebrow">{escape(e)}</p>'
        f'<div class="card-header"><span class="dot {dot}"></span><h3>{escape(h)}</h3></div>'
        f'<ul>{"".join(f"<li>{escape(li)}</li>" for li in items)}</ul></div>'
        for e, dot, h, items in d["cards"])
    inner = (f'{defs(P)}\n<rect x="0" y="{y0}" width="{W}" height="{H - y0}" fill="{P.paper}"/>\n'
             f'{body}')
    inner = inner.replace('id="arrow', f'id="{sid}-arrow').replace("url(#arrow",
                                                                   f"url(#{sid}-arrow")
    svg = (f'<svg viewBox="0 {y0} {W} {H - y0}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-labelledby="{sid}-title {sid}-desc">\n'
           f'<title id="{sid}-title">{escape(d["title"])}</title>\n'
           f'<desc id="{sid}-desc">{escape(d["desc"])}</desc>\n{inner}\n</svg>')
    return W, svg, cards


def render(d: dict, i: int, P: Palette) -> str:
    W, svg, cards = svg_and_cards(d, P)
    other = DARK if P.name == "light" else LIGHT
    nxt = DIAGRAMS[(i + 1) % len(DIAGRAMS)]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(d["short"])} · CCP</title>
  <link href="{FONT_LINK}" rel="stylesheet">
  <style>{css(P, W)}</style>
</head>
<body>
  <div class="frame">
    <nav class="nav">
      <a href="{file_name("index", P)}">← CCP SYSTEM DESIGN</a>
      <span><a href="{file_name(d["slug"], other)}">{other.name.upper()} VERSION</a>
      &nbsp;·&nbsp; <a href="{file_name(nxt["slug"], P)}">NEXT:
      {escape(nxt["short"].upper())} →</a></span>
    </nav>
    <p class="eyebrow">{i + 1:02d} · {escape(d["kind"])} · CCP target architecture</p>
    <h1>{escape(d["title"])}</h1>
    <p class="subtitle">{escape(d["sub"])}</p>
    <div class="diagram-container">
{svg}
    </div>
    <div class="cards" style="grid-template-columns: {d["cols"]};">
{cards}
    </div>
    <footer>Source: docs/reports/ccp-target-architecture.md and spec.md. Generated by
    docs/diagrams/system-design/build.py; edit the script, not this file.</footer>
  </div>
</body>
</html>
"""


def render_index(P: Palette) -> str:
    other = DARK if P.name == "light" else LIGHT
    rows = "\n".join(
        f'<a class="row" href="{file_name(d["slug"], P)}">'
        f'<span class="n">{i + 1:02d}</span>'
        f'<span class="k">{escape(d["kind"])}</span>'
        f'<span class="t">{escape(d["title"])}</span>'
        f'<span class="d">{escape(d["sub"])}</span></a>'
        for i, d in enumerate(DIAGRAMS))
    extra = """
    .list { display: grid; border-top: 1px solid var(--rule); }
    .row { display: grid; grid-template-columns: 3rem 9rem 1fr; gap: 0.25rem 1rem;
      padding: 1.1rem 0; border-bottom: 1px solid var(--rule); text-decoration: none;
      color: inherit; }
    .row:hover .t { color: var(--accent); }
    .n, .k { font-family: 'Geist Mono', ui-monospace, monospace; font-size: 0.7rem;
      letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); padding-top: 0.3rem; }
    .t { font-family: 'Instrument Serif', 'Noto Serif', serif; font-size: 1.45rem;
      line-height: 1.2; }
    .d { grid-column: 3; color: var(--muted); font-size: 0.88rem; line-height: 1.5;
      max-width: 80ch; }
    @media (max-width: 700px) { .row { grid-template-columns: 2.5rem 1fr; }
      .k { grid-column: 2; padding-top: 0; } .t, .d { grid-column: 1 / -1; } }
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CCP system design</title>
  <link href="{FONT_LINK}" rel="stylesheet">
  <style>{css(P, 0)}{extra}</style>
</head>
<body>
  <div class="frame">
    <nav class="nav"><span class="eyebrow" style="margin:0">CCP · target architecture</span>
      <a href="{file_name("index", other)}">{other.name.upper()} VERSION</a></nav>
    <h1>CCP system design</h1>
    <p class="subtitle">Seven views of the platform we are building. Read them in order: the
    trust boundaries, where it runs, one worked example end to end, then the evaluation path,
    the approval lifecycle, the data that ties a verdict to its evidence, and the guardrails.
    The design itself lives in docs/reports/ccp-target-architecture.md and spec.md.</p>
    <p class="subtitle"><a href="{file_name("all", P)}" style="color: var(--accent)">Scroll
    through all seven on one page →</a></p>
    <div class="list">
{rows}
    </div>
    <footer>Generated by docs/diagrams/system-design/build.py.</footer>
  </div>
</body>
</html>
"""


def render_all(P: Palette) -> str:
    """Every diagram on one scrolling page, with a sticky jump bar."""
    other = DARK if P.name == "light" else LIGHT
    jumps = "\n".join(f'<a href="#{d["slug"]}">{i + 1:02d} {escape(d["short"])}</a>'
                      for i, d in enumerate(DIAGRAMS))
    sections = []
    for i, d in enumerate(DIAGRAMS):
        _W, svg, cards = svg_and_cards(d, P)
        sections.append(f"""<section id="{d["slug"]}">
    <p class="eyebrow">{i + 1:02d} · {escape(d["kind"])}</p>
    <h2>{escape(d["title"])}</h2>
    <p class="subtitle">{escape(d["sub"])}</p>
    <div class="diagram-container">
{svg}
    </div>
    <div class="cards" style="grid-template-columns: {d["cols"]};">
{cards}
    </div>
  </section>""")
    extra = f"""
    .jump {{ position: sticky; top: 0; z-index: 10; background: var(--paper);
      border-bottom: 1px solid var(--rule); padding: 0.75rem 0; margin-bottom: 2rem;
      display: flex; flex-wrap: wrap; gap: 0.4rem 1.25rem; font-family: {MONO};
      font-size: 0.68rem; letter-spacing: 0.06em; text-transform: uppercase; }}
    .jump a {{ color: var(--muted); text-decoration: none; }}
    .jump a:hover {{ color: var(--accent); }}
    .jump .end {{ margin-left: auto; }}
    section {{ padding: 3rem 0 3.5rem; border-bottom: 1px solid var(--rule);
      scroll-margin-top: 3.5rem; }}
    section:first-of-type {{ padding-top: 1rem; }}
    h2 {{ font-family: {SERIF}; font-size: clamp(1.5rem, 2vw + 0.8rem, 2.1rem); font-weight: 400;
      letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 0.6rem; }}
    """
    body = "\n  ".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CCP system design, all views</title>
  <link href="{FONT_LINK}" rel="stylesheet">
  <style>{css(P, 1280)}{extra}</style>
</head>
<body>
  <div class="frame">
    <p class="eyebrow">CCP · target architecture</p>
    <h1>CCP system design</h1>
    <p class="subtitle">Seven views of the platform we are building, in reading order.</p>
    <nav class="jump">
{jumps}
      <a class="end" href="{file_name("all", other)}">{other.name} version</a>
    </nav>
  {body}
    <footer>Source: docs/reports/ccp-target-architecture.md and spec.md. Generated by
    docs/diagrams/system-design/build.py; edit the script, not this file.</footer>
  </div>
</body>
</html>
"""


def main() -> None:
    for P in (LIGHT, DARK):
        for i, d in enumerate(DIAGRAMS):
            (OUT / file_name(d["slug"], P)).write_text(render(d, i, P), encoding="utf-8")
        (OUT / file_name("index", P)).write_text(render_index(P), encoding="utf-8")
        (OUT / file_name("all", P)).write_text(render_all(P), encoding="utf-8")
    print(f"wrote {2 * (len(DIAGRAMS) + 2)} files to {OUT}")


if __name__ == "__main__":
    main()
