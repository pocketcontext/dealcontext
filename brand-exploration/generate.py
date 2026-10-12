"""Build the isolated, public-safe brand review directory."""
import json, shutil, re
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'dist'
OUT.mkdir(exist_ok=True)
font = TTFont(ROOT / 'fonts/ibm-plex-sans-700-latin.woff2')
glyphs = font.getGlyphSet()
cmap = font.getBestCmap()
units = font['head'].unitsPerEm

def word(text, x, y, size):
    parts = []
    scale = size / units
    for char in text:
        name = cmap[ord(char)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x, y)))
        parts.append(f'<path d="{pen.getCommands()}"/>')
        x += glyphs[name].width * scale
    return '<g fill="#2e493e">' + ''.join(parts) + '</g>'

def svg(w, h, content):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" color="#2e493e">{content}</svg>'

def mark(body, x, y, size):
    return f'<g transform="translate({x} {y}) scale({size/128})">{body}</g>'

concepts = sum([json.loads((ROOT / f'concepts-{c}.json').read_text()) for c in 'abc'], [])
assert sorted(c['id'] for c in concepts) == list(range(1,101))
assert len(set(c['body'] for c in concepts)) == 100
for c in concepts:
    base = OUT / 'assets' / f"{c['id']:03}"
    base.mkdir(parents=True, exist_ok=True)
    body = c['body']
    # An outline adds optical weight only on the small-size rendition.
    small_body = re.sub(r'stroke-width="([0-9.]+)"', lambda m: f'stroke-width="{float(m[1])*1.2:g}"', body)
    small = f'<g stroke="currentColor" stroke-width="1.5" stroke-linejoin="round">{small_body}</g>'
    files = {
      'logo': svg(512,512,mark(body,0,0,512)),
      'favicon': svg(64,64,mark(small,0,0,64)),
      'wordmark': svg(640,128,mark(body,0,12,104)+word('DealContext',128,82,64)),
      'linkedin-logo': svg(400,400,'<path fill="white" d="M0 0h400v400H0z"/>'+mark(body,48,48,304)),
      'luma-logo': svg(1024,1024,'<path fill="white" d="M0 0h1024v1024H0z"/>'+mark(body,164,164,696)),
    }
    for name,w,h in [('social-card',1200,630),('linkedin-banner',1512,256),('luma-banner',1680,480),('luma-social',1200,628)]:
        banner = 'banner' in name
        size = 100 if h < 300 else 150
        x = w*.37 if banner else 80
        y = (h-size)/2 if banner else 105
        content = f'<path fill="#f5f3ed" d="M0 0h{w}v{h}H0z"/>'
        content += mark(body,x,y,size)
        content += word('DealContext',x+size+30,y+size*.62,58 if banner else 70)
        content += word('Relationships, moving forward.', x+size+30 if banner else x, y+size+44 if banner else 410, 23 if banner else 34)
        files[name] = svg(w,h,content)
    for name,content in files.items():
        (base / f'{name}.svg').write_text(content)
(OUT / 'concepts.json').write_text(json.dumps(concepts))
for name in ['index.html','gallery.css','gallery.js']:
    shutil.copyfile(ROOT / name,OUT / name)
shutil.copytree(ROOT / 'fonts',OUT / 'fonts',dirs_exist_ok=True)
print('Generated 100 concepts and 900 SVG assets; wordmarks use outlined IBM Plex Sans.')
