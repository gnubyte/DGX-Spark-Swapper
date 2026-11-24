"""Drive the installed sparkctl TUI through a pseudo-terminal and a terminal emulator (pyte), checking each screen."""
import os, pty, select, struct, sys, termios, fcntl, time
import pyte

ROWS, COLS = 40, 150
ESC = chr(27)
CR = chr(13)
scr = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(scr)

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execvp("sparkctl", ["sparkctl"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
os.kill(pid, 28)  # SIGWINCH

alive = True
def pump(secs):
    global alive
    end = time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                alive = False
                return
            if not data:
                alive = False
                return
            stream.feed(data)

def text():
    return "\n".join(scr.display)

def send(keys, wait=1.2):
    os.write(fd, keys.encode() if isinstance(keys, str) else keys)
    pump(wait)

shots = []
fails = []
def expect(label, *words, absent=()):
    s = text()
    missing = [w for w in words if w not in s]
    present = [w for w in absent if w in s]
    ok = not missing and not present
    print(f"[{'ok ' if ok else 'FAIL'}] {label}" + (f"  missing {missing}" if missing else "") + (f"  unexpected {present}" if present else ""), flush=True)
    shots.append((label, s))
    if not ok:
        fails.append(label)
        print("\n".join(l.rstrip() for l in s.splitlines()), flush=True)

def wait_for(label, word, secs=15, absent=()):
    end = time.time() + secs
    while time.time() < end and word not in text():
        pump(0.3)
    expect(label, word, absent=absent)

pump(5)
wait_for("overview", "This machine")
expect("overview panels", "Spark Swapper", "Docker", "Sparkia", "Services")
send("2", 1)
wait_for("swapper signed out", "Sign in to Spark Swapper")
send("\r", 0.8)
expect("user dialog", "Your Swapper login")
send("\x15admin\r", 0.8)
expect("password prompt", "Password for admin")
send("admin", 0.3)
expect("password hidden", "•••••", absent=("admin admin",))
send("\r", 0.5)
wait_for("swapper signed in", "serving as")
expect("models", "Qwen3.8 Flash Next", "Nemotron 3.5 Bolt", "MODEL")
send(ESC + "[B", 0.5)
send("o", 1)
expect("options dialog", "Window 512K", "Window 1M", "Abliterated", "options")
send(ESC + "[B" + ESC + "[B", 0.5)
expect("512K needs its build", "Needs the long-context build", "first (press i)")
send("\x1b", 0.6)
send("u", 3)
expect("update check", "Install Spark Swapper")
send("n", 0.6)
send("3", 1)
wait_for("sparkia stack", "sparkia-e2e-vision")
expect("sparkia header", "following latest", "v0.9.140")
send("\r", 1)
wait_for("sparkia service log", "hello from e2e")
send("/", 0.3)
send("e2e\r", 0.8)
expect("log search", "search “e2e”")
send("q", 0.8)
expect("back to sparkia", "SERVICE")
send("t", 0.8)
expect("pin dialog", "Pin the Sparkia release")
send("\x15" + "0.9.142\r", 3)
send("4", 1)
wait_for("docker containers", "sparkia-e2e-vision")
expect("docker cols", "NAME", "IMAGE", "STATE")
send("i", 1)
send("/", 0.3)
send("busybox" + CR, 1)
wait_for("docker images", "busybox")
send(ESC, 0.5)
send("d", 1)
wait_for("docker disk", "Images", secs=40)
send("c", 1)
wait_for("containers again", "sparkia-e2e-vision")
send("/", 0.3)
send("e2e", 0.5)
send("\r", 0.8)
expect("docker filter", "sparkia-e2e-vision")
send("\x1b", 0.5)
send("5", 2)
expect("services tab", "Services")
send("6", 1)
expect("run tab", "saved commands", "Disk usage")
send("H", 1)
expect("history", "Pin Sparkia to 0.9.142", "Sign in as admin")
send("?", 1)
expect("help", "Everywhere", "sign in to sudo")
send("\x1b", 0.6)
send("1", 3)
expect("overview after", "This machine")
send("q", 3)
pump(2)
print("still running after q:", alive and os.waitpid(pid, os.WNOHANG) == (0, 0), flush=True)
out = os.environ.get("E2E_OUT", "/tmp/sparkctl-e2e")
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "screens.txt"), "w") as f:
    for label, s in shots:
        f.write(f"===== {label}\n" + "\n".join(l.rstrip() for l in s.splitlines()) + "\n\n")
print("RESULT:", "PASS" if not fails else f"FAIL {fails}", flush=True)
sys.exit(1 if fails else 0)
