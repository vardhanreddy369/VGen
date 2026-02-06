#!/usr/bin/env python3
"""
Utility to create DreamVideo image list files (train/infer) in the format expected by ImageCustomDataset:
    relative_path|||prompt

Assumed folder layout (flexible):
root/
  id_1/
    cam_00/xxx.png
    cam_01/yyy.png
  id_2/
    ...

For single-view lists, one image per identity is chosen.
For multiview lists, multiple images per identity (spread across subfolders) are included.

Example:
  python tools/build_dreamvideo_lists.py \
    --root data/images/custom/human1_multiview \
    --mode multiview \
    --max-per-id 20 \
    --prompt "a * person" \
    --output-train data/custom/train/img_human1_multiview.txt \
    --output-infer data/custom/infer/subject_human1.txt
"""

import argparse
import os
from collections import defaultdict
from pathlib import Path

VALID_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def collect_images(root: Path):
    """Return dict[id] -> sorted list of image Paths."""
    id_to_imgs = defaultdict(list)
    for img_path in root.rglob("*"):
        if img_path.is_file() and img_path.suffix.lower() in VALID_EXTS:
            # identity is first directory under root
            rel = img_path.relative_to(root)
            parts = rel.parts
            if len(parts) < 2:
                # skip files placed directly under root (needs at least id/subfolder/img)
                continue
            identity = parts[0]
            id_to_imgs[identity].append(img_path)
    for ident in id_to_imgs:
        id_to_imgs[ident].sort()
    return id_to_imgs


def pick_single_view(id_to_imgs):
    """Pick the first image for each id."""
    picks = []
    for ident, imgs in id_to_imgs.items():
        if imgs:
            picks.append(imgs[0])
    return picks


def pick_multiview(id_to_imgs, root, max_per_id):
    """Pick up to max_per_id images per id, spreading across cam subfolders when present."""
    picks = []
    for ident, imgs in id_to_imgs.items():
        if not imgs:
            continue
        cam_to_imgs = defaultdict(list)
        for p in imgs:
            rel_parts = p.relative_to(root).parts
            # expect root/ident/cam/file -> cam is index 1
            cam = rel_parts[1] if len(rel_parts) > 1 else "default"
            cam_to_imgs[cam].append(p)

        cams = sorted(cam_to_imgs.keys())
        taken = 0
        round_idx = 0
        while taken < max_per_id:
            advanced = False
            for cam in cams:
                imgs_cam = cam_to_imgs[cam]
                if round_idx < len(imgs_cam) and taken < max_per_id:
                    picks.append(imgs_cam[round_idx])
                    taken += 1
                    advanced = True
            if not advanced:
                break
            round_idx += 1
    return picks


def write_list(paths, root, prompt, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for p in paths:
        rel = p.relative_to(root)
        lines.append(f"{rel.as_posix()}|||{prompt}")
    out_path.write_text("\n".join(lines))
    print(f"Wrote {len(lines)} lines to {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Root image directory")
    parser.add_argument("--mode", choices=["single", "multiview"], default="single")
    parser.add_argument("--max-per-id", type=int, default=20, help="Max images per identity for multiview")
    parser.add_argument("--prompt", default="a * person", help="Text prompt (use * for placeholder)")
    parser.add_argument("--output-train", required=True, help="Output train list path")
    parser.add_argument("--output-infer", help="Optional output infer list path (same paths/prompt)")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    id_to_imgs = collect_images(root)
    if not id_to_imgs:
        raise SystemExit(f"No images found under {root}")

    if args.mode == "single":
        picks = pick_single_view(id_to_imgs)
    else:
        picks = pick_multiview(id_to_imgs, root, args.max_per_id)

    write_list(picks, root, args.prompt, Path(args.output_train))

    if args.output_infer:
        write_list(picks, root, args.prompt, Path(args.output_infer))


if __name__ == "__main__":
    main()
