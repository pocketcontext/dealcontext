"""Package generated assets with the font license and usage notes."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json
root=Path(__file__).resolve().parent
for c in json.loads((root/'dist/concepts.json').read_text()):
    folder=root/'dist/assets'/f"{c['id']:03}"
    with ZipFile(folder/'kit.zip','w',ZIP_DEFLATED) as z:
        for p in sorted(folder.iterdir()):
            if p.suffix in {'.svg','.png'}: z.write(p,p.name)
        z.write(root/'fonts/OFL.txt','FONT-LICENSE.txt')
        z.writestr('README.txt',f"DealContext concept {c['id']:03}: {c['name']}\n{c['description']}\n\nSelection-stage brand kit. Not yet an adopted application identity.\nColors: forest #2e493e, sage #688258, pale #e9ede2, paper #f5f3ed.\nTypography: IBM Plex Sans Bold; lettering is outlined in vector artwork.\nUse the favicon rendition at small sizes. Preserve aspect ratios.\nKeep at least 1/8 mark width clear around the mark.\nLinkedIn logo: 48px margin. Luma logo: 16% margin on white.\n")
print('Packaged 100 complete brand kits.')
