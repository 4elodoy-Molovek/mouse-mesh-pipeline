# -*- coding: utf-8 -*-
"""
render_surfaces.py — publication-style previews of a set of nested envelope
surfaces (the ones build_envelopes.py writes).

Two images are produced next to the surfaces:
  preview_6views.png   six orthographic views (top/bottom/left/right/front/back),
                       graded translucency so every shell shows through;
  preview_cutaway.png  capped coronal + sagittal hemisections, each shell a solid
                       colour band (inner caps nudged toward the camera so the
                       coplanar cut faces do not z-fight).

Tissue colours come from the tissue NAME (skin/skull/dura/csf/grey/white are
recognised; anything else cycles a distinct palette); nesting depth (from the
config's envelope_parents) drives the translucency grading and the cut offset.

Usage:
    python render_surfaces.py --config pipeline_config_brain.json
    python render_surfaces.py --dir <surfaces_envelopes> [--info INFO.txt] \
                              [--parents '{"7":4,...}']
Runs in the main env (needs vtk + matplotlib).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BG = (0.09, 0.09, 0.12)

# name -> rgb for the tissues we know; keeps the familiar brain look
_NAMED = {
    "skin": (0.74, 0.53, 0.41),
    "scalp": (0.74, 0.53, 0.41),
    "skull": (0.90, 0.86, 0.74),
    "bone": (0.90, 0.86, 0.74),
    "dura": (0.15, 0.78, 0.72),
    "csf": (0.24, 0.62, 0.96),
    "gray": (0.96, 0.55, 0.18),
    "grey": (0.96, 0.55, 0.18),
    "white": (0.82, 0.80, 0.98),
    "brain": (0.96, 0.55, 0.18),
}
# fallback palette for unrecognised tissue names
_PALETTE = [
    (0.90, 0.36, 0.36),
    (0.40, 0.76, 0.42),
    (0.38, 0.55, 0.95),
    (0.95, 0.72, 0.22),
    (0.68, 0.45, 0.86),
    (0.30, 0.80, 0.80),
    (0.95, 0.55, 0.75),
    (0.60, 0.60, 0.62),
]


def _color_for(name: str, fallback_idx: int):
    key = name.strip().lower()
    for k, c in _NAMED.items():
        if k in key:
            return c
    return _PALETTE[fallback_idx % len(_PALETTE)]


def _load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _names_from_info(info_txt: str) -> dict:
    if info_txt and os.path.exists(info_txt):
        try:
            from parse_info_txt import parse_info_txt

            return {int(k): v for k, v in parse_info_txt(info_txt).label_names.items()}
        except Exception:
            pass
    return {}


def _depth(label: int, parents: dict) -> int:
    d, p = 0, parents.get(label, 0)
    while p:
        d += 1
        p = parents.get(p, 0)
    return d


def _surface(path: str):
    r = vtk.vtkUnstructuredGridReader()
    r.SetFileName(path)
    r.Update()
    gf = vtk.vtkGeometryFilter()
    gf.SetInputConnection(r.GetOutputPort())
    gf.Update()
    nr = vtk.vtkPolyDataNormals()
    nr.SetInputConnection(gf.GetOutputPort())
    nr.ConsistencyOn()
    nr.AutoOrientNormalsOn()
    nr.Update()
    return nr.GetOutput()


def _capture(rw):
    rw.Render()
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rw)
    w2i.SetInputBufferTypeToRGB()
    w2i.ReadFrontBufferOff()
    w2i.Update()
    im = w2i.GetOutput()
    w, h, _ = im.GetDimensions()
    return vtk_to_numpy(im.GetPointData().GetScalars()).reshape(h, w, 3)[::-1]


def collect(surf_dir: str, names: dict, parents: dict):
    """Return [(label, polydata, name, rgb, depth)] sorted outer->inner."""
    items = []
    for path in sorted(glob.glob(os.path.join(surf_dir, "surface_*.vtk"))):
        m = re.search(r"surface_(\d+)_", os.path.basename(path))
        if not m:
            continue
        label = int(m.group(1))
        name = names.get(label, f"label_{label}")
        items.append([label, _surface(path), name, None, _depth(label, parents)])
    items.sort(key=lambda it: (it[4], it[0]))  # by depth, then label (outer first)
    for i, it in enumerate(items):
        it[3] = _color_for(it[2], i)
    return items


def _bbox_center(items):
    b = [1e18, -1e18, 1e18, -1e18, 1e18, -1e18]
    for _, p, *_ in items:
        bb = p.GetBounds()
        for i in range(3):
            b[2 * i] = min(b[2 * i], bb[2 * i])
            b[2 * i + 1] = max(b[2 * i + 1], bb[2 * i + 1])
    return b, [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]


def _opacity(depth: int, max_depth: int) -> float:
    if depth >= max_depth:
        return 1.0
    return round(0.07 + 0.16 * depth, 3)  # outer faint -> inner denser


def render_six_views(items, center, out_png):
    ren = vtk.vtkRenderer()
    ren.SetBackground(*BG)
    max_depth = max(it[4] for it in items)
    for _, poly, _, col, depth in items:
        mp = vtk.vtkPolyDataMapper()
        mp.SetInputData(poly)
        mp.ScalarVisibilityOff()
        a = vtk.vtkActor()
        a.SetMapper(mp)
        pr = a.GetProperty()
        pr.SetColor(*col)
        pr.SetOpacity(_opacity(depth, max_depth))
        pr.SetSpecular(0.25)
        pr.SetSpecularPower(25)
        pr.SetInterpolationToPhong()
        ren.AddActor(a)
    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.SetSize(780, 780)
    rw.AddRenderer(ren)
    rw.SetAlphaBitPlanes(1)
    rw.SetMultiSamples(0)
    ren.SetUseDepthPeeling(1)
    ren.SetMaximumNumberOfPeels(80)
    ren.SetOcclusionRatio(0.0)
    # mesh X = superior, Y = anterior, Z = right (head-atlas convention)
    views = [
        ("top", (1, 0, 0), (0, 1, 0)),
        ("bottom", (-1, 0, 0), (0, 1, 0)),
        ("right", (0, 0, 1), (1, 0, 0)),
        ("left", (0, 0, -1), (1, 0, 0)),
        ("front", (0, 1, 0), (1, 0, 0)),
        ("back", (0, -1, 0), (1, 0, 0)),
    ]
    cam = ren.GetActiveCamera()
    imgs = []
    for nm, d, up in views:
        cam.SetFocalPoint(*center)
        cam.SetPosition(center[0] + d[0], center[1] + d[1], center[2] + d[2])
        cam.SetViewUp(*up)
        ren.ResetCamera()
        cam.Zoom(1.2)
        ren.ResetCameraClippingRange()
        imgs.append((nm, _capture(rw)))
    fig, axes = plt.subplots(2, 3, figsize=(15, 10))
    fig.patch.set_facecolor(BG)
    for ax, (nm, arr) in zip(axes.ravel(), imgs):
        ax.imshow(arr)
        ax.set_title(nm.upper(), color="white", fontsize=13)
        ax.axis("off")
    handles = [Patch(facecolor=it[3], label=it[2]) for it in items]
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=min(len(items), 6),
        facecolor=BG,
        labelcolor="white",
        framealpha=0,
    )
    fig.suptitle(
        "Nested envelope surfaces - all shells (graded translucency)", color="white", fontsize=14
    )
    fig.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(out_png, dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)


def _hemisection(items, center, normal, cam_dir, up):
    ren = vtk.vtkRenderer()
    ren.SetBackground(*BG)
    planes = vtk.vtkPlaneCollection()
    pl = vtk.vtkPlane()
    pl.SetOrigin(*center)
    pl.SetNormal(*normal)
    planes.AddItem(pl)
    cd = np.array(cam_dir, float)
    cd /= np.linalg.norm(cd)
    for i, (_, poly, _, col, _) in enumerate(items):
        clip = vtk.vtkClipClosedSurface()
        clip.SetInputData(poly)
        clip.SetClippingPlanes(planes)
        clip.GenerateFacesOn()
        clip.SetScalarModeToNone()
        clip.Update()
        mp = vtk.vtkPolyDataMapper()
        mp.SetInputConnection(clip.GetOutputPort())
        mp.ScalarVisibilityOff()
        a = vtk.vtkActor()
        a.SetMapper(mp)
        off = cd * i * 0.7  # inner shells toward camera -> no coplanar z-fight
        a.SetPosition(off[0], off[1], off[2])
        pr = a.GetProperty()
        pr.SetColor(*col)
        pr.SetSpecular(0.2)
        pr.SetSpecularPower(20)
        pr.SetInterpolationToPhong()
        ren.AddActor(a)
    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.SetSize(820, 820)
    rw.AddRenderer(ren)
    cam = ren.GetActiveCamera()
    cam.SetFocalPoint(*center)
    cam.SetPosition(center[0] + cam_dir[0], center[1] + cam_dir[1], center[2] + cam_dir[2])
    cam.SetViewUp(*up)
    ren.ResetCamera()
    cam.Zoom(1.4)
    cam.Azimuth(16)
    cam.Elevation(10)
    ren.ResetCameraClippingRange()
    return _capture(rw)


def render_cutaway(items, center, out_png):
    cor = _hemisection(items, center, (0, -1, 0), (0, 1, 0), (1, 0, 0))
    sag = _hemisection(items, center, (0, 0, -1), (0, 0, 1), (1, 0, 0))
    fig, axes = plt.subplots(1, 2, figsize=(14, 8))
    fig.patch.set_facecolor(BG)
    for ax, arr, t in zip(axes, (cor, sag), ("coronal hemisection", "sagittal hemisection")):
        ax.imshow(arr)
        ax.set_title(t, color="white", fontsize=13)
        ax.axis("off")
    handles = [Patch(facecolor=it[3], label=it[2]) for it in items]
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=min(len(items), 6),
        facecolor=BG,
        labelcolor="white",
        framealpha=0,
    )
    fig.suptitle(
        "Nested envelope surfaces - capped cutaway (all shells as solid bands)",
        color="white",
        fontsize=14,
    )
    fig.tight_layout(rect=[0, 0.04, 1, 1])
    fig.savefig(out_png, dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="render nested envelope surfaces (6 views + cutaway)")
    ap.add_argument("--config", help="pipeline_config*.json (for parents/info/output_dir)")
    ap.add_argument("--dir", help="surfaces_envelopes dir (default: from config output_dir)")
    ap.add_argument("--info", help="INFO.txt for tissue names (default: config info_txt)")
    ap.add_argument("--parents", help="envelope_parents as JSON (default: from config)")
    ap.add_argument("--six", action="store_true", help="only the 6-view image")
    ap.add_argument("--cutaway", action="store_true", help="only the cutaway image")
    args = ap.parse_args()

    cfg = _load_config(args.config) if args.config else {}
    surf_dir = args.dir
    if not surf_dir:
        out_dir = cfg["output_dir"]
        surf_dir = os.path.join(out_dir, "vtk_export", "conformal", "surfaces_envelopes")
    info = args.info or cfg.get("info_txt", "")
    parents = json.loads(args.parents) if args.parents else cfg.get("envelope_parents", {})
    parents = {int(k): int(v) for k, v in parents.items()}
    names = _names_from_info(info)

    items = collect(surf_dir, names, parents)
    if not items:
        print(f"[render] no surface_*.vtk in {surf_dir}", file=sys.stderr)
        return 2
    _, center = _bbox_center(items)
    print(f"[render] {len(items)} surfaces: " + ", ".join(f"{it[2]}(d{it[4]})" for it in items))

    do_six = args.six or not args.cutaway
    do_cut = args.cutaway or not args.six
    if do_six:
        p = os.path.join(surf_dir, "preview_6views.png")
        render_six_views(items, center, p)
        print("[render] saved", p)
    if do_cut:
        p = os.path.join(surf_dir, "preview_cutaway.png")
        render_cutaway(items, center, p)
        print("[render] saved", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
