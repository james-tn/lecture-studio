"""Headless verification + frame capture of the whiteboard player."""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
URL = (ROOT / "out" / "index.html").as_uri()


def main():
    shots = ROOT / "out" / "frames"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
        pg = b.new_page(viewport={"width": 1320, "height": 840})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
        pg.goto(URL)
        pg.wait_for_selector("#play:not([disabled])", timeout=20000)
        durs = pg.evaluate("durations")
        total = sum(d or 0 for d in durs)
        print(f"durations: {[round(d,1) for d in durs]}  total={total:.1f}s")
        pg.click("#play")

        marks = [t for t in (2, 6, 12, 20, 30, 42, 55) if t < total + 4]
        last = 0
        for t in marks:
            time.sleep(max(0, t - last)); last = t
            n_obj = pg.eval_on_selector_all(".obj", "els => els.length")
            n_svg = pg.eval_on_selector_all("#svgLayer *", "els => els.length")
            pg.screenshot(path=str(shots / f"t{t:02d}.png"))
            print(f"  t={t:2d}s  objs={n_obj:2d}  svg_nodes={n_svg:3d}")
        print("console errors:", errs[:8] if errs else "none")
        b.close()


if __name__ == "__main__":
    main()
