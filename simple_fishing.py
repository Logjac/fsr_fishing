import pygame
import serial
import serial.tools.list_ports
import sys
import math

# ============================================================
#  MAGNET FISHING  -  single-FSR catch game
#  Built on top of the FSR gauge example.
# ============================================================

# ---------------- Serial / display ----------------
SERIAL_PORT = None          # None = auto-detect
BAUD_RATE   = 115200
WIDTH, HEIGHT = 900, 600
FPS = 60
FSR_RAW_MAX = 3150

# ---------------- Game tuning (tweak freely) ----------------
GAME_DURATION = 60.0    # total round length (seconds)
BITE_TIME     = 3.0     # magnets must stay attached this long for a fish to latch
FILL_RATE     = 18.0    # catch-meter %/sec gained while pulling in the sweet spot
DROP_RATE     = 40.0    # catch-meter %/sec lost while detached during the reel
METER_MAX     = 100.0
CATCH_FLASH   = 1.2     # how long the "Nice catch!" celebration shows (seconds)

# Force zones, in NORMALIZED force (0 = detached, 1 = fully attached / resting)
DETACH_T = 0.15   # below this           -> magnets are DETACHED
PULL_T   = 0.70   # DETACH_T .. PULL_T    -> PULLING lightly  (fills the meter)
                  # above PULL_T          -> attached but RESTING (no fill)

# Moving target band used while REELING (Stardew-style) - the player has to
# track this band with their force. Kept away from DETACH_T since the FSR
# reads unreliably near detachment, not near full force.
TARGET_FLOOR = 0.40   # the band never asks for less force than this
TARGET_CAP   = 1.00   # the band can ask for up to full force
TARGET_WIDTH = 0.30   # width of the target band, in normalized force
TARGET_FREQ  = 1.3    # rad/sec - how fast the band drifts back and forth

SMOOTH = 0.35     # exponential smoothing on the raw reading (higher = snappier)

# ---------------- Colors ----------------
C_BG    = (16, 20, 28)
C_PANEL = (26, 32, 42)
C_WATER = (32, 92, 138)
C_WATER_DK = (20, 60, 96)
C_RED   = (214, 78, 72)
C_GREEN = (96, 204, 116)
C_BLUE  = (74, 152, 220)
C_GOLD  = (236, 196, 92)
C_AMBER = (224, 150, 60)
C_TEXT  = (226, 230, 236)
C_DIM   = (118, 128, 140)


def find_esp32_port():
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if any(x in p.description.lower() for x in ['usb', 'uart', 'cp210', 'ch340', 'esp']):
            return p.device
    if ports:
        return ports[0].device
    return None


def zone_of(norm):
    if norm < DETACH_T:
        return 'detached'
    if norm < PULL_T:
        return 'pulling'
    return 'resting'


# ============================================================
#  Drawing helpers
# ============================================================
def draw_zone_gauge(surface, cx, cy, radius, norm, fonts, target=None, yank=False, t=0.0):
    """Semicircle force gauge.
    norm 0..1 (1 = fully attached / resting, 0 = detached).
    The track is shaded by zone so the player can aim for the green band.
    If `target` is given as (lo, hi), it overrides the static green band with
    a live, moving target window (used while reeling).
    If `yank` is True (landing phase), all zone coloring drops away - the
    track goes neutral and one big arrow points toward the 0 end, showing
    the direction to pull the needle."""
    START_DEG, END_DEG = 180, 0
    sweep = 180.0
    inner = radius - 24

    for deg in range(END_DEG, START_DEG + 1):
        frac = (START_DEG - deg) / sweep          # 0 at left, 1 at right
        if yank:
            base = C_DIM
        elif frac < DETACH_T:
            base = C_RED
        elif target is not None:
            base = C_GREEN if target[0] <= frac <= target[1] else C_AMBER
        elif frac < PULL_T:
            base = C_GREEN
        else:
            base = C_BLUE
        # filled portion bright, the rest dimmed
        if frac <= norm:
            col = base
        else:
            col = tuple(int(c * 0.28) for c in base)
        rad = math.radians(deg)
        x1 = cx + inner * math.cos(rad)
        y1 = cy - inner * math.sin(rad)
        x2 = cx + radius * math.cos(rad)
        y2 = cy - radius * math.sin(rad)
        pygame.draw.line(surface, col, (int(x1), int(y1)), (int(x2), int(y2)), 3)

    # zone tick labels along the arc
    def arc_label(frac, text, color):
        a = math.radians(START_DEG - frac * sweep)
        lx = cx + (radius + 16) * math.cos(a)
        ly = cy - (radius + 16) * math.sin(a)
        s = fonts['tiny'].render(text, True, color)
        surface.blit(s, (int(lx) - s.get_width() // 2, int(ly) - 6))
    if yank:
        arc_label(0.06, "YANK!", C_GOLD)
    else:
        arc_label(0.06, "OFF", C_RED)
        if target is not None:
            arc_label((target[0] + target[1]) / 2, "TARGET", C_GREEN)
        else:
            arc_label((DETACH_T + PULL_T) / 2, "PULL", C_GREEN)
            arc_label(0.92, "HOLD", C_BLUE)

    # needle
    fill_deg = START_DEG - norm * sweep
    nrad = math.radians(fill_deg)
    nx = cx + (radius - 8) * math.cos(nrad)
    ny = cy - (radius - 8) * math.sin(nrad)
    pygame.draw.line(surface, C_TEXT, (cx, cy), (int(nx), int(ny)), 4)
    pygame.draw.circle(surface, C_TEXT, (cx, cy), 8)
    pygame.draw.circle(surface, C_BG, (cx, cy), 3)

    if yank:
        # an arrow that curves along the outside of the arc, sweeping from
        # the current needle position down to the 0 end, plus a burst of
        # attention marks right at the tip - that's where to pull to
        pulse = 0.5 + 0.5 * math.sin(t * 9)
        curve_r = radius + 30
        start_frac = max(0.30, min(0.92, norm))
        steps = 28
        arc_pts = []
        for i in range(steps + 1):
            frac = start_frac - (start_frac - 0.0) * (i / steps)
            deg = START_DEG - frac * sweep
            rad = math.radians(deg)
            arc_pts.append((cx + curve_r * math.cos(rad), cy - curve_r * math.sin(rad)))
        pygame.draw.lines(surface, C_GOLD, False, arc_pts, 5)

        # arrowhead at the tip, oriented along the curve's direction of travel
        tipx, tipy = arc_pts[-1]
        px, py = arc_pts[-3]
        ux, uy = tipx - px, tipy - py
        ulen = math.hypot(ux, uy) or 1.0
        ux, uy = ux / ulen, uy / ulen
        perp = (-uy, ux)
        nose = (tipx + ux * (16 + 4 * pulse), tipy + uy * (16 + 4 * pulse))
        left = (tipx + perp[0] * 10, tipy + perp[1] * 10)
        right = (tipx - perp[0] * 10, tipy - perp[1] * 10)
        pygame.draw.polygon(surface, C_GOLD, [nose, left, right])

        # attention burst right where the arrow points
        for ang in (200, 220, 245, 270):
            a = math.radians(ang)
            r1 = 14 + 3 * pulse
            r2 = 22 + 6 * pulse
            x1 = tipx + r1 * math.cos(a)
            y1 = tipy - r1 * math.sin(a)
            x2 = tipx + r2 * math.cos(a)
            y2 = tipy - r2 * math.sin(a)
            pygame.draw.line(surface, C_GOLD, (x1, y1), (x2, y2), 2)

    # zone name under the hub
    if yank:
        name, col = "YANK!", C_GOLD
    elif target is not None:
        z = zone_of(norm)
        if z == 'detached':
            name, col = "DETACHED", C_RED
        elif target[0] <= norm <= target[1]:
            name, col = "ON TARGET", C_GREEN
        else:
            name, col = "ADJUST!", C_AMBER
    else:
        z = zone_of(norm)
        name, col = {
            'detached': ("DETACHED", C_RED),
            'pulling':  ("PULLING",  C_GREEN),
            'resting':  ("ATTACHED", C_BLUE),
        }[z]
    s = fonts['mid'].render(name, True, col)
    surface.blit(s, (cx - s.get_width() // 2, cy + 14))


def draw_catch_meter(surface, x, y, w, h, pct, full, t, fonts):
    pygame.draw.rect(surface, C_PANEL, (x, y, w, h), border_radius=8)
    fh = int((pct / 100.0) * (h - 6))
    fy = y + h - 3 - fh
    col = C_GOLD if pct >= 99 else C_GREEN
    if fh > 0:
        pygame.draw.rect(surface, col, (x + 3, fy, w - 6, fh), border_radius=6)
    border = C_GOLD if full else (66, 74, 86)
    bw = 3 if not full else int(2 + 2 * (0.5 + 0.5 * math.sin(t * 9)))
    pygame.draw.rect(surface, border, (x, y, w, h), bw, border_radius=8)

    lbl = fonts['tiny'].render("CATCH", True, C_DIM)
    surface.blit(lbl, (x + w // 2 - lbl.get_width() // 2, y - 20))
    pc = fonts['small'].render(f"{int(pct)}%", True, C_TEXT)
    surface.blit(pc, (x + w // 2 - pc.get_width() // 2, y + h + 6))


def draw_pond(surface, rect, t):
    x, y, w, h = rect
    pygame.draw.rect(surface, C_WATER, rect, border_radius=12)
    # depth shading toward the bottom
    band = pygame.Surface((w, h), pygame.SRCALPHA)
    for i in range(h):
        a = int(70 * (i / h))
        pygame.draw.line(band, (*C_WATER_DK, a), (0, i), (w, i))
    surface.blit(band, (x, y))
    # animated ripple lines
    for k in range(3):
        yy = y + 30 + k * 26
        pts = []
        for i in range(0, w + 1, 10):
            pts.append((x + i, yy + 4 * math.sin(i * 0.05 + t * 2 + k)))
        if len(pts) > 1:
            pygame.draw.lines(surface, (*C_WATER_DK, 0)[:3], False, pts, 1)
    pygame.draw.rect(surface, (60, 120, 162), rect, 2, border_radius=12)


def draw_fish(surface, cx, cy, t, color, tug=0.0):
    cx = int(cx + tug * 6 * math.sin(t * 30))
    bob = math.sin(t * 2) * 3
    cy = int(cy + bob)
    pygame.draw.ellipse(surface, color, (cx - 24, cy - 11, 48, 22))
    pygame.draw.polygon(surface, color,
                        [(cx - 22, cy), (cx - 40, cy - 11), (cx - 40, cy + 11)])
    pygame.draw.circle(surface, (250, 250, 250), (cx + 13, cy - 3), 4)
    pygame.draw.circle(surface, (20, 20, 20), (cx + 14, cy - 3), 2)


def draw_line_and_hook(surface, top_x, top_y, hook_x, hook_y, taut_color):
    pygame.draw.line(surface, taut_color, (top_x, top_y), (hook_x, hook_y), 2)
    pygame.draw.circle(surface, (40, 40, 46), (hook_x, hook_y), 6)
    pygame.draw.circle(surface, (200, 60, 60), (hook_x, hook_y), 3)  # the magnet


# ============================================================
#  Main
# ============================================================
def main():
    port = SERIAL_PORT or find_esp32_port()
    ser = None
    kb_mode = False
    if port:
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
            print(f"Connected to {port} @ {BAUD_RATE}.")
        except Exception as e:
            print(f"Could not open {port}: {e}")
    if ser is None:
        kb_mode = True
        print("No serial connection -> KEYBOARD TEST MODE (hold UP / DOWN to fake force).")

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Magnet Fishing")
    clock = pygame.time.Clock()

    fonts = {
        'big':   pygame.font.SysFont('arial', 30, bold=True),
        'mid':   pygame.font.SysFont('arial', 20, bold=True),
        'small': pygame.font.SysFont('arial', 16),
        'tiny':  pygame.font.SysFont('arial', 12),
        'mono':  pygame.font.SysFont('monospace', 13),
    }

    # layout
    POND  = (24, 80, 470, 380)
    METER = (512, 80, 42, 380)
    GCX, GCY, GR = 730, 305, 140
    hook_x = POND[0] + POND[2] // 2
    hook_y = POND[1] + int(POND[3] * 0.5)
    top_x  = hook_x
    top_y  = POND[1] + 4

    # sensor / calibration
    raw1 = 0
    raw_smooth = 0.0
    cal_min = 0
    cal_max = FSR_RAW_MAX
    flash_msg = ''
    flash_timer = 0.0

    # game state
    state = 'ready'            # ready -> casting -> biting -> reeling -> landing -> caught -> (gameover)
    score = 0
    time_left = GAME_DURATION
    bite_elapsed = 0.0
    meter = 0.0
    caught_timer = 0.0
    reel_elapsed = 0.0
    t = 0.0

    # bounds for the moving target's center, so the band itself never asks
    # for less force than DETACH_T or more than TARGET_CAP
    target_center_lo = TARGET_FLOOR + TARGET_WIDTH / 2
    target_center_hi = TARGET_CAP - TARGET_WIDTH / 2
    target_mid = (target_center_lo + target_center_hi) / 2
    target_amp = (target_center_hi - target_center_lo) / 2

    def reset_game():
        nonlocal state, score, time_left, bite_elapsed, meter, caught_timer
        state = 'casting'
        score = 0
        time_left = GAME_DURATION
        bite_elapsed = 0.0
        meter = 0.0
        caught_timer = 0.0

    running = True
    while running:
        dt = clock.tick(FPS) / 1000.0
        t += dt
        if flash_timer > 0:
            flash_timer -= dt

        # -------- events --------
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_SPACE and state == 'ready':
                    reset_game()
                elif event.key == pygame.K_r and state == 'gameover':
                    reset_game()
                elif event.key == pygame.K_o:
                    cal_min = int(raw_smooth)
                    flash_msg = f"min set -> {cal_min}"
                    flash_timer = 1.8
                elif event.key == pygame.K_p:
                    cal_max = int(raw_smooth)
                    flash_msg = f"max set -> {cal_max}"
                    flash_timer = 1.8

        # -------- input: serial or keyboard --------
        if kb_mode:
            keys = pygame.key.get_pressed()
            if keys[pygame.K_UP]:
                raw1 = min(FSR_RAW_MAX, raw1 + 90)
            if keys[pygame.K_DOWN]:
                raw1 = max(0, raw1 - 90)
        else:
            try:
                while ser.in_waiting:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    parts = line.split(',')
                    try:
                        raw1 = int(parts[0])   # FSR 1 only
                    except ValueError:
                        pass
            except Exception:
                pass

        # smooth + normalize
        raw_smooth += SMOOTH * (raw1 - raw_smooth)
        span = (cal_max - cal_min) or 1
        norm = max(0.0, min(1.0, (raw_smooth - cal_min) / span))
        z = zone_of(norm)
        attached = (z != 'detached')
        target_lo = target_hi = None

        # -------- game clock --------
        if state not in ('ready', 'gameover'):
            time_left -= dt
            if time_left <= 0:
                time_left = 0.0
                state = 'gameover'

        # -------- state machine --------
        if state == 'casting':
            if attached:
                state = 'biting'
                bite_elapsed = 0.0
        elif state == 'biting':
            if not attached:
                state = 'casting'           # let go too soon, no bite
            else:
                bite_elapsed += dt
                if bite_elapsed >= BITE_TIME:
                    state = 'reeling'
                    meter = 0.0
                    reel_elapsed = 0.0
        elif state == 'reeling':
            target_center = target_mid + target_amp * math.sin(reel_elapsed * TARGET_FREQ)
            target_lo = target_center - TARGET_WIDTH / 2
            target_hi = target_center + TARGET_WIDTH / 2
            reel_elapsed += dt
            if not attached or not (target_lo <= norm <= target_hi):
                meter = max(0.0, meter - DROP_RATE * dt)
            else:
                meter = min(METER_MAX, meter + FILL_RATE * dt)
            if meter >= METER_MAX:
                state = 'landing'
        elif state == 'landing':
            if z == 'detached':             # the yank!
                score += 1
                state = 'caught'
                caught_timer = CATCH_FLASH
        elif state == 'caught':
            caught_timer -= dt
            if caught_timer <= 0:
                state = 'casting'

        # ============== draw ==============
        screen.fill(C_BG)

        # HUD
        screen.blit(fonts['big'].render(f"Score {score}", True, C_TEXT), (24, 18))
        tcol = C_RED if time_left <= 10 and state not in ('ready', 'gameover') else C_TEXT
        tstr = fonts['big'].render(f"{int(time_left):02d}s", True, tcol)
        screen.blit(tstr, (WIDTH - tstr.get_width() - 24, 18))

        # pond + line + fish
        draw_pond(screen, POND, t)
        show_fish = state in ('biting', 'reeling', 'landing', 'caught')
        in_target = target_lo is not None and target_lo <= norm <= target_hi
        line_col = C_GOLD if (state == 'reeling' and in_target) else (210, 210, 215)
        draw_line_and_hook(screen, top_x, top_y, hook_x, hook_y, line_col)
        if show_fish:
            tug = 1.0 if state in ('reeling', 'landing') else 0.0
            fcol = C_GOLD if state == 'caught' else (180, 188, 196)
            draw_fish(screen, hook_x + 34, hook_y, t, fcol, tug)

        # catch meter + gauge
        draw_catch_meter(screen, *METER, meter, state == 'landing', t, fonts)
        gauge_target = (target_lo, target_hi) if target_lo is not None else None
        draw_zone_gauge(screen, GCX, GCY, GR, norm, fonts, target=gauge_target,
                         yank=(state == 'landing'), t=t)
        rawtxt = fonts['mono'].render(
            f"raw:{int(raw_smooth):>4}  min:{cal_min} max:{cal_max}", True, C_DIM)
        screen.blit(rawtxt, (GCX - rawtxt.get_width() // 2, GCY + 44))

        # state prompt
        prompt, pcol = "", C_TEXT
        if state == 'ready':
            prompt, pcol = "Press SPACE to start fishing", C_TEXT
        elif state == 'casting':
            prompt, pcol = "Drop the line  -  attach the magnets to cast", C_BLUE
        elif state == 'biting':
            n = max(1, math.ceil(BITE_TIME - bite_elapsed))
            prompt, pcol = f"Something's nibbling... hold steady!   {n}", C_GOLD
        elif state == 'reeling':
            prompt, pcol = "Reel it in!  Track the moving TARGET zone", C_GREEN
        elif state == 'landing':
            prompt, pcol = "It's hooked!  YANK to land it!", C_GOLD
        elif state == 'caught':
            prompt, pcol = "Nice catch!  +1", C_GREEN
        elif state == 'gameover':
            prompt, pcol = f"Time!  Final score: {score}   -   Press R to play again", C_TEXT
        ps = fonts['mid'].render(prompt, True, pcol)
        screen.blit(ps, (WIDTH // 2 - ps.get_width() // 2, 498))

        # flash + controls footer
        if flash_timer > 0:
            fs = fonts['small'].render(flash_msg, True, C_GREEN)
            screen.blit(fs, (WIDTH // 2 - fs.get_width() // 2, 470))
        ctrl = "O set min   P set max   ESC quit"
        if kb_mode:
            ctrl = "[KEYBOARD MODE]  hold UP/DOWN = force    " + ctrl
        cs = fonts['tiny'].render(ctrl, True, (80, 88, 98))
        screen.blit(cs, (WIDTH // 2 - cs.get_width() // 2, 560))

        pygame.display.flip()

    if ser:
        ser.close()
    pygame.quit()


if __name__ == '__main__':
    main()