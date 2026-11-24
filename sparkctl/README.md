# sparkctl

One terminal app for a DGX Spark running **Spark Swapper** and **Sparkia**: the models, the Sparkia stack, Docker, the
services and every log — what you'd otherwise do with `curl`, `docker`, `docker compose`, `systemctl` and `journalctl`.

```
 sparkctl  1 Overview  2 Swapper  3 Sparkia  4 Docker  5 Services  6 Run                    sudo ✓ · spark
● Qwen3.8 Flash Next serving as Qwen3.8-Flash-Next at 256K   ·  27 of 121 GiB free  ·  disk 1.7T free  ·  v2.3.1
   MODEL                    WINDOW  ENGINE                         TOK/S STATE
○  Qwen3.8 27B              256K    TensorFold v0.6.0 · MLX 4-bit     58 installed
●  Qwen3.8 Flash Next       256K    TensorFold v1.2.7 Zig engine      58 serving
·  Nemotron 3.5 Bolt        256K    TensorFold Zig engine             74 not installed
enter swap to  o options  i install  j job log  c cancel job  m speed check  …                ? keys  q quit
```

## Install

From a release (Ubuntu 22.04+ / Debian 12+; `arm64` for the DGX Spark, `amd64` elsewhere):

```bash
sudo apt install ./sparkctl_1.0.0_arm64.deb
sparkctl
```

It's one static binary (`/usr/bin/sparkctl`) with no dependencies; it drives the `docker`, `docker compose`, `systemctl`
and `journalctl` already on the machine. Build the packages yourself with `bash packaging/build-deb.sh` (Go 1.24+ and
`dpkg-deb`), or `make deb`.

## The screens

| | |
|---|---|
| **1 Overview** | memory, disks, load and the GPU (and what's on it); what the Swapper serves and any job; the Sparkia services and Vision Server; Docker; the state of every service |
| **2 Swapper** | sign in once, then: **swap** (enter), **install** (i — also a model's other build, like Flash Next's 512K/1M one), **options** (o — context window and switches such as Abliterated), the **job log** live (j), **cancel** (c), **speed check** (m), **pictures** on/off (p), **delete** a model's files (d), the Swapper's activity (a), its service log (L), restart it (R), **update** it (u), connection info (w) |
| **3 Sparkia** | the Compose stack in `/opt/sparkia`: each service's **log** (enter) or all of them (A), **restart/stop/start/recreate** (r s R), **up -d** (U), **pull** (P), **down** (D), **update now** and follow the updater (u, H), **pin the release** (t — `SPARKIA_TAG`), **firewall status** (f) and **live traffic** (F), a **shell** in a container (e) |
| **4 Docker** | containers with CPU and memory (c), images (i), volumes (v), disk usage (d): logs, start/stop, restart, kill, remove, shell, inspect, pull, prune — with a warning on containers that Sparkia or the Swapper manage |
| **5 Services** | `spark-swapper`, `docker`, `sparkia-update` (timer and service), `spark-firewall`, `nginx`, `qwen-image`, `openclaw-gateway`…: start, stop, restart, enable/disable at boot, clear a failure, status, the journal; `n` shows any other unit |
| **6 Run** | saved commands and any command line (`:` anywhere), run in your terminal; `H` is the history of what sparkctl did, with what each command printed |

Everywhere: `/` filters a list, `r` refreshes, `!` opens a shell, `S` signs in to sudo, `?` lists the keys, `q` quits.
Log views follow live (`f` pauses), search (`/`, `n`/`N`), show only matching lines (`F`), wrap (`w`) and save to
`~/sparkctl-logs` (`s`).

## How it stays safe and robust

- **Asks first.** Anything that can't be undone (remove, prune, down, delete a model, stop a service others depend on)
  opens a confirmation that names the exact command; deleting a model or pruning every unused image wants the name typed.
- **Root through sudo, only when needed.** Reading needs no root. A root action runs `sudo -n`; when sudo needs a
  password, sparkctl hands the terminal to `sudo -v` once (you type the password to sudo, never to sparkctl). Run it as
  root (`sudo sparkctl`) and user services are reached in the right user's manager.
- **Never stalls.** Every command and request has a deadline, runs in the background, and a cancelled one stops what it
  started (its whole process group). A screen that can't reach something says why and keeps working for the rest.
- **The Swapper's certificate is pinned.** It's self-signed, so sparkctl trusts the first one it sees (its fingerprint
  in `~/.config/sparkctl/state.json`, mode 0600, with your session) and refuses a different one until you press `T`.

## For scripts

```bash
sparkctl status            # one look; --json for the full picture
sparkctl models
sparkctl swap tfflash      # or part of a title: sparkctl swap "flash next"
sparkctl login admin
sparkctl logs sparkia:vision    # swapper, updater, sparkia[:SERVICE], docker:NAME, unit:NAME
sparkctl doctor            # exit status 1 when something's wrong
```

## Settings

`/etc/sparkctl/config.json` (a conffile: your edits survive upgrades), then `~/.config/sparkctl/config.json` over it:

```json
{
  "swapper_url": "https://127.0.0.1:8999",
  "sparkia_dir": "/opt/sparkia",
  "vision_url": "https://127.0.0.1:8443/",
  "refresh_seconds": 3,
  "units": [{ "name": "ollama.service", "label": "Ollama" }],
  "commands": [{ "name": "Restart OpenClaw", "cmd": "systemctl --user restart openclaw-gateway" }]
}
```

A list you set replaces the default one; `sparkctl default-config` prints the defaults to start from.

## Development

```bash
go test -race ./...                         # parsers, the Swapper client (TLS, pinning, sign-in), the screens driven by keys
sudo bash test/e2e/run.sh dist/sparkctl_1.0.0_amd64.deb   # the installed TUI in a pseudo-terminal: demo Swapper,
                                                          # a test Compose stack, real Docker (needs python3-pyte)
SWAPPER_DEMO=1 SWAPPER_DEMO_HTTP=1 SWAPPER_PORT=18999 python3 ../swapper/app.py &   # a demo Swapper (admin/admin)
echo '{"swapper_url":"https://127.0.0.1:18999"}' > ~/.config/sparkctl/config.json && go run .
```
