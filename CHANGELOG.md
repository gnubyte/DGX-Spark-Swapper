# Changelog

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
- **Connection codes for DSH.** DSH for Mac and Windows connect with one pasted code:
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
