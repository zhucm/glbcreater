"""Local batch PNG -> validated Meshopt GLB and transparent thumbnail."""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
import uuid

from PIL import Image

from .conversion import model


def discover(source):
    images = sorted(
        (path for path in source.rglob('*')
         if path.is_file() and path.suffix.lower() == '.png'
         and not path.stem.lower().endswith('_thumb')),
        key=lambda path: (path.relative_to(source).as_posix().casefold(),
                          path.relative_to(source).as_posix()))
    destinations = {}
    for path in images:
        relative = path.relative_to(source).with_suffix('')
        key = relative.as_posix().casefold()
        if key in destinations:
            raise ValueError(f'Output name collision: {destinations[key]} and {path}')
        destinations[key] = path
    return images


def convert(image, work, target, thumbnail_target, args, blender, node):
    def stage(name):
        print(f'  {name}', flush=True)

    with image.open('rb') as source:
        if source.read(8) != b'\x89PNG\r\n\x1a\n':
            raise ValueError('PNG signature mismatch')
    stage('analyzing')
    analysis = model.color_tools.analyze_image(
        image, args.color, recover_cartoon_colors=args.recover_cartoon_colors)
    stage('modeling and rendering thumbnail')
    analysis, validation = model.run_model_with_recovery(
        work, analysis, image_path=image, color=args.color,
        name='original', title='PNG model', width_cm=args.width_cm,
        thickness_cm=args.thickness_cm, blender=blender)

    stage('compressing and verifying')
    with (work / 'compression.log').open('w', encoding='utf-8') as log:
        subprocess.run(
            [node, str(Path(__file__).with_name('compression') / 'compress.mjs'),
             str(work / 'original.glb'), str(work / 'compressed.glb'),
             str(work / 'compression.json')],
            check=True, stdout=log, stderr=subprocess.STDOUT, timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    thumbnail = work / 'preview-96.png'
    with Image.open(thumbnail) as preview:
        if (preview.mode != 'RGBA' or preview.size != (96, 96)
                or preview.getchannel('A').getextrema()[0] == 255):
            raise ValueError('Blender thumbnail must be 96x96 RGBA with transparency')
    warning = analysis['analysis'].get('color_warning') or validation.get('color_warning')
    target.parent.mkdir(parents=True, exist_ok=True)
    for destination_path, source_path in ((target, work / 'compressed.glb'),
                                          (thumbnail_target, thumbnail)):
        if args.overwrite or not destination_path.is_file():
            mode = 'wb' if args.overwrite else 'xb'
            with destination_path.open(mode) as destination, source_path.open('rb') as source:
                shutil.copyfileobj(source, destination)
    return 'review' if warning else 'success'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='PNG directory to scan recursively')
    parser.add_argument('--output', type=Path, required=True, help='separate result directory')
    parser.add_argument('--blender', help='Blender executable; defaults to blender on PATH')
    parser.add_argument('--node', help='Node.js executable; defaults to node on PATH')
    parser.add_argument('--width-cm', type=float, default=8.0)
    parser.add_argument('--thickness-cm', type=float, default=0.5)
    parser.add_argument('--color', help='optional forced color, e.g. #ee4561 (quote in your shell)')
    parser.add_argument('--recover-cartoon-colors', action='store_true')
    parser.add_argument('--overwrite', action='store_true', help='replace existing GLBs and thumbnails')
    args = parser.parse_args(argv)
    source, output = args.input.resolve(), args.output.resolve()
    if not source.is_dir():
        parser.error('Input must be an existing directory')
    if output == source or output.is_relative_to(source):
        parser.error('Output must be outside the input directory')
    if (not math.isfinite(args.width_cm) or not .01 <= args.width_cm <= 100
            or not math.isfinite(args.thickness_cm) or not .01 <= args.thickness_cm <= 10):
        parser.error('Dimensions are out of range')
    if args.color and not re.fullmatch(r'#[a-fA-F0-9]{6}', args.color):
        parser.error('Color must be #RRGGBB')
    blender = args.blender or shutil.which('blender')
    node = args.node or shutil.which('node')
    if not blender or not Path(blender).is_file():
        parser.error('Blender executable not found; pass --blender')
    if not node or not Path(node).is_file():
        parser.error('Node.js executable not found; pass --node')
    try:
        images = discover(source)
    except ValueError as error:
        parser.error(str(error))
    if not images:
        print('No PNG source files found.')
        return 0
    output.mkdir(parents=True, exist_ok=True)
    counts = {'success': 0, 'skipped': 0, 'failed': 0}
    for index, image in enumerate(images, 1):
        relative = image.relative_to(source)
        target = output / relative.with_suffix('.glb')
        thumbnail_target = image.with_name(image.stem + '_thumb.png')
        print(f'[{index}/{len(images)}] {relative.as_posix()}', flush=True)
        if not args.overwrite and target.is_file() and thumbnail_target.is_file():
            print('  skipped: completed output already exists (use --overwrite to replace)', flush=True)
            counts['skipped'] += 1
            continue
        work = (output / '.work' / relative.with_suffix('') /
                (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + uuid.uuid4().hex[:8]))
        try:
            work.mkdir(parents=True, exist_ok=False)
            status = convert(image, work, target, thumbnail_target, args,
                             Path(blender).resolve(), node)
        except (model.color_tools.SkippedInput, model.GenerationSkipped) as error:
            counts['skipped'] += 1
            print(f'  skipped: {error}', flush=True)
            (work / 'outcome.json').write_text(
                json.dumps({'status': 'skipped', 'reason': error.reason,
                            'message': str(error)}, ensure_ascii=False, indent=2),
                encoding='utf-8')
        except Exception as error:
            counts['failed'] += 1
            print(f'  failed: {error}; see {work}', flush=True)
            if work.is_dir():
                (work / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        else:
            counts['success'] += 1
            print(f'  {status}: {target}; thumbnail: {thumbnail_target}', flush=True)
    print('Summary: ' + ', '.join(f'{key}={value}' for key, value in counts.items()), flush=True)
    return 1 if counts['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
