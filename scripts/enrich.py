"""Add palette, CLIP-based `similar` neighbours and a near-duplicate report.

For every catalog entry this writes (hand-edited fields are never touched):
  palette  - up to 5 dominant colours as hex, biggest first
  similar  - ids of the 8 most similar wallpapers (CLIP cosine similarity)

Also writes catalog/embeddings.npz (cache, float16) and
catalog/near-duplicates.yaml (a report only; nothing is ever deleted).

CLIP centre-crops to a square, which would drop the sides of a 16:9 wallpaper,
so non-square images are embedded as the mean of three square crops
(start / middle / end along the long edge).

Usage: uv run --group enrich scripts/enrich.py [--dup-threshold 0.95] [--top-k 8]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import torch
import yaml
import open_clip
from PIL import Image

import catalog_io

ROOT = catalog_io.ROOT
EMBEDDINGS = ROOT / "catalog" / "embeddings.npz"
DUPLICATES = ROOT / "catalog" / "near-duplicates.yaml"

MODEL = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
MODEL_TAG = f"{MODEL}/{PRETRAINED}"
WORK_EDGE = 768  # long edge of the working copy used for crops and palette


def load_work_image(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((WORK_EDGE, WORK_EDGE), Image.Resampling.LANCZOS)
        return im


def palette_of(
    im: Image.Image, n: int = 5, min_share: float = 0.03, merge_dist: float = 36.0
) -> list[str]:
    """Dominant colours, biggest first. Quantize to 10 clusters, then merge clusters
    that are visually near-identical (e.g. #000000 / #010101 / #030303) into the
    biggest one, so the palette is not filled with five shades of black."""
    small = im.copy()
    small.thumbnail((128, 128), Image.Resampling.BOX)
    q = small.quantize(colors=10, method=Image.Quantize.MEDIANCUT, kmeans=2)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    total = sum(c for c, _ in counts) or 1

    groups: list[list] = []  # [representative rgb, pixel count]
    for count, idx in counts:
        rgb = np.array(pal[idx * 3 : idx * 3 + 3], dtype=np.float32)
        for g in groups:
            if np.linalg.norm(g[0] - rgb) < merge_dist:
                g[1] += count
                break
        else:
            groups.append([rgb, count])

    groups.sort(key=lambda g: -g[1])
    out = []
    for rgb, count in groups:
        if count / total < min_share and out:
            continue
        r, g, b = (int(v) for v in rgb)
        out.append(f"#{r:02x}{g:02x}{b:02x}")
        if len(out) == n:
            break
    return out


def square_crops(im: Image.Image) -> list[Image.Image]:
    w, h = im.size
    s = min(w, h)
    if max(w, h) / s < 1.15:  # close enough to square: one centred crop
        left, top = (w - s) // 2, (h - s) // 2
        return [im.crop((left, top, left + s, top + s))]
    if w > h:
        offsets = [0, (w - s) // 2, w - s]
        return [im.crop((x, 0, x + s, s)) for x in offsets]
    offsets = [0, (h - s) // 2, h - s]
    return [im.crop((0, y, s, y + s)) for y in offsets]


def load_cache() -> dict[str, np.ndarray]:
    if not EMBEDDINGS.exists():
        return {}
    data = np.load(EMBEDDINGS, allow_pickle=False)
    if str(data["model"]) != MODEL_TAG:
        print(f"embedding cache is for {data['model']}, recomputing for {MODEL_TAG}")
        return {}
    return {str(i): v.astype(np.float32) for i, v in zip(data["ids"], data["vectors"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--dup-threshold", type=float, default=0.95)
    ap.add_argument("--redo-palette", action="store_true", help="recompute every palette (embeddings stay cached)")
    args = ap.parse_args()

    entries = catalog_io.load()
    if not entries:
        print("catalog is empty; run scripts/build_catalog.py first", file=sys.stderr)
        return 1

    if args.redo_palette:
        for e in entries:
            e.pop("palette", None)

    cache = load_cache()
    todo = [e for e in entries if e["id"] not in cache or "palette" not in e]
    print(f"{len(entries)} entries, {len(todo)} need work", flush=True)

    if todo:
        torch.set_num_threads(4)
        model = preprocess = None
        if any(e["id"] not in cache for e in todo):  # only load CLIP when something needs it
            model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
            model.eval()

        for n, e in enumerate(todo, 1):
            im = load_work_image(ROOT / e["path"])
            e["palette"] = palette_of(im)
            if e["id"] not in cache:
                batch = torch.stack([preprocess(c) for c in square_crops(im)])
                with torch.no_grad():
                    f = model.encode_image(batch)
                    f = f / f.norm(dim=-1, keepdim=True)
                    v = f.mean(dim=0)
                    v = v / v.norm()
                cache[e["id"]] = v.numpy().astype(np.float32)
            if n % 25 == 0 or n == len(todo):
                print(f"  {n}/{len(todo)}", flush=True)

    ids = [e["id"] for e in entries]
    vecs = np.stack([cache[i] for i in ids])
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)  # float16 round-trip safety
    sims = vecs @ vecs.T
    np.fill_diagonal(sims, -1.0)

    for i, e in enumerate(entries):
        top = np.argsort(-sims[i])[: args.top_k]
        e["similar"] = [ids[j] for j in top]

    pairs = []
    ii, jj = np.where(np.triu(sims, k=1) >= args.dup_threshold)
    for i, j in sorted(zip(ii, jj), key=lambda p: -sims[p[0], p[1]]):
        a, b = entries[i], entries[j]
        pairs.append({
            "score": round(float(sims[i, j]), 4),
            "a": {"id": a["id"], "path": a["path"], "size": a["size"], "bytes": a["bytes"]},
            "b": {"id": b["id"], "path": b["path"], "size": b["size"], "bytes": b["bytes"]},
        })

    catalog_io.save(entries)
    np.savez_compressed(
        EMBEDDINGS,
        ids=np.array(ids),
        vectors=vecs.astype(np.float16),
        model=np.array(MODEL_TAG),
    )
    DUPLICATES.write_text(
        "# Report only. Review by hand; nothing here is removed automatically.\n"
        + catalog_io.dump(pairs),
        encoding="utf-8",
    )

    best = np.sort(sims.max(axis=1))[::-1]
    print(f"similar written for {len(entries)} entries (top {args.top_k})")
    print(f"nearest-neighbour score: max {best[0]:.3f}, median {np.median(best):.3f}, min {best[-1]:.3f}")
    print(f"near-duplicate pairs >= {args.dup_threshold}: {len(pairs)} -> {DUPLICATES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
