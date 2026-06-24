import pygame
import serial
import serial.tools.list_ports
import os
import sys
import math
import random

# ============================================================
#  MAGNET FISHING  -  single-FSR catch game
#  Layout: the ocean (bg.png) fills the background, a hook
#  (hook.png) hangs on a black string from the top center and
#  rides up/down with pressure, a fish bites the hook and rides
#  with it, parallax bubbles drift up at varying depths, and one
#  combined PRESSURE meter sits on the right (a vertical bar with
#  zones + a moving mouse/pressure indicator line). The "what to
#  do" prompt is centered low on the screen.
# ============================================================

# ---------------- Serial / display ----------------
SERIAL_PORT = None          # None = auto-detect
BAUD_RATE   = 115200
WIDTH, HEIGHT = 900, 600
FPS = 60
FSR_RAW_MIN = 300
FSR_RAW_MAX = 3150

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# ---------------- Game tuning (tweak freely) ----------------
BITE_TIME     = 3.0     # magnets must stay attached this long for a fish to latch
FILL_RATE     = 24.0    # catch-meter %/sec gained while pulling in the sweet spot
DROP_RATE     = 36.0    # catch-meter %/sec lost while detached during the reel
METER_START   = 20.0    # catch meter starts here when a fish is hooked
METER_MAX     = 100.0
CATCH_FLASH   = 1.2     # how long the "Nice catch!" celebration shows (seconds)

# Force zones, in NORMALIZED force (0 = detached, 1 = fully attached / resting)
DETACH_T = 0.15   # below this           -> magnets are DETACHED
PULL_T   = 0.70   # DETACH_T .. PULL_T    -> PULLING lightly  (fills the meter)
                  # above PULL_T          -> attached but RESTING (no fill)

# Moving target band used while REELING (Stardew-style) - the player has to
# track this band with their force. Kept away from DETACH_T since the FSR
# reads unreliably near detachment, not near full force.
TARGET_FLOOR = 0.32   # the band never asks for less force than this
TARGET_CAP   = 1.00   # the band can ask for up to full force
TARGET_WIDTH = 0.30   # width of the target band, in normalized force (narrow = hard)
TARGET_SPEED = 0.8    # norm/sec - how fast the band chases its next spot
RETARGET_MIN = 0.6    # the band jumps to a new random spot this often...
RETARGET_MAX = 1.4    # ...up to this long between jumps (seconds)

# Reel marker physics: a damped spring pulls the marker toward the live
# pressure (its rest target). Three knobs fully define the system.
SPRING_K = 40.0   # stiffness: how hard the marker is pulled toward the pressure
SPRING_C = 6.0    # damping: how fast velocity bleeds off (low -> springy overshoot)
SPRING_M = 1.0    # mass: inertia of the marker

SMOOTH = 0.35     # exponential smoothing on the raw reading (higher = snappier)

# How the hook hangs as pressure swings 0..1. 0 pressure = fully pulled off
# (hook lifted off the top of the screen); full pressure = resting deep.
HOOK_H        = 160                      # rendered hook height (px)
HOOK_OFF_TOP  = -HOOK_H - 20             # top edge when 0 pressure (off-screen)
HOOK_DEEP     = HEIGHT - HOOK_H - 110    # top edge when resting attached

# ---------------- Colors ----------------
C_PANEL = (26, 32, 42)
C_STRING = (12, 12, 16)
C_RED   = (214, 78, 72)
C_GREEN = (96, 204, 116)
C_BLUE  = (74, 152, 220)
C_GOLD  = (236, 196, 92)
C_AMBER = (224, 150, 60)
C_TEXT  = (226, 230, 236)
C_DIM   = (118, 128, 140)

# Live-tunable reel parameters shown in the in-game panel (TAB to toggle).
# (global name, label, step, min, max, value format)
TUN_SPEC = [
    ('SPRING_K',      'stiffness',2.0,  1.0, 200.0, '{:.0f}'),
    ('SPRING_C',      'damping',  0.5,  0.0, 40.0,  '{:.1f}'),
    ('SPRING_M',      'mass',     0.1,  0.1, 8.0,   '{:.1f}'),
    ('TARGET_WIDTH',  'band w',   0.01, 0.05, 0.6, '{:.2f}'),
    ('TARGET_SPEED',  'band spd', 0.1,  0.0, 4.0,  '{:.1f}'),
    ('RETARGET_MIN',  'jump min', 0.05, 0.05, 3.0, '{:.2f}'),
    ('RETARGET_MAX',  'jump max', 0.05, 0.05, 3.0, '{:.2f}'),
    ('FILL_RATE',     'fill',     2.0,  2.0, 100., '{:.0f}'),
    ('DROP_RATE',     'drop',     2.0,  2.0, 200., '{:.0f}'),
    ('METER_START',   'start %',  5.0,  0.0, 90.0, '{:.0f}'),
]


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
#  Assets
# ============================================================
def load_background():
    """Load bg.png and scale it to *cover* the window (keep aspect, crop)."""
    bg = pygame.image.load(os.path.join(ASSET_DIR, "bg.png")).convert()
    bw, bh = bg.get_size()
    scale = max(WIDTH / bw, HEIGHT / bh)
    img = pygame.transform.smoothscale(bg, (int(bw * scale), int(bh * scale)))
    off = ((WIDTH - img.get_width()) // 2, (HEIGHT - img.get_height()) // 2)
    return img, off


def load_hook():
    src = pygame.image.load(os.path.join(ASSET_DIR, "hook.png")).convert_alpha()
    w, h = src.get_size()
    return pygame.transform.smoothscale(src, (int(w * HOOK_H / h), HOOK_H))


def load_bubble_sprites():
    """bubbles4.png is a 2x2 grid -> return the four bubble sprites."""
    sheet = pygame.image.load(os.path.join(ASSET_DIR, "bubbles4.png")).convert_alpha()
    w, h = sheet.get_size()
    cw, ch = w // 2, h // 2
    sprites = []
    for gy in range(2):
        for gx in range(2):
            sprites.append(sheet.subsurface((gx * cw, gy * ch, cw, ch)).copy())
    return sprites


class Bubble:
    """A drifting bubble at a random depth. Far bubbles (depth -> 0) are
    smaller, slower and fainter; near bubbles (depth -> 1) are bigger,
    faster and more opaque, giving a parallax sense of depth. Each bubble
    cycles through all four bubbles4 sprites over time for a shimmer."""

    def __init__(self, sprites):
        self.sprites = sprites
        self._respawn(initial=True)

    def _respawn(self, initial=False):
        self.depth = random.random()                       # 0 far .. 1 near
        self.diam = int(10 + self.depth * 32)              # 10..42 px
        alpha = int(95 + self.depth * 160)                 # 95..255
        # pre-render all four sprites at this bubble's size + opacity
        self.frames = []
        for sprite in self.sprites:
            img = pygame.transform.smoothscale(sprite, (self.diam, self.diam))
            img.fill((255, 255, 255, alpha), special_flags=pygame.BLEND_RGBA_MULT)
            self.frames.append(img)
        self.anim_fps = random.uniform(3.0, 7.0)           # frame cycle speed
        self.anim_offset = random.uniform(0, len(self.frames))
        self.speed = 10 + self.depth * 48                  # px/sec upward
        self.sway_amp = 3 + self.depth * 9
        self.sway_freq = random.uniform(0.6, 1.5)
        self.sway_phase = random.uniform(0, math.tau)
        self.x = random.uniform(0, WIDTH)
        self.y = random.uniform(0, HEIGHT) if initial else HEIGHT + self.diam

    def update(self, dt):
        self.y -= self.speed * dt
        if self.y < -self.diam:
            self._respawn()

    def draw(self, surface, t):
        sx = self.x + self.sway_amp * math.sin(t * self.sway_freq + self.sway_phase)
        frame = int(t * self.anim_fps + self.anim_offset) % len(self.frames)
        surface.blit(self.frames[frame], (int(sx - self.diam / 2), int(self.y - self.diam / 2)))


# ============================================================
#  Drawing helpers
# ============================================================
def draw_pressure_meter(surface, rect, norm, target, state, t, fonts, catch_pct):
    """Combined PRESSURE widget: one vertical bar showing the force axis
    (0 = pulled off at the TOP, 1 = resting attached at the BOTTOM) with zone
    shading, plus a moving indicator line that tracks the live mouse/pressure
    value. During reeling the static zones become the moving green TARGET band;
    during landing it flips to a neutral track with a YANK cue. The catch
    progress fills the bar's outline from the bottom up (gold + pulsing once
    full)."""
    x, y, w, h = rect

    def yb(frac):
        # 0 -> top of the bar, 1 -> bottom
        frac = max(0.0, min(1.0, frac))
        return int(y + h * frac)

    def dim(c):
        return tuple(int(v * 0.32) for v in c)

    # backing panel
    pygame.draw.rect(surface, C_PANEL, (x - 6, y - 6, w + 12, h + 12), border_radius=10)

    yank = (state == 'landing')
    if yank:
        pygame.draw.rect(surface, (46, 54, 66), (x, y, w, h), border_radius=6)
    elif target is not None:
        lo, hi = target
        pygame.draw.rect(surface, dim(C_AMBER), (x, y, w, h), border_radius=6)
        pygame.draw.rect(surface, C_GREEN, (x, yb(lo), w, yb(hi) - yb(lo)))
    else:
        # static zones: red (detached/pulled off) on top, green (pull), blue (rest) at bottom
        pygame.draw.rect(surface, dim(C_RED), (x, y, w, yb(DETACH_T) - y))
        pygame.draw.rect(surface, dim(C_GREEN), (x, yb(DETACH_T), w, yb(PULL_T) - yb(DETACH_T)))
        pygame.draw.rect(surface, dim(C_BLUE), (x, yb(PULL_T), w, y + h - yb(PULL_T)))

    # current-pressure fill (translucent, from the top down to the indicator)
    iy = yb(norm)
    if iy - y > 0:
        fill = pygame.Surface((w, iy - y), pygame.SRCALPHA)
        fill.fill((255, 255, 255, 38))
        surface.blit(fill, (x, y))

    # dim base outline
    pygame.draw.rect(surface, (74, 84, 98), (x, y, w, h), 2, border_radius=6)
    # catch progress fills the outline from the bottom up
    if catch_pct > 0:
        fy = int(y + h - h * min(1.0, catch_pct / 100.0))
        full = catch_pct >= 99
        glow = C_GOLD if full else C_GREEN
        gw = int(3 + 2 * (0.5 + 0.5 * math.sin(t * 9))) if full else 3
        prev_clip = surface.get_clip()
        surface.set_clip(pygame.Rect(x - gw, fy, w + 2 * gw, (y + h) - fy + gw))
        pygame.draw.rect(surface, glow, (x, y, w, h), gw, border_radius=6)
        surface.set_clip(prev_clip)

    # the moving mouse / pressure indicator line
    ind = C_GOLD if yank else C_TEXT
    pygame.draw.line(surface, ind, (x - 12, iy), (x + w + 12, iy), 4)
    if yank:
        # pulsing arrow pointing UP -> yank the line toward 0 pressure (detach)
        pulse = int(4 * (0.5 + 0.5 * math.sin(t * 9)))
        ax = x + w + 26
        pygame.draw.polygon(surface, C_GOLD,
                            [(ax - 8, iy), (ax + 8, iy), (ax, iy - 14 - pulse)])


def draw_fish(surface, cx, cy, t, color, tug=0.0, scale=1.0):
    def s(v):
        return int(v * scale)
    cx = int(cx + tug * 6 * math.sin(t * 30))
    bob = math.sin(t * 2) * 3
    cy = int(cy + bob)
    pygame.draw.ellipse(surface, color, (cx - s(24), cy - s(11), s(48), s(22)))
    pygame.draw.polygon(surface, color,
                        [(cx - s(22), cy), (cx - s(40), cy - s(11)), (cx - s(40), cy + s(11))])
    pygame.draw.circle(surface, (250, 250, 250), (cx + s(13), cy - s(3)), max(2, s(4)))
    pygame.draw.circle(surface, (20, 20, 20), (cx + s(14), cy - s(3)), max(1, s(2)))


FIREWORK_COLORS = [C_GOLD, C_GREEN, C_BLUE, (255, 255, 255)]
FIREWORK_GRAVITY = 240.0


def spawn_firework(particles, x, y):
    color = random.choice(FIREWORK_COLORS)
    for _ in range(32):
        ang = random.uniform(0, 2 * math.pi)
        speed = random.uniform(90, 320)
        particles.append({
            'x': x, 'y': y,
            'vx': math.cos(ang) * speed,
            'vy': math.sin(ang) * speed,
            'age': 0.0,
            'life': random.uniform(0.7, 1.4),
            'color': color,
        })


def update_fireworks(particles, dt):
    for p in particles:
        p['age'] += dt
        p['x'] += p['vx'] * dt
        p['y'] += p['vy'] * dt
        p['vy'] += FIREWORK_GRAVITY * dt
    particles[:] = [p for p in particles if p['age'] < p['life']]


def draw_fireworks(surface, particles):
    for p in particles:
        frac = max(0.0, 1.0 - p['age'] / p['life'])
        r = max(1, int(6 * frac) + 2)
        col = tuple(int(c * frac) for c in p['color'])
        pygame.draw.circle(surface, col, (int(p['x']), int(p['y'])), r)


def draw_tuning_panel(surface, fonts, tun, sel_idx):
    """Live reel-parameter editor (toggle with TAB)."""
    x, y = 16, 58
    pad, line_h, w = 8, 20, 256
    h = pad * 2 + line_h * (len(TUN_SPEC) + 1)
    panel = pygame.Surface((w, h), pygame.SRCALPHA)
    panel.fill((10, 16, 24, 205))
    surface.blit(panel, (x, y))
    pygame.draw.rect(surface, (70, 84, 98), (x, y, w, h), 1, border_radius=6)
    surface.blit(fonts['tiny'].render("TAB hide   up/down pick   left/right adjust",
                                      True, C_DIM), (x + pad, y + pad))
    for i, (key, label, step, lo, hi, fmt) in enumerate(TUN_SPEC):
        ry = y + pad + line_h * (i + 1)
        sel = (i == sel_idx)
        col = C_GOLD if sel else C_TEXT
        txt = f"{'>' if sel else ' '} {label:<9}{fmt.format(tun[key])}"
        surface.blit(fonts['mono'].render(txt, True, col), (x + pad, ry))


# ============================================================
#  Main
# ============================================================
def main():
    port = SERIAL_PORT or find_esp32_port()
    ser = None
    mouse_mode = False
    if port:
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
            print(f"Connected to {port} @ {BAUD_RATE}.")
        except Exception as e:
            print(f"Could not open {port}: {e}")
    if ser is None:
        mouse_mode = True
        print("No serial connection -> MOUSE TEST MODE (move the mouse up/down to fake force).")

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Magnet Fishing")
    pygame.key.set_repeat(250, 60)   # hold arrows to keep adjusting tuning values
    clock = pygame.time.Clock()

    fonts = {
        'big':   pygame.font.SysFont('arial', 30, bold=True),
        'mid':   pygame.font.SysFont('arial', 20, bold=True),
        'small': pygame.font.SysFont('arial', 16),
        'tiny':  pygame.font.SysFont('arial', 12),
        'mono':  pygame.font.SysFont('monospace', 13),
    }

    # ---- assets ----------------------------------------------------------
    bg_img, bg_off = load_background()
    hook_img = load_hook()
    hook_w = hook_img.get_width()
    bubble_sprites = load_bubble_sprites()
    bubbles = [Bubble(bubble_sprites) for _ in range(34)]

    # ---- layout ----------------------------------------------------------
    # the right-side combined pressure meter (vertical bar)
    PM_W = 50
    PM_X = WIDTH - 96
    PM_Y = 100
    PM_H = HEIGHT - 210
    PRESSURE = (PM_X, PM_Y, PM_W, PM_H)
    # the string hangs down the screen center; the hook (and the fish biting
    # it) sit centered on that same line
    string_cx = WIDTH // 2
    hook_cx = string_cx

    # sensor / calibration
    raw1 = 0
    raw_smooth = 0.0
    cal_min = FSR_RAW_MIN
    cal_max = FSR_RAW_MAX
    flash_msg = ''
    flash_timer = 0.0

    # game state (starts fishing immediately, loops forever; R restarts the score)
    state = 'casting'          # casting -> biting -> reeling -> landing -> caught -> casting
    score = 0
    bite_elapsed = 0.0
    meter = 0.0
    caught_timer = 0.0
    reel_elapsed = 0.0
    fireworks = []
    t = 0.0

    # bounds for the moving target's center, so the band itself never asks
    # for less force than TARGET_FLOOR or more than TARGET_CAP
    target_center_lo = TARGET_FLOOR + TARGET_WIDTH / 2
    target_center_hi = TARGET_CAP - TARGET_WIDTH / 2
    # the band wanders toward a random destination, re-picking frequently
    target_center = target_center_hi
    target_dest = target_center_hi
    retarget_timer = 0.0
    # the player-controlled marker (acceleration-based) chases the band
    reel_pos = target_center_hi
    reel_vel = 0.0

    # live-tunable reel params (TAB toggles the editor panel; hidden by default)
    tun = {key: globals()[key] for key, *_ in TUN_SPEC}
    tun_visible = False
    sel_idx = 0

    def reset_game():
        nonlocal state, score, bite_elapsed, meter, caught_timer
        state = 'casting'
        score = 0
        bite_elapsed = 0.0
        meter = 0.0
        caught_timer = 0.0
        fireworks.clear()

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
                elif event.key == pygame.K_r:
                    reset_game()
                elif event.key == pygame.K_o:
                    cal_min = int(raw_smooth)
                    flash_msg = f"min set -> {cal_min}"
                    flash_timer = 1.8
                elif event.key == pygame.K_p:
                    cal_max = int(raw_smooth)
                    flash_msg = f"max set -> {cal_max}"
                    flash_timer = 1.8
                elif event.key == pygame.K_TAB:
                    tun_visible = not tun_visible
                elif event.key == pygame.K_UP:
                    sel_idx = (sel_idx - 1) % len(TUN_SPEC)
                elif event.key == pygame.K_DOWN:
                    sel_idx = (sel_idx + 1) % len(TUN_SPEC)
                elif event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                    key, _lbl, step, lo, hi, _fmt = TUN_SPEC[sel_idx]
                    d = step if event.key == pygame.K_RIGHT else -step
                    tun[key] = round(min(hi, max(lo, tun[key] + d)), 4)

        # -------- input: serial or mouse --------
        if mouse_mode:
            # mouse Y drives force to match the bar/hook: top = 0 (pulled off),
            # bottom = max (resting attached)
            _, my = pygame.mouse.get_pos()
            frac = max(0.0, min(1.0, my / HEIGHT))
            raw1 = frac * FSR_RAW_MAX
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
        # when fully released (yanked off), snap straight to 0 with no smoothing lag
        if raw1 <= cal_min:
            raw_smooth = float(raw1)
        span = (cal_max - cal_min) or 1
        norm = max(0.0, min(1.0, (raw_smooth - cal_min) / span))
        target_lo = target_hi = None

        # hook position from pressure: 0 -> off the top of the screen,
        # full pressure -> resting deep
        hook_top = HOOK_OFF_TOP + (HOOK_DEEP - HOOK_OFF_TOP) * norm
        hook_left = hook_cx - hook_w // 2
        bite_x = hook_cx
        bite_y = hook_top + HOOK_H * 0.66      # where the fish grabs the barb

        # spring-based reel marker: a damped spring pulls it toward live pressure.
        # Runs in every state so the spring is already live before a fish arrives.
        if raw1 <= cal_min:
            # fully yanked off -> snap the marker straight to 0, ignore momentum
            reel_pos, reel_vel = 0.0, 0.0
        else:
            accel = (tun['SPRING_K'] * (norm - reel_pos)
                     - tun['SPRING_C'] * reel_vel) / tun['SPRING_M']
            reel_vel += accel * dt
            reel_pos += reel_vel * dt
            if reel_pos < 0.0:
                reel_pos, reel_vel = 0.0, 0.0
            elif reel_pos > 1.0:
                reel_pos, reel_vel = 1.0, 0.0
        # all minigame zone logic follows the marker, not the raw pressure
        mz = zone_of(reel_pos)
        marker_attached = (mz != 'detached')

        # -------- state machine --------
        if state == 'casting':
            if marker_attached:
                state = 'biting'
                bite_elapsed = 0.0
        elif state == 'biting':
            if not marker_attached:
                state = 'casting'           # let go entirely -> lost it
            elif mz == 'resting':           # hold steady in the blue to hook it
                bite_elapsed += dt
                if bite_elapsed >= BITE_TIME:
                    state = 'reeling'
                    meter = tun['METER_START']
                    reel_elapsed = 0.0
                    target_center = target_center_hi   # start up in the blue
                    target_dest = target_center_hi
                    retarget_timer = random.uniform(tun['RETARGET_MIN'], tun['RETARGET_MAX'])
                    # the spring marker carries over seamlessly (no reset)
            # attached but not in the blue: the bite timer just pauses
        elif state == 'reeling':
            reel_elapsed += dt
            # live target-band bounds (band width is tunable in real time)
            tw = tun['TARGET_WIDTH']
            tc_lo = TARGET_FLOOR + tw / 2
            tc_hi = TARGET_CAP - tw / 2
            # the band jumps to a fresh random spot on a short random timer,
            # then chases it -> erratic, hard to track
            retarget_timer -= dt
            if retarget_timer <= 0:
                target_dest = random.uniform(tc_lo, tc_hi)
                retarget_timer = random.uniform(tun['RETARGET_MIN'], tun['RETARGET_MAX'])
            target_center = min(tc_hi, max(tc_lo, target_center))
            # ease toward the destination: fast at first, decelerating as it nears
            k = 1.0 - math.exp(-tun['TARGET_SPEED'] * 4.0 * dt)
            target_center += (target_dest - target_center) * k
            target_lo = target_center - tw / 2
            target_hi = target_center + tw / 2
            if reel_pos < DETACH_T:
                # marker slipped into the red mid-reel -> the fish escapes
                state = 'casting'
            else:
                if not (target_lo <= reel_pos <= target_hi):
                    meter = max(0.0, meter - tun['DROP_RATE'] * dt)
                else:
                    meter = min(METER_MAX, meter + tun['FILL_RATE'] * dt)
                if meter <= 0:
                    state = 'casting'       # fish got away -> look for another
                elif meter >= METER_MAX:
                    state = 'landing'
        elif state == 'landing':
            if reel_pos < DETACH_T:         # yank the marker up into the red to land!
                score += 1
                state = 'caught'
                caught_timer = CATCH_FLASH
                meter = 0.0                 # empty the meter so the gold flash clears
                spawn_firework(fireworks, bite_x, bite_y)
                spawn_firework(fireworks, bite_x, bite_y)
        elif state == 'caught':
            caught_timer -= dt
            if caught_timer <= 0:
                state = 'casting'

        # bubbles drift up regardless of state
        for b in bubbles:
            b.update(dt)
        bubbles.sort(key=lambda b: b.depth)   # draw far behind near
        update_fireworks(fireworks, dt)

        # ============== draw ==============
        # ocean background
        screen.blit(bg_img, bg_off)
        # parallax bubbles (far -> near)
        for b in bubbles:
            b.draw(screen, t)

        # HUD
        screen.blit(fonts['big'].render(f"{score} fish caught", True, C_TEXT), (24, 18))

        # black string straight down the screen center, to the hook's depth
        pygame.draw.line(screen, C_STRING, (string_cx, 0), (string_cx, int(hook_top + 6)), 3)
        # the hook sprite, offset left of the string, riding up/down with pressure
        screen.blit(hook_img, (int(hook_left), int(hook_top)))

        # the fish stays off-screen for the first second, then swims in from
        # the left and reaches the hook (center) as the nibble timer runs out
        if state == 'biting':
            APPEAR = 1.0   # seconds spent fully off the left edge
            OFF_X = -170   # x where the 4x fish is fully off-screen
            if bite_elapsed < APPEAR:
                swim = 0.0
            else:
                swim = (bite_elapsed - APPEAR) / max(0.001, BITE_TIME - APPEAR)
            base_x = OFF_X + (hook_cx - OFF_X) * swim
            # the approaching fish ignores player pressure: it swims at the
            # full-pressure (resting) depth as if pressure were 100%
            rest_bite_y = HOOK_DEEP + HOOK_H * 0.66
            wx = base_x + (1.0 - swim) * 30 * math.sin(t * 1.6)
            wy = rest_bite_y + (1.0 - swim) * 70 + 20 * math.sin(t * 1.3 + 1.7)
            draw_fish(screen, wx, wy, t, (150, 170, 185), 0.0, scale=4.0)
        # once hooked, the fish has eaten the hook and rides up/down with it
        if state in ('reeling', 'landing', 'caught'):
            tug = 1.0 if state in ('reeling', 'landing') else 0.0
            fcol = C_GOLD if state == 'caught' else (180, 188, 196)
            # offset left by a quarter of the fish's body width so the mouth sits on the hook
            fish_off = int(48 * 4.0 / 4)
            draw_fish(screen, bite_x - fish_off, bite_y, t, fcol, tug, scale=4.0)
        draw_fireworks(screen, fireworks)

        # right-side combined pressure meter (its outline fills with catch progress)
        # the indicator always shows the spring marker, not raw pressure
        meter_target = (target_lo, target_hi) if target_lo is not None else None
        draw_pressure_meter(screen, PRESSURE, reel_pos, meter_target, state, t, fonts, meter)

        # state prompt, centered low on the screen
        prompt, pcol = "", C_TEXT
        if state == 'casting':
            prompt, pcol = "Drop the line  -  attach the magnets to cast", C_BLUE
        elif state == 'biting':
            if mz == 'resting':
                n = max(1, math.ceil(BITE_TIME - bite_elapsed))
                prompt, pcol = f"Something's nibbling... hold steady!   {n}", C_GOLD
            else:
                prompt, pcol = "Press into the blue to hook it!", C_BLUE
        elif state == 'reeling':
            # adapt the prompt to where the marker sits relative to the target band
            if target_lo is not None and reel_pos < target_lo:
                prompt, pcol = "More pressure - drive the marker down to the target!", C_AMBER
            elif target_hi is not None and reel_pos > target_hi:
                prompt, pcol = "Less pressure - let the marker rise to the target!", C_AMBER
            else:
                prompt, pcol = "On target - keep it steady and reel it in!", C_GREEN
        elif state == 'landing':
            prompt, pcol = "It's hooked!  YANK to land it!", C_GOLD
        elif state == 'caught':
            prompt, pcol = "Nice catch!  +1", C_GREEN
        ps = fonts['mid'].render(prompt, True, pcol)
        screen.blit(ps, (WIDTH // 2 - ps.get_width() // 2, HEIGHT - 72))

        # live reel-tuning panel
        if tun_visible:
            draw_tuning_panel(screen, fonts, tun, sel_idx)

        # flash + controls footer (centered low)
        if flash_timer > 0:
            fs = fonts['small'].render(flash_msg, True, C_GREEN)
            screen.blit(fs, (WIDTH // 2 - fs.get_width() // 2, HEIGHT - 100))
        ctrl = "R reset   O set min   P set max   TAB tune   ESC quit"
        if mouse_mode:
            ctrl = "[MOUSE MODE]  move mouse up/down = force    " + ctrl
        cs = fonts['tiny'].render(ctrl, True, (210, 220, 230))
        screen.blit(cs, (WIDTH // 2 - cs.get_width() // 2, HEIGHT - 30))

        pygame.display.flip()

    if ser:
        ser.close()
    pygame.quit()


if __name__ == '__main__':
    main()
