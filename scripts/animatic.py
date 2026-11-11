#!/usr/bin/env python3
"""
Offline animatic builder for Zack D Director.

Builds a timing/pacing preview of a project WITHOUT any API calls: every shot
becomes a placeholder card (shot type, scene description, character refs) with
its camera move applied, a burned caption, and local espeak-ng narration. The
clips are then cut together with scripts/assemble.py, so transitions and impact
zooms match the real pipeline.

Narration is split per shot. If a shot has a "narration" field it is used;
otherwise the beat narration is split at the "|" markers given via --splits
(a JSON file mapping shot_id -> line), or spoken whole on the beat's first shot.

Output: out/<project>/animatic/final.mp4 (real renders keep using clips/).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

W, H = 720, 1280
SCALE = 2  # render cards at 2x so zoompan motion stays smooth
FPS = 30
PAD_SEC = 0.45  # breathing room after each line (covers the 0.35s crossfade)
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BEAT_COLORS = [(18, 28, 58), (58, 18, 30), (14, 46, 52), (40, 22, 60), (52, 38, 14), (20, 44, 30)]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}... failed:\n{r.stderr[-2000:]}")
    return r.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", path]).strip())


def font(path, size):
    return ImageFont.truetype(path, size * SCALE)


def draw_wrapped(d, xy, text, fnt, fill, width_chars, spacing=10):
    lines = textwrap.wrap(text, width_chars)
    x, y = xy
    for line in lines:
        d.text((x, y), line, font=fnt, fill=fill)
        y += fnt.getbbox("Ag")[3] + spacing * SCALE
    return y


def make_card(path, beat_idx, beat, shot):
    w, h = W * SCALE, H * SCALE
    base = BEAT_COLORS[beat_idx % len(BEAT_COLORS)]
    img = Image.new("RGB", (w, h))
    px = img.load()
    # vertical studio gradient with a soft center glow
    for y in range(h):
        t = y / h
        row = tuple(int(c * (1.25 - 0.6 * t)) for c in base)
        for x in range(0, w):
            px[x, y] = row
    d = ImageDraw.Draw(img)
    m = 100 * SCALE  # wide margins: pans/tilts crop up to 12% of the frame
    d.text((m, 170 * SCALE), f"{beat['beat_id'].replace('_', ' ').upper()} · {beat.get('title_en', '')}",
           font=font(FONT_BOLD, 30), fill=(255, 196, 80))
    y = draw_wrapped(d, (m, 215 * SCALE),
                     f"SHOT {shot['shot_id'].split('_')[-1].upper()} · {shot['type'].replace('_', ' ').upper()}",
                     font(FONT_BOLD, 38), (255, 255, 255), 20, 4)
    y = draw_wrapped(d, (m, y + 25 * SCALE), shot["scene_description"], font(FONT_REG, 28),
                     (225, 230, 240), 32, 10)
    y += 30 * SCALE
    d.text((m, y), f"camera: {shot['camera_move'].replace('_', ' ')}"
           + ("   ·   IMPACT ZOOM" if shot.get("zoom_impact") else ""),
           font=font(FONT_BOLD, 24), fill=(140, 200, 255))
    y += 50 * SCALE
    for ref in shot.get("character_refs", []):
        label = ref.replace("_turnaround", "").replace("_", " ")
        tw = d.textlength(label, font=font(FONT_REG, 22))
        d.rounded_rectangle((m, y, m + tw + 30 * SCALE, y + 40 * SCALE), radius=20 * SCALE,
                            outline=(255, 255, 255), width=2 * SCALE)
        d.text((m + 15 * SCALE, y + 7 * SCALE), label, font=font(FONT_REG, 22), fill=(255, 255, 255))
        y += 52 * SCALE
    d.text((m, h - 190 * SCALE), "PLACEHOLDER · 3D render pending", font=font(FONT_BOLD, 22),
           fill=(255, 255, 255, 120))
    img.save(path)


def make_caption(path, text):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fnt = ImageFont.truetype(FONT_BOLD, 40)
    lines = textwrap.wrap(text.upper(), 22)
    lh = 52
    y = int(H * 0.72) - (len(lines) * lh) // 2
    for line in lines:
        tw = d.textlength(line, font=fnt)
        d.text(((W - tw) / 2, y), line, font=fnt, fill=(255, 255, 255),
               stroke_width=5, stroke_fill=(0, 0, 0))
        y += lh
    img.save(path)


def motion_filter(move, frames):
    n = max(frames - 1, 1)
    z_end = 1.15
    if move == "push_in":
        z, x, y = f"1+{z_end - 1}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif move == "pull_out":
        z, x, y = f"{z_end}-{z_end - 1}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif move == "pan_right":
        z, x, y = "1.12", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"
    elif move == "pan_left":
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"
    elif move == "tilt_down":
        z, x, y = "1.12", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{n}"
    elif move == "tilt_up":
        z, x, y = "1.12", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{n})"
    else:  # static: gentle drift so the card doesn't look frozen
        z, x, y = f"1+0.03*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}"


def build(project_dir, splits, voice, speed):
    with open(os.path.join(project_dir, "beats.json"), encoding="utf-8") as f:
        data = json.load(f)
    work = os.path.join(project_dir, "animatic")
    tmp = os.path.join(work, "_work")
    clips = os.path.join(work, "clips")
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(clips, exist_ok=True)
    shutil.copy(os.path.join(project_dir, "beats.json"), os.path.join(work, "beats.json"))

    for bi, beat in enumerate(data["beats"]):
        for si, shot in enumerate(beat["shots"]):
            sid = shot["shot_id"]
            line = shot.get("narration") or splits.get(sid)
            if line is None:
                line = beat["narration"] if si == 0 else ""
            card, cap, wav = (os.path.join(tmp, f"{sid}{ext}") for ext in (".png", "_cap.png", ".wav"))
            make_card(card, bi, beat, shot)
            make_caption(cap, line)
            speech = 0.0
            if line.strip():
                run(["espeak-ng", "-v", voice, "-s", str(speed), "-w", wav, line])
                speech = duration(wav)
            dur = max(shot.get("duration_sec", 3.0) * 0.67, speech + PAD_SEC)
            frames = int(round(dur * FPS))
            dur = frames / FPS
            audio_in = (["-i", wav] if speech else
                        ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo"])
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-loop", "1", "-i", card, "-i", cap, *audio_in,
                 "-filter_complex",
                 f"[0:v]{motion_filter(shot['camera_move'], frames)},format=rgba[bg];"
                 f"[bg][1:v]overlay=0:0,format=yuv420p[v];"
                 f"[2:a]adelay=120|120,apad,atrim=0:{dur},aresample=48000[a]",
                 "-map", "[v]", "-map", "[a]", "-frames:v", str(frames), "-t", f"{dur}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-c:a", "aac", "-ac", "2", os.path.join(clips, f"{sid}.mp4")])
            print(f"  [clip] {sid}: {dur:.2f}s  “{line}”")

    here = os.path.dirname(os.path.abspath(__file__))
    run([sys.executable, os.path.join(here, "assemble.py"), work])
    out = os.path.join(work, "final.mp4")
    print(f"[Animatic] {out} ({duration(out):.1f}s)")
    return out


def main():
    p = argparse.ArgumentParser(description="Build an offline placeholder animatic")
    p.add_argument("project_dir")
    p.add_argument("--splits", help="JSON file mapping shot_id -> narration line")
    p.add_argument("--voice", default="en-us+m3")
    p.add_argument("--speed", type=int, default=170, help="espeak words per minute")
    a = p.parse_args()
    splits = {}
    if a.splits:
        with open(a.splits, encoding="utf-8") as f:
            splits = json.load(f)
    build(a.project_dir, splits, a.voice, a.speed)


if __name__ == "__main__":
    main()
