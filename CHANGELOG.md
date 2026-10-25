# Changelog

## 2.2.0

- **Pictures & context** (Models tab). Qwen3.8 Flash Next's recipe can serve **256K** — its native window, with a
  smaller KV pool that leaves ~11 GiB more free, room for Qwen-Image next to it — or **512K** (YaRN, the recipe's own
  pool, no room for pictures). The window is a per-card choice kept in the state dir (`contexts` in `state.json`) and
  put into the card's env at load (`YARN`, `MAX_MODEL_LEN` / `YARN_MAX_MODEL_LEN`, `HOST_RESERVE_GIB`); the site config
  isn't touched. Changing it on the serving model restarts it as a job like a switch — steps, a log, and the old
  window back if the new one won't start; switching to 512K turns pictures off first.
- **Pictures on/off.** A switch for Qwen-Image (ComfyUI as the box user's `qwen-image.service`, started on demand by
  `~/.local/bin/qwen-image`): off writes `~/qwen-image/enabled.json` and stops ComfyUI at once, so its memory goes back
  to the model; on is refused while the model serves 512K. The panel shows each check (installed, switched on, the
  window, memory free against the 22 GiB a picture needs to start, ComfyUI loaded) and the last picture.
- **Activity and logs.** `activity.log` in the state dir records switches, context changes and the pictures switch;
  the panel shows it with the end of `qwen-image.service`'s journal.
- API (sessions and app tokens): `GET /api/pictures` (status, checks, `log`, `activity`), `POST /api/pictures
  {enabled}`, `POST /api/context {key, tokens}` (202 with a job when the model restarts; 200 when it isn't serving —
  saved for its next switch — or already serves that window), `GET /api/activity`. `GET /api/status` carries
  `pictures`, and each Flash Next model its `contexts`.
- `deploy/pictures/` keeps the box's picture setup: the `qwen-image` command, its units and the ComfyUI node that loads
  a GGUF text encoder for Qwen-Image 2.1.

## 2.1.2

- **Add from Hugging Face through an app connection.** An admin's app connection (the connection code Vision Server
  takes) may check a Hugging Face model and add it (`/api/hf/inspect`, `/api/hf/add`), so models can be added from
  Vision's Models page. It adds them with the recipe's own launch settings only: extra launch arguments and
  `trust_remote_code` stay with the admin signed in to this page, and app connections still can't set the Hugging Face
  token, delete models or touch storage.

## 2.1.1

- **Vision Server.** A box can replace OpenClaw with Vision Server (Vision's agent as a service, with real-time voice and
  the memory and skills brought over from OpenClaw). When OpenClaw's gateway unit is disabled — or `"enabled": false` is
  set in the site config's `"openclaw"` section — switching models no longer repoints OpenClaw or restarts its gateway
  (which started it again), and neither does a rollback; Vision Server follows the serving model by itself. The footer
  shows Vision Server's health (`"vision_port"`, default 8443) with a link to it, in place of the gateway's.
- **The site config can't be emptied by an install any more.** `deploy/install.sh` rendered
  `/etc/spark-swapper/models.json` with `sed`, which fails on the slashes in OpenClaw's path *after* the shell has
  truncated the file; the service then exited on every start. It is rendered with Python now, and a config an older
  install kept next to the app is carried over.

## 2.1.0

- **New recipes.** *Nemotron 3.5 Bolt* (NVIDIA's 74B-A9B in 23 GB on TensorFold's Zig engine,
  16 streams, images and video) and *Laguna S 2.1* (Poolside's 118B coding model on vLLM 0.25.1
  with DFlash drafts) join the DGX Spark models, from MiaAI-Lab's recipes. Two Hugging Face
  models can be installed and switched to with one button as well: *Qwen3.8 Flash Next on
  NVIDIA's FP4 checkpoint with vLLM* (the community b12x Spark image, sharing the checkpoint the
  TensorFold recipe already downloads) and *Ornith 1.5 122B* (EXL3 4-bit, ExLlamaV3 + TabbyAPI,
  built on the box — experimental).
- **Qwen3.8 Flash Next (TensorFold) is brought up to date** with its recipe: NVIDIA's FP4
  checkpoint on the new Zig engine (about 49.8 tok/s alone, 198 across 5, prefill ≈2,400 tok/s;
  it needs ~108 GiB free to start and ~160 GB of disk). The memory and disk figures, speeds and
  install notes in the catalog say so.
- **Add from Hugging Face.** Paste a model link on the Models tab (admin): Spark Swapper checks
  the repo, says whether the box can run it and how, downloads it into the shared Hugging Face
  cache (resumable, SHA-256 checked, the token never sent to the CDN) and creates a card.
  Safetensors repos run on vLLM, EXL3 on ExLlamaV3/TabbyAPI (DGX Spark), GGUF on llama.cpp
  (Strix Halo). A Hugging Face token for gated models goes on the Settings tab.
- **Storage tab.** Disk usage with the models' share, what each model takes (weights, engine
  image, recipe, caches), what deleting it really frees (weights and images that two models share
  are counted once and kept for the one that remains), and a list of leftovers nothing accounts
  for — old checkpoints, unused images, docker's build cache. Cards show their size on disk and
  the footer a disk meter.
- **Delete models.** Delete… on a model card or the Storage tab removes its container, weights,
  images, recipe, caches, measured speed and OpenClaw provider entry, after showing the plan.
  It refuses the model that is serving and removes nothing outside the box user's home, the
  Hugging Face cache and the Swapper's download folder.
- New model kind `docker` (a container made from the catalog entry's `docker_run`), install steps
  `hf_snapshot`, `docker_build` and `mkdirs`, shared `snippets`/`builds`/`profiles` in the
  catalog. The speed check no longer sends `chat_template_kwargs` to servers that reject it.

## 2.0.1

- **Connect Vision.** The Mac and Windows apps that connect with a connection code are
  now called Vision, and the Connect tab, its setup guide and the default name of a new
  app connection say so. Vision was previously called DSH: the section notes this for
  anyone whose app still says *Connect DSH*, app connections made under that name keep
  working, and links to the old `#dsh` section open the new one.
- An update also replaces an HTTPS front written by Vision for Windows's own Spark guide
  (as it already did for fronts that app wrote under its old name), so the front checks
  the API key.

## 2.0.0

- **AMD Strix Halo support.** Spark Swapper now runs on a Ryzen AI Max+ 395 box with
  128 GB as well as on a DGX Spark, and offers the models that fit each one. On a
  Strix Halo it installs Mesa's Vulkan driver, Unsloth's llama.cpp build (checksum-
  verified, with MTP drafts), and can raise the GPU memory limit; models run as
  systemd units with OpenClaw, the HTTPS front and model switching working as on
  the Spark.
- **New models.** Qwen3.8 27B and Qwen3.8 Flash Next on TensorFold, and Nemotron 3.5
  Lightning on a DGX Spark; Qwen3.8 27B (4-bit and 8-bit) and Qwen3.8 Flash Next on a
  Strix Halo. Every model card shows its context window and published speed, plus
  the speed measured on your box after each switch. Models install from their card.
- **Automatic updates.** Every Sunday the Swapper checks GitHub for a new release and
  installs it (checksum-verified, with automatic rollback if the new version doesn't
  start). Turn it off on the new Settings tab or with `SWAPPER_AUTO_UPDATE=0`.
- **Connect tab.** The API address, key and model with a test button; copy-paste setup
  for curl, Python, JavaScript, Continue, Cline / Roo / Kilo, Aider, OpenCode, Qwen
  Code, Claude Code and OpenClaw; certificate trust steps.
- **Connection codes for Vision.** Vision for Mac and Windows connect with one pasted code:
  API address and key, certificate fingerprint, and an app token for switching models.
  App connections can be revoked.
- **The HTTPS front checks the API key.** Provisioning ▸ HTTPS front now installs and
  configures nginx on :11443 itself, refusing requests without the key — needed for
  model servers that have no key of their own.
- The model catalog ships with the app (`catalog.json`), so updates bring new models
  to existing boxes; local overrides in `/etc/spark-swapper/models.json` still win, and
  boxes set up with 1.x pick up the new model titles instead of showing duplicates.
- Security: fresh installs get their own API key; models without a key of their own
  won't start behind a front that doesn't check it; llama-server runs unprivileged;
  release signatures (`swapper/release-signers`, `deploy/sign-release.sh`) can be
  required for updates; app connections end when their user's password changes;
  provisioning is admin-only; adding a user no longer signs the admin in as them.
