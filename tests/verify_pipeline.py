"""Exercise the independent CLI with real Blender and synthetic PNGs."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import sys
import uuid

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from glb_tools.cli import main as convert


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    args = parser.parse_args()
    run = ROOT / 'output/verification' / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + uuid.uuid4().hex[:8])
    source, output = run / 'input', run / 'result'
    source.mkdir(parents=True)
    (source / 'nested').mkdir()
    for name in ('multicolor', 'nested/text', 'hole', 'empty'):
        image = Image.new('RGBA', (128, 128))
        draw = ImageDraw.Draw(image)
        if name == 'multicolor':
            draw.rectangle((20, 20, 70, 110), fill='#ee4561')
            draw.rectangle((71, 20, 110, 110), fill='#3484df')
        elif name == 'nested/text':
            draw.text((14, 35), 'GLB', fill='#292e47', font_size=45)
        elif name == 'hole':
            draw.ellipse((12, 12, 116, 116), fill='#24aa89')
            draw.ellipse((42, 42, 86, 86), fill=(0, 0, 0, 0))
        image.save(source / (name + '.png'))
    (source / 'broken.png').write_bytes(b'not a PNG')
    result = convert(['--input', str(source), '--output', str(output),
                      '--blender', args.blender])
    assert result == 1, result
    for name in ('multicolor', 'nested/text', 'hole'):
        target = output / (name + '.glb')
        thumbnail_path = source / (name + '_thumb.png')
        assert target.is_file()
        assert thumbnail_path.is_file()
        assert not list(output.rglob('report.json'))
        work = next((output / '.work' / name).iterdir())
        compression = json.loads((work / 'compression.json').read_text(encoding='utf-8'))
        validation = json.loads((work / 'validation.json').read_text(encoding='utf-8'))
        assert compression['lossless']
        assert all(c['exact'] for c in compression['buffer_checks'])
        assert abs(validation['dimensions_cm']['width'] - 8) < .001
        with Image.open(thumbnail_path) as thumbnail:
            assert thumbnail.mode == 'RGBA' and thumbnail.size == (96, 96)
            assert thumbnail.getchannel('A').getextrema()[0] < 255
    assert not (output / 'empty.glb').exists()
    assert not (source / 'empty_thumb.png').exists()
    assert not (output / 'broken.glb').exists()
    assert not (source / 'broken_thumb.png').exists()
    assert len(list((output / '.work' / 'broken').rglob('error.txt'))) == 1
    originals = list((output / '.work').rglob('original.glb'))
    assert len(originals) == 3, originals
    rerun_output = StringIO()
    with redirect_stdout(rerun_output):
        rerun_result = convert(['--input', str(source), '--output', str(output),
                                '--blender', args.blender])
    assert rerun_result == 1
    assert 'Summary: success=0, skipped=4, failed=1' in rerun_output.getvalue()
    print('PASS: three GLBs, three transparent thumbnails, one skipped and one failed PNG: ' + str(run))


if __name__ == '__main__':
    main()
