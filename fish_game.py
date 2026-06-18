"""
Magnet Fishing Game
====================
Hardware: FSR sensors over a magnet; user has a chain with an opposite-polarity
magnet. Serial sends a continuous force value 0 (no pressure) .. ~4000 (max).

Force semantics in this game:
  - 0          -> magnet disconnected
  - ~MAX       -> magnet connected, resting (not pulling)  -> armor slowly lowers
  - lower val  -> user pulling chain away -> armor rises (harder pull = faster)

Pull progress is normalized 0..1:
  - <20%   : not enough pull to move (slack)
  - 20-80% : green "safe" pulling zone
  - 80-100%: line straining (fairies struggling) -> warning
  - 100%   : held too long -> snaps/disconnects, armor falls

Connect a real serial port, or run with --sim to drive force with the mouse
(vertical position = force) for testing without hardware.
"""

import sys
import math
import random
import argparse
import pygame

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------
SCREEN_W, SCREEN_H = 1000, 700
FPS = 60

FSR_MAX = 4000.0          # DEFAULT max ceiling; overridden at runtime by calibration
CAL_MIN = 200.0           # don't let a calibrated max be set lower than this (sanity)
CONNECT_THRESHOLD = 0.92  # fraction of calibrated max needed to "lock on" to armor
DISCONNECT_FORCE = 0.05   # below this (of calibrated max) magnet is considered detached

# Pull-progress band thresholds (fraction 0..1)
SLACK_END = 0.20          # below this: no upward movement
SAFE_END = 0.80           # 0.20-0.80 green zone
STRAIN_END = 1.00         # 0.80-1.00 warning zone, then snap

RISE_SPEED = 0.45         # how fast armor rises (units/sec of vertical fraction)
LOWER_SPEED = 0.12        # slow lowering when connected but resting
FALL_SPEED = 1.20         # falling back down when disconnected
SNAP_HOLD_TIME = 0.6      # seconds at 100% strain before line snaps

GAME_TIME = 120.0         # seconds to collect all armor
ARMOR_TO_WIN = 4

BAUD = 115200

# ----------------------------------------------------------------------------
# Colors
# ----------------------------------------------------------------------------
C_SKY_TOP = (135, 180, 220)
C_SKY_BOT = (175, 210, 235)
C_WATER_TOP = (40, 110, 150)
C_WATER_BOT = (12, 45, 75)
C_WATER_LINE = (200, 235, 245)
C_SAND = (120, 100, 70)
C_LINE = (230, 230, 230)
C_TEXT = (245, 245, 245)
C_GREEN = (70, 200, 90)
C_YELLOW = (230, 200, 60)
C_RED = (220, 70, 60)
C_GREY = (90, 100, 110)
C_ARMOR = (170, 180, 195)
C_ARMOR_DK = (110, 120, 135)
C_FAIRY = (255, 230, 120)
C_FAIRY_GLOW = (255, 245, 200)


# ----------------------------------------------------------------------------
# Force input sources
# ----------------------------------------------------------------------------
def find_esp32_port():
    """Best-effort auto-detection of the ESP32's serial port."""
    try:
        import serial.tools.list_ports
    except Exception:
        return None
    ports = serial.tools.list_ports.comports()
    for p in ports:
        desc = (p.description or "").lower()
        if any(x in desc for x in ["usb", "uart", "cp210", "ch340", "esp"]):
            return p.device
    if ports:
        return ports[0].device
    return None


class SerialForce:
    """Reads 'raw1,raw2' lines from the ESP32.

    Uses readline() with a short timeout (same pattern as the working FSR
    monitor) rather than non-blocking read() + manual buffer.
    Channel 0 = raw1 (left spot), channel 1 = raw2 (right spot).
    """
    def __init__(self, ser):
        self.ser = ser          # already-open serial.Serial instance
        self.values = (0.0, 0.0)

    def read(self):
        try:
            while self.ser.in_waiting:
                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                parts = line.split(',')
                if len(parts) == 2:
                    try:
                        raw1 = int(parts[0])
                        raw2 = int(parts[1])
                        self.values = (float(raw1), float(raw2))
                    except ValueError:
                        pass
        except Exception:
            pass
        return self.values


class SimForce:
    """Two-channel simulated force for testing without hardware.

    Mouse X picks which spot the 'magnet' is over (left half -> channel 0,
    right half -> channel 1); mouse Y sets the force on that channel
    (top = 0, bottom = FSR_MAX). The other channel reads 0.
    """
    def read(self):
        mx, my = pygame.mouse.get_pos()
        frac = max(0.0, min(1.0, my / SCREEN_H))
        f = frac * FSR_MAX
        if mx < SCREEN_W / 2:
            return (f, 0.0)
        return (0.0, f)


# ----------------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------------
class Armor:
    KINDS = ["helm", "chest", "gauntlet", "boot"]

    def __init__(self, kind, x, y):
        self.kind = kind
        self.x = x
        self.y = y                  # resting/floating y in the pond
        self.base_y = y
        self.collected = False
        self.phase = random.uniform(0, math.tau)
        self.vx = random.choice([-1, 1]) * random.uniform(45, 90)

    def float_update(self, dt, water_top, water_bot):
        """Idle bobbing/drifting while uncaught."""
        self.phase += dt * 1.5
        self.x += self.vx * dt
        if self.x < 60:
            self.x = 60
            self.vx = abs(self.vx)
        if self.x > SCREEN_W - 60:
            self.x = SCREEN_W - 60
            self.vx = -abs(self.vx)
        self.base_y += math.sin(self.phase) * 0.4
        self.base_y = max(water_top + 10, min(water_bot - 20, self.base_y))
        self.y = self.base_y

    def draw(self, surf, font):
        cx, cy = int(self.x), int(self.y)
        if self.kind == "helm":
            pygame.draw.ellipse(surf, C_ARMOR, (cx-22, cy-20, 44, 36))
            pygame.draw.rect(surf, C_ARMOR_DK, (cx-22, cy-2, 44, 10))
            pygame.draw.rect(surf, (20, 20, 30), (cx-14, cy-4, 8, 12))
            pygame.draw.rect(surf, (20, 20, 30), (cx+6, cy-4, 8, 12))
        elif self.kind == "chest":
            pygame.draw.rect(surf, C_ARMOR, (cx-26, cy-24, 52, 48), border_radius=8)
            pygame.draw.line(surf, C_ARMOR_DK, (cx, cy-22), (cx, cy+22), 3)
            pygame.draw.circle(surf, C_ARMOR_DK, (cx, cy-6), 6)
        elif self.kind == "gauntlet":
            pygame.draw.rect(surf, C_ARMOR, (cx-14, cy-22, 28, 40), border_radius=6)
            for i in range(3):
                pygame.draw.rect(surf, C_ARMOR_DK, (cx-12+i*9, cy+14, 6, 12), border_radius=3)
        else:  # boot
            pygame.draw.rect(surf, C_ARMOR, (cx-12, cy-22, 24, 34), border_radius=5)
            pygame.draw.ellipse(surf, C_ARMOR, (cx-12, cy+6, 38, 18))
        pygame.draw.ellipse(surf, C_ARMOR_DK, (cx-22, cy-26, 44, 14), 2)


class Hotspot:
    """One of the two FSR sensor spots in the pond, guarded by a fairy.

    Each spot is driven by its own FSR channel (`channel` indexes the serial
    pair) and keeps its own calibrated max, since the two sensors may differ.
    """
    def __init__(self, x, y, channel):
        self.x = x
        self.y = y
        self.channel = channel
        self.force = 0.0            # latest reading from this spot's FSR
        self.cal_max = FSR_MAX      # calibrated ceiling for this sensor
        self.lit = False
        self.armor = None       # armor currently resting here (lit) or hooked
        self.glow_phase = random.uniform(0, math.tau)

    def draw(self, surf, dt, font):
        self.glow_phase += dt * 4
        # the sensor pad
        pad_col = C_FAIRY if self.lit else C_GREY
        pygame.draw.circle(surf, pad_col, (int(self.x), int(self.y)), 16)
        pygame.draw.circle(surf, (30, 40, 50), (int(self.x), int(self.y)), 16, 2)
        # fairy hovering above the pad
        fy = self.y - 34 + math.sin(self.glow_phase) * 4
        draw_fairy(surf, self.x, fy, lit=self.lit, phase=self.glow_phase)


def draw_fairy(surf, x, y, lit, phase, strain=0.0):
    """A small glowing fairy. strain 0..1 makes it flash red (struggling)."""
    x, y = int(x), int(y)
    glow = 120 + int(80 * (0.5 + 0.5 * math.sin(phase)))
    if lit or strain > 0:
        # glow halo
        halo = pygame.Surface((60, 60), pygame.SRCALPHA)
        col = C_FAIRY_GLOW
        if strain > 0.5:
            col = (255, int(200 * (1 - strain)), int(120 * (1 - strain)))
        pygame.draw.circle(halo, (*col, min(160, glow)), (30, 30), 24)
        surf.blit(halo, (x - 30, y - 30))
    body = C_FAIRY
    if strain > 0.5:
        body = (255, int(120 * (1 - strain)) + 60, 80)
    pygame.draw.circle(surf, body, (x, y), 6)
    # wings
    wing_a = math.sin(phase * 3) * 0.4
    for s in (-1, 1):
        wx = x + s * 8
        pts = [(x, y), (wx, y - 8), (wx + s * 4, y + 2)]
        pygame.draw.polygon(surf, (*C_FAIRY_GLOW, ), pts)
    pygame.draw.line(surf, (255, 255, 230), (x, y), (x, y - 14), 1)


# ----------------------------------------------------------------------------
# Game
# ----------------------------------------------------------------------------
class Game:
    # hook states
    IDLE = "idle"           # nothing hooked; waiting for armor to touch a lit spot
    READY = "ready"         # armor lit on a spot, waiting for full-force connect
    REELING = "reeling"     # hooked; pulling up based on force
    SNAPPED = "snapped"     # over-pulled; armor falling back

    def __init__(self, force_source):
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        pygame.display.set_caption("Fairy Magnet Fishing")
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("arialrounded,arial", 22)
        self.big = pygame.font.SysFont("arialrounded,arial", 56, bold=True)
        self.small = pygame.font.SysFont("arial", 16)

        self.force_source = force_source
        self.water_top = SCREEN_H - 160   # shallow pond — only 120px deep
        self.water_bot = SCREEN_H - 40

        # two FSR hotspots, each bound to a serial channel (raw1 -> 0, raw2 -> 1)
        self.spots = [
            Hotspot(SCREEN_W * 0.33, self.water_top + 50, channel=0),
            Hotspot(SCREEN_W * 0.67, self.water_top + 50, channel=1),
        ]

        # spawn armor pieces in the pond — same shallow band as the hotspots
        kinds = list(Armor.KINDS)
        random.shuffle(kinds)
        self.armors = []
        for i, k in enumerate(kinds):
            x = random.uniform(120, SCREEN_W - 120)
            y = random.uniform(self.water_top + 20, self.water_bot - 20)
            self.armors.append(Armor(k, x, y))

        self.collected = 0
        self.time_left = GAME_TIME

        # per-spot calibration lives on each Hotspot now.
        self.cal_flash = 0.0        # brief "Calibrated!" confirmation timer
        self.cal_flash_spot = None  # which spot was just calibrated (for HUD)

        # reeling state
        self.state = Game.IDLE
        self.active_spot = None
        self.hooked = None
        self.progress = 0.0      # 0 (bottom) .. 1 (top, landed)
        self.pull = 0.0          # current pull fraction 0..1
        self.snap_timer = 0.0
        self.result = None       # "win" / "lose"
        self.bubbles = [self._mk_bubble() for _ in range(30)]

    def _mk_bubble(self):
        return [random.uniform(0, SCREEN_W),
                random.uniform(self.water_top, self.water_bot),
                random.uniform(4, 10),
                random.uniform(1.5, 3.5)]

    # ---- force -> pull mapping -------------------------------------------
    def force_to_pull(self, force, cal_max):
        """Connected & resting = that spot's calibrated max -> pull 0.
        Pulling reduces force -> pull rises toward 1 as force approaches 0."""
        cm = cal_max
        f = max(0.0, min(cm, force))
        # connected band is [DISCONNECT_FORCE*cm .. cm]; map within it.
        lo = DISCONNECT_FORCE * cm
        if f <= lo:
            return 1.0  # essentially detached / pulled all the way
        frac = (cm - f) / (cm - lo)
        return max(0.0, min(1.0, frac))

    # ---- main update ------------------------------------------------------
    def update(self, dt):
        if self.result:
            return

        self.time_left -= dt
        if self.time_left <= 0:
            self.time_left = 0
            self.result = "lose"
            return

        # read both FSR channels and assign each to its spot
        forces = self.force_source.read()
        for spot in self.spots:
            spot.force = forces[spot.channel] if spot.channel < len(forces) else 0.0

        # float the uncaught armor and check hotspot contact
        for a in self.armors:
            if a.collected or a is self.hooked:
                continue
            a.float_update(dt, self.water_top, self.water_bot)

        # ---- IDLE: light up a spot when armor drifts over it -------------
        if self.state == Game.IDLE:
            for spot in self.spots:
                spot.lit = False
                spot.armor = None
            for a in self.armors:
                if a.collected or a is self.hooked:
                    continue
                for spot in self.spots:
                    if spot.armor is None and math.hypot(a.x - spot.x, a.y - spot.y) < 90:
                        spot.lit = True
                        spot.armor = a
            # if a lit spot's own FSR reaches full force, hook that spot
            for spot in self.spots:
                full_force = spot.force >= CONNECT_THRESHOLD * spot.cal_max
                if spot.lit and full_force:
                    self.state = Game.REELING
                    self.active_spot = spot
                    self.hooked = spot.armor
                    self.progress = 0.0
                    self.snap_timer = 0.0
                    break

        # ---- REELING: convert the active spot's force into progress ------
        elif self.state == Game.REELING:
            spot = self.active_spot
            connected = spot.force >= DISCONNECT_FORCE * spot.cal_max
            if not connected:
                # magnet fully detached -> armor falls back
                self.state = Game.SNAPPED
            else:
                self.pull = self.force_to_pull(spot.force, spot.cal_max)
                if self.pull < SLACK_END:
                    # not pulling enough -> slowly lowers
                    self.progress -= LOWER_SPEED * dt
                elif self.pull <= SAFE_END:
                    # green zone, rise scaled within band
                    span = (self.pull - SLACK_END) / (SAFE_END - SLACK_END)
                    self.progress += RISE_SPEED * (0.4 + 0.6 * span) * dt
                    self.snap_timer = 0.0
                elif self.pull < STRAIN_END:
                    # strain zone, still rising but warning
                    self.progress += RISE_SPEED * dt
                    self.snap_timer = 0.0
                else:
                    # at/over 100% -> count down to snap
                    self.progress += RISE_SPEED * dt
                    self.snap_timer += dt
                    if self.snap_timer >= SNAP_HOLD_TIME:
                        self.state = Game.SNAPPED

                self.progress = max(0.0, min(1.0, self.progress))
                if self.progress >= 1.0:
                    # landed it!
                    self.hooked.collected = True
                    self.collected += 1
                    self.active_spot.lit = False
                    self.active_spot.armor = None
                    self._reset_hook()
                    if self.collected >= ARMOR_TO_WIN:
                        self.result = "win"

        # ---- SNAPPED: armor falls back to pond floor ---------------------
        elif self.state == Game.SNAPPED:
            self.progress -= FALL_SPEED * dt
            if self.progress <= 0:
                # return armor to floating, near its spot
                a = self.hooked
                a.base_y = self.water_top + 40
                a.y = a.base_y
                a.x = self.active_spot.x + random.uniform(-30, 30)
                self._reset_hook()

        # bubbles
        for b in self.bubbles:
            b[1] -= b[3]
            if b[1] < self.water_top:
                b[0] = random.uniform(0, SCREEN_W)
                b[1] = self.water_bot

        if self.cal_flash > 0:
            self.cal_flash -= dt

    def _reset_hook(self):
        self.state = Game.IDLE
        self.active_spot = None
        self.hooked = None
        self.progress = 0.0
        self.pull = 0.0
        self.snap_timer = 0.0

    def calibrate(self):
        """Capture the current reading on whichever spot the magnet is resting
        on (the one reading highest) as that spot's new max ceiling.
        Rest the magnet on a pad, then press C; repeat for the other pad."""
        forces = self.force_source.read()
        for spot in self.spots:
            spot.force = forces[spot.channel] if spot.channel < len(forces) else 0.0
        # pick the spot currently under the magnet = highest force
        target = max(self.spots, key=lambda sp: sp.force)
        target.cal_max = max(CAL_MIN, target.force)
        self.cal_flash = 1.5
        self.cal_flash_spot = self.spots.index(target)
        if self.state in (Game.REELING, Game.SNAPPED):
            self._reset_hook()

    # ---- drawing ----------------------------------------------------------
    def draw(self):
        s = self.screen
        # sky gradient
        for y in range(self.water_top):
            t = y / self.water_top
            col = lerp(C_SKY_TOP, C_SKY_BOT, t)
            pygame.draw.line(s, col, (0, y), (SCREEN_W, y))
        # water gradient
        for y in range(self.water_top, SCREEN_H):
            t = (y - self.water_top) / (SCREEN_H - self.water_top)
            col = lerp(C_WATER_TOP, C_WATER_BOT, t)
            pygame.draw.line(s, col, (0, y), (SCREEN_W, y))
        pygame.draw.line(s, C_WATER_LINE, (0, self.water_top), (SCREEN_W, self.water_top), 3)
        pygame.draw.rect(s, C_SAND, (0, SCREEN_H - 40, SCREEN_W, 40))

        # bubbles
        for b in self.bubbles:
            pygame.draw.circle(s, (200, 230, 240, 60), (int(b[0]), int(b[1])), int(b[2]/4)+1, 1)

        dt = self.clock.get_time() / 1000.0

        # hotspots + fairies
        for spot in self.spots:
            spot.draw(s, dt, self.font)

        # floating armor (not hooked)
        for a in self.armors:
            if a.collected or a is self.hooked:
                continue
            a.draw(s, self.font)

        # hooked armor being reeled
        if self.hooked and self.state in (Game.REELING, Game.SNAPPED):
            spot = self.active_spot
            top_y = self.water_top + 30
            bot_y = self.water_bot - 60
            ay = bot_y - (bot_y - top_y) * self.progress
            # the fishing line
            pygame.draw.line(s, C_LINE, (spot.x, self.water_top - 40), (spot.x, ay), 2)
            # fairies clinging to the line near the armor
            strain = 0.0
            if self.state == Game.REELING:
                if self.pull >= SAFE_END:
                    strain = (self.pull - SAFE_END) / (STRAIN_END - SAFE_END)
            for off in (-18, 18):
                draw_fairy(s, spot.x + off, ay - 30, lit=True,
                           phase=spot.glow_phase, strain=strain)
            self.hooked.x = spot.x
            self.hooked.y = ay
            self.hooked.draw(s, self.font)

        self.draw_hud()

        if self.result:
            self.draw_end()

        pygame.display.flip()

    def draw_hud(self):
        s = self.screen
        # timer
        mins = int(self.time_left // 60)
        secs = int(self.time_left % 60)
        tcol = C_RED if self.time_left < 20 else C_TEXT
        ttxt = self.big.render(f"{mins}:{secs:02d}", True, tcol)
        s.blit(ttxt, (SCREEN_W//2 - ttxt.get_width()//2, 12))

        # collected count
        ctxt = self.font.render(f"Armor: {self.collected} / {ARMOR_TO_WIN}", True, C_TEXT)
        s.blit(ctxt, (16, 16))

        # calibration readout: per-spot max + live reading, and reset hint
        s0, s1 = self.spots[0], self.spots[1]
        cal = self.small.render(
            f"L  max:{int(s0.cal_max)} live:{int(s0.force)}    "
            f"R  max:{int(s1.cal_max)} live:{int(s1.force)}    [C] reset max",
            True, C_TEXT)
        s.blit(cal, (16, 46))
        if self.cal_flash > 0:
            side = "L" if self.cal_flash_spot == 0 else "R"
            cm = int(self.spots[self.cal_flash_spot].cal_max)
            ftxt = self.font.render(f"Calibrated {side} spot! Max = {cm}",
                                    True, C_GREEN)
            s.blit(ftxt, (16, 66))

        # pull-progress bar in corner (only meaningful while reeling)
        bx, by, bw, bh = SCREEN_W - 60, 60, 28, 240
        pygame.draw.rect(s, (20, 30, 40), (bx-2, by-2, bw+4, bh+4), border_radius=6)
        # zone bands
        def yb(frac):  # bottom-anchored
            return by + bh - bh * frac
        pygame.draw.rect(s, (50, 60, 70), (bx, yb(SLACK_END), bw, bh*SLACK_END))
        pygame.draw.rect(s, (35, 90, 45), (bx, yb(SAFE_END), bw, bh*(SAFE_END-SLACK_END)))
        pygame.draw.rect(s, (110, 90, 30), (bx, yb(STRAIN_END), bw, bh*(STRAIN_END-SAFE_END)))
        # progress fill (how far armor has risen)
        fill_h = bh * self.progress
        pcol = C_GREEN
        if self.state == Game.SNAPPED:
            pcol = C_RED
        pygame.draw.rect(s, pcol, (bx, by + bh - fill_h, bw, fill_h))
        # current pull indicator line
        if self.state == Game.REELING:
            py = yb(self.pull)
            ind = C_GREEN
            if self.pull < SLACK_END:
                ind = C_GREY
            elif self.pull >= SAFE_END:
                ind = C_YELLOW if self.pull < STRAIN_END else C_RED
            pygame.draw.line(s, ind, (bx-6, py), (bx+bw+6, py), 3)
        lbl = self.small.render("PULL", True, C_TEXT)
        s.blit(lbl, (bx + bw//2 - lbl.get_width()//2, by + bh + 6))

        # state prompt
        prompt = ""
        if self.state == Game.IDLE:
            if any(sp.lit for sp in self.spots):
                prompt = "A fairy lit up! Connect your magnet (press down)."
            else:
                prompt = "Wait for armor to drift onto a glowing spot..."
        elif self.state == Game.REELING:
            if self.pull < SLACK_END:
                prompt = "Pull the chain to reel it up!"
            elif self.pull >= SAFE_END:
                prompt = "Careful! The fairies are straining — ease off!"
            else:
                prompt = "Reeling... keep it steady."
        elif self.state == Game.SNAPPED:
            prompt = "The line slipped! It's sinking back down."
        if prompt:
            ptx = self.font.render(prompt, True, C_TEXT)
            s.blit(ptx, (SCREEN_W//2 - ptx.get_width()//2, SCREEN_H - 34))

    def draw_end(self):
        s = self.screen
        overlay = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 150))
        s.blit(overlay, (0, 0))
        msg = "You collected the full set!" if self.result == "win" else "Time's up!"
        col = C_GREEN if self.result == "win" else C_RED
        t = self.big.render("You Win!" if self.result == "win" else "Game Over", True, col)
        s.blit(t, (SCREEN_W//2 - t.get_width()//2, SCREEN_H//2 - 60))
        m = self.font.render(msg, True, C_TEXT)
        s.blit(m, (SCREEN_W//2 - m.get_width()//2, SCREEN_H//2 + 10))
        r = self.font.render("Press R to play again, Esc to quit", True, C_TEXT)
        s.blit(r, (SCREEN_W//2 - r.get_width()//2, SCREEN_H//2 + 50))

    # ---- loop -------------------------------------------------------------
    def run(self):
        while True:
            self.clock.tick(FPS)
            dt = 1.0 / FPS
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    return
                if e.type == pygame.KEYDOWN:
                    if e.key == pygame.K_ESCAPE:
                        return
                    if e.key == pygame.K_c:
                        self.calibrate()
                    if e.key == pygame.K_r and self.result:
                        self.__init__(self.force_source)
            self.update(dt)
            self.draw()


def lerp(a, b, t):
    return (int(a[0] + (b[0]-a[0])*t),
            int(a[1] + (b[1]-a[1])*t),
            int(a[2] + (b[2]-a[2])*t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true",
                    help="simulate force with the mouse instead of serial "
                         "(left/right half = which spot, Y = force)")
    ap.add_argument("--port", default=None,
                    help="serial port; if omitted, the ESP32 is auto-detected")
    ap.add_argument("--baud", type=int, default=BAUD)
    args = ap.parse_args()

    if args.sim:
        print("Running in SIM mode (mouse drives force).")
        src = SimForce()
    else:
        import serial
        port = args.port or find_esp32_port()
        if port is None:
            print("No serial port found; falling back to SIM mode (mouse).")
            src = SimForce()
        else:
            try:
                ser = serial.Serial(port, args.baud, timeout=0.1)
                print(f"Connected to {port} @ {args.baud} baud.")
                src = SerialForce(ser)
            except Exception as ex:
                print(f"Could not open {port} ({ex}); falling back to SIM mode.")
                src = SimForce()

    Game(src).run()
    pygame.quit()


if __name__ == "__main__":
    main()