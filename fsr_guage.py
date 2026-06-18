import pygame
import serial
import serial.tools.list_ports
import sys
import math

# --- Config ---
SERIAL_PORT = None
BAUD_RATE = 115200
WIDTH, HEIGHT = 700, 480
FPS = 60

FSR_RAW_MAX = 4095

def find_esp32_port():
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if any(x in p.description.lower() for x in ['usb', 'uart', 'cp210', 'ch340', 'esp']):
            return p.device
    if ports:
        return ports[0].device
    return None

def draw_semicircle_gauge(surface, cx, cy, radius, value, label, color, cal_min, cal_max):
    """
    Draws a semicircular gauge. value is raw FSR reading.
    Inverse: higher raw = emptier gauge.
    Arc goes from 180deg (left) to 0deg (right), bottom half hidden.
    """
    # Normalize and invert
    span = cal_max - cal_min if cal_max != cal_min else 1
    normalized = max(0.0, min(1.0, (value - cal_min) / span))
    display = 1.0 - normalized  # inverse

    # Angles: gauge sweeps from 180° (left) to 0° (right)
    START_DEG = 180
    END_DEG = 0
    sweep = (START_DEG - END_DEG)  # 180 degrees total

    fill_deg = START_DEG - display * sweep  # current fill angle

    # Track arc (background)
    track_color = (45, 45, 45)
    for deg in range(END_DEG, START_DEG + 1):
        rad = math.radians(deg)
        x1 = cx + (radius - 12) * math.cos(rad)
        y1 = cy - (radius - 12) * math.sin(rad)
        x2 = cx + radius * math.cos(rad)
        y2 = cy - radius * math.sin(rad)
        pygame.draw.line(surface, track_color, (int(x1), int(y1)), (int(x2), int(y2)), 2)

    # Fill arc
    fill_color = color
    fill_start = int(fill_deg)
    fill_end = START_DEG
    if fill_start < fill_end:
        for deg in range(fill_start, fill_end + 1):
            rad = math.radians(deg)
            x1 = cx + (radius - 12) * math.cos(rad)
            y1 = cy - (radius - 12) * math.sin(rad)
            x2 = cx + radius * math.cos(rad)
            y2 = cy - radius * math.sin(rad)
            pygame.draw.line(surface, fill_color, (int(x1), int(y1)), (int(x2), int(y2)), 3)

    # Needle
    needle_rad = math.radians(fill_deg)
    nx = cx + (radius - 20) * math.cos(needle_rad)
    ny = cy - (radius - 20) * math.sin(needle_rad)
    pygame.draw.line(surface, (255, 255, 255), (cx, cy), (int(nx), int(ny)), 3)
    pygame.draw.circle(surface, (255, 255, 255), (cx, cy), 6)

    # Center value text
    font_val = pygame.font.SysFont('monospace', 22, bold=True)
    pct = int(display * 100)
    val_surf = font_val.render(f'{pct}%', True, color)
    surface.blit(val_surf, (cx - val_surf.get_width() // 2, cy - 38))

    font_raw = pygame.font.SysFont('monospace', 13)
    raw_surf = font_raw.render(f'raw: {value}', True, (150, 150, 150))
    surface.blit(raw_surf, (cx - raw_surf.get_width() // 2, cy - 14))

    # Label below
    font_label = pygame.font.SysFont('monospace', 14, bold=True)
    lbl_surf = font_label.render(label, True, (200, 200, 200))
    surface.blit(lbl_surf, (cx - lbl_surf.get_width() // 2, cy + 16))

    # Cal range
    font_cal = pygame.font.SysFont('monospace', 11)
    cal_surf = font_cal.render(f'min:{cal_min}  max:{cal_max}', True, (90, 90, 90))
    surface.blit(cal_surf, (cx - cal_surf.get_width() // 2, cy + 34))

    # End labels
    font_end = pygame.font.SysFont('monospace', 11)
    # Left end = 0% (full, low force)
    left_rad = math.radians(180)
    lx = cx + (radius + 14) * math.cos(left_rad)
    ly = cy - (radius + 14) * math.sin(left_rad)
    surface.blit(font_end.render('0%', True, (100,100,100)), (int(lx) - 10, int(ly) - 6))
    # Right end = 100%
    right_rad = math.radians(0)
    rx = cx + (radius + 8) * math.cos(right_rad)
    ry = cy - (radius + 8) * math.sin(right_rad)
    surface.blit(font_end.render('100%', True, (100,100,100)), (int(rx), int(ry) - 6))


def main():
    port = SERIAL_PORT or find_esp32_port()
    if not port:
        print("No serial port found.")
        sys.exit(1)

    print(f"Connecting to {port} at {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
    except Exception as e:
        print(f"Could not open serial port: {e}")
        sys.exit(1)

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("FSR Gauge")
    clock = pygame.time.Clock()

    raw1 = 0
    raw2 = 0

    # Independent calibration per sensor
    cal = {
        1: {'min': 0, 'max': FSR_RAW_MAX},
        2: {'min': 0, 'max': FSR_RAW_MAX},
    }

    flash_msg = ''
    flash_timer = 0

    font_title = pygame.font.SysFont('monospace', 18, bold=True)
    font_hint = pygame.font.SysFont('monospace', 12)
    font_flash = pygame.font.SysFont('monospace', 13, bold=True)

    running = True
    while running:
        dt = clock.tick(FPS)
        if flash_timer > 0:
            flash_timer -= dt

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_o:
                    cal[1]['min'] = raw1
                    cal[2]['min'] = raw2
                    flash_msg = f'Min set  →  FSR1: {raw1}  FSR2: {raw2}'
                    flash_timer = 2000
                elif event.key == pygame.K_p:
                    cal[1]['max'] = raw1
                    cal[2]['max'] = raw2
                    flash_msg = f'Max set  →  FSR1: {raw1}  FSR2: {raw2}'
                    flash_timer = 2000

        # Read serial
        try:
            while ser.in_waiting:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                parts = line.split(',')
                if len(parts) == 2:
                    raw1 = int(parts[0])
                    raw2 = int(parts[1])
        except Exception:
            pass

        screen.fill((18, 18, 18))

        title = font_title.render("FSR MONITOR", True, (220, 220, 220))
        screen.blit(title, (WIDTH // 2 - title.get_width() // 2, 18))

        hint = font_hint.render("O = set min    P = set max    ESC = quit", True, (70, 70, 70))
        screen.blit(hint, (WIDTH // 2 - hint.get_width() // 2, 44))

        if flash_timer > 0:
            flash = font_flash.render(flash_msg, True, (100, 221, 120))
            screen.blit(flash, (WIDTH // 2 - flash.get_width() // 2, 66))

        # Draw gauges
        draw_semicircle_gauge(screen, 185, 300, 150, raw1, "FSR 1 (pin 4)",
                              (55, 138, 221), cal[1]['min'], cal[1]['max'])
        draw_semicircle_gauge(screen, 515, 300, 150, raw2, "FSR 2 (pin 15)",
                              (221, 130, 55), cal[2]['min'], cal[2]['max'])

        pygame.display.flip()

    ser.close()
    pygame.quit()

if __name__ == '__main__':
    main()