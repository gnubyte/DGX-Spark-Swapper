# Spark Swapper

A calm, multi-user web control panel for the NVIDIA DGX Spark: switch between its
Qwen3.8 LLM servers with one click, install the pieces that are missing, and grab
connection info — all over HTTPS with a self-signed certificate.

Built with Python (standard library only) + a small vanilla-JS frontend. No pip,
no npm on the host.

![Setup](docs/screenshots/00-setup.png)
![Dashboard](docs/screenshots/02-dashboard.png)
![Provisioning](docs/screenshots/04-provisioning.png)
![Users](docs/screenshots/05-users.png)
![Credentials](docs/screenshots/03-credentials.png)

---

## What it does

**Models tab**

- Shows which model is serving right now (or that the box is idle/loading), with a
  live memory meter and health dots for the nginx TLS front and the OpenClaw gateway.
- **Switch to this** on a model card: stops the running model, waits for the GPU and
  unified memory to free up, starts the other one, checks it answers a test prompt,
  and repoints OpenClaw at it (`agents.defaults.model.primary`, image, compaction,
  and memory-flush models, via `openclaw config patch`, then restarts the gateway).
  If the new model fails to load, the previous one is brought back automatically.
- **Keys & connection** reveals the OpenClaw gateway token (with a sign-in link to
  the dashboard) and the model API key, read live from the box.

**Provisioning tab** — auto-detects what's already present and installs what's not:

| Button | Detects / installs |
|---|---|
| git | the `git` binary (apt/apk) |
| Docker + NVIDIA GPU passthrough | docker daemon + `nvidia-container-toolkit` |
| Qwen3.8 27B (SGLang) recipe | [`MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark`](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark) |
| Qwen3.8 Flash Next (vLLM) recipe | [`MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark`](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark) |
| OpenClaw | Node.js + the `openclaw` npm package |
| Self-signed SSL | the cert for this page + the nginx `:11443` TLS front |

Each button streams its install log in-page, like a model swap.

**Users tab** (admin only) — add users, reset passwords, remove accounts. Any user
can sign in to view the running model, swap it, and grab connection info; only
admins can manage users.

---

## Install (one click)

On the Spark:

```bash
curl -fsSL https://raw.githubusercontent.com/gnubyte/DGX-Spark-Swapper/main/install-swapper.sh | bash
```

That script installs git if missing, clones this repo, and runs the host installer
(`deploy/install.sh`), which:

- copies the app to `/opt/spark-swapper`,
- renders `swapper/models.json` (a public template with `{{USER}}` / `{{PUBLIC_HOST}}`
  / `{{OPENCLAW_BIN}}` placeholders) into `/etc/spark-swapper/models.json` using real
  values (existing config wins),
- generates a 10-year self-signed cert in `/etc/spark-swapper` if none exists,
- installs and starts `spark-swapper.service` (runs as root — docker and the model
  recipes need it) and opens `8999/tcp` in ufw if active.

Then open **https://\<spark-ip\>:8999** and create the admin login (one-time).

> To update later: `git -C ~/spark-swapper pull && sudo bash ~/spark-swapper/deploy/install.sh`

---

## Docker (pulled from GitHub Artifacts)

A container image is built on every push and stored as a GitHub Actions artifact.
To pull and run it:

```bash
export GH_TOKEN=github_pat_...   # fine-grained PAT with Actions read on this repo
bash pull-docker.sh             # finds the latest successful `docker` run, loads, and starts
```

`pull-docker.sh` downloads the artifact tarball, `docker load`s it as
`gnubyte/dgx-spark-swapper:latest`, and runs it on the host network with
`/var/run/docker.sock` and `/etc/spark-swapper` mounted (so it can drive the model
containers and keep its cert/state). You can also build it yourself:

```bash
docker build -t dgx-spark-swapper .
docker run -d --name spark-swapper --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /etc/spark-swapper:/config -v /var/lib/spark-swapper:/state \
  dgx-spark-swapper
```

---

## Trusting the self-signed SSL (AWS / EC2, macOS, Windows, curl)

This page (and the `:11443` model front, if you set it up) uses a self-signed
certificate. Browsers and AWS tooling reject it until you add it to a trust store.
Download it from the UI (**Provisioning** tab → *Download the certificate*, or
`https://<host>:8999/cert.crt`), then:

1. **macOS** — open the `.crt`, drag it into **Keychain Access → System**. Select it,
   right-click → *Show Info* → *Trust* → set **"When using this certificate"** to
   **Always Trust**. Lock/unlock the System keychain.
2. **Windows** — Certificate Viewer → *Import* → place it in the
   **Trusted Root Certification Authorities** store (per-machine, or per-user).
3. **Linux / CLI** —
   ```bash
   sudo cp spark-swapper.crt /usr/local/share/ca-certificates/spark-swapper.crt
   sudo update-ca-certificates
   curl --cacert spark-swapper.crt https://<host>:8999/   # no -k needed
   ```
4. **AWS EC2 / Lightsail** (trust from an EC2 host or your AMI) — copy the cert to
   `/usr/local/share/ca-certificates/spark-swapper.crt` (bake it into the AMI, or run
   on each instance), then `update-ca-certificates`. For **ALB / CloudFront / API
   Gateway** you must use a *CA-signed* cert (Let's Encrypt / ACM) — a self-signed
   cert can't be attached there; trust it on the client side instead (steps 1–3).
5. **AWS SDK / boto3** (Python, from any host) —
   ```python
   import boto3, os
   os.environ["AWS_CA_BUNDLE"] = os.path.expanduser("~/spark-swapper.crt")
   s3 = boto3.client("s3")   # or pass verify=... to requests/urllib
   ```

These same steps are in the UI under the **Provisioning** tab.

---

## Security

- Passwords are hashed with scrypt (PBKDF2 fallback on libressl builds).
- Sessions are HMAC-signed cookies (`HttpOnly; Secure; SameSite=Strict`, 30 days).
  Changing a password signs out that user's other browsers (per-user epoch).
- Multi-user: first visitor creates the **admin** login; admins add others via the
  Users tab. Non-admins get 403 on admin routes.
- Every state change is a JSON POST that must carry an `X-Swapper` header and a
  matching `Origin`; five wrong passwords from one address locks it out on a
  doubling interval (30 s → 1 h).
- Strict CSP (no inline scripts), TLS 1.2+ only, `X-Frame-Options: DENY`.
- **The first visitor creates the admin login, so open the page yourself right
  after installing** — anyone else on the LAN could claim it.

---

## Configuration & layout

- `swapper/models.json` — public template (model definitions, OpenClaw paths,
  recipe repos). Placeholder values are filled by the installer; to override on a
  box, copy it to `/etc/spark-swapper/models.json` and edit.
- `swapper/app.py` — the HTTPS server + auth + routing.
- `swapper/engine.py` — the swap engine (stop/start/verify models, repoint
  OpenClaw, boot reconcile).
- `swapper/installer.py` — provisioning: presence detection + install jobs.
- `swapper/web/` — the static frontend (index.html, app.js, app.css).
- `deploy/install.sh`, `deploy/spark-swapper.service` — host install + systemd unit.
- `install-swapper.sh` — the one-click bootstrap (installs git, clones, runs install).
- `pull-docker.sh` — pull the container from GitHub Artifacts and run it.
- `.github/workflows/docker.yml` — builds the image on push and uploads it as an
  artifact.

## Logs

- Host: `journalctl -u spark-swapper -f`
- Docker: `docker logs -f spark-swapper`
- Each Flash launch log is kept under the state dir (`/var/lib/spark-swapper/` or
  the `/state` volume).
