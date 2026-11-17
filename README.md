# llama-ce (Llama Community Edition)

Reproducible patch-queue and container-backed binary builder for `llama.cpp` + community patches and trusted upstream PRs.

- Tracks `ggml-org/llama.cpp` `origin/master`
- Applies curated downstream patches (`tandem.cpp`, `topppg/llama.cpp@sparse-decode`)
- Auto-stacks open, `READY`, CI-passing PRs from `MEMBER`, `OWNER`, `COLLABORATOR`, and `CONTRIBUTOR` authors, cleanly skipping conflicts
- Compiles inside a container (`podman` or `docker`) and exports host-runnable binaries to `dist/<profile>-<backend>/`

## Quickstart

```bash
# 1. Fetch upstream cache, patches, and trusted PRs
./llama_ce.py fetch

# 2. Create a clean worktree (tests/ pruned) and apply the llama-ce recipe
./llama_ce.py apply

# 3. Compile in a container and export binaries to dist/llama-ce-<backend>/
./llama_ce.py build --backend cuda    # or rocm | vulkan | cpu

# 4. Run exported binaries directly on the host
./dist/llama-ce-cuda/llama-server --help
```

## CLI Commands

- `./llama_ce.py fetch [--profile <name>] [--workers 4] [--force]` — Clones/updates `.cache/llama.cpp`, downloads configured patches, and scans open PRs via GitHub GraphQL (or REST fallback).
- `./llama_ce.py apply [PR_NUM ...] [--profile llama-ce] [--base-commit <ref>]` — Creates `work/<profile>` with `tests/` pruned, applies static patches (`on_conflict = "stop"`), stacks auto-PRs (`on_conflict = "skip"`), and optionally applies explicit PR numbers (e.g. `./llama_ce.py apply 32390`).
- `./llama_ce.py prs [--limit 200] [--all]` — Scans open PRs (`--limit 0` for all open PRs) and lists non-trusted `READY` + CI-green PRs that can be applied manually by number.
- `./llama_ce.py save-patch <profile> <name>` — Exports staged/working changes from `work/<profile>` to `patches/<name>.patch`.
- `./llama_ce.py build [--profile llama-ce] [--backend cpu|cuda|rocm|vulkan] [--runtime podman|docker] [--cmake-flags "..."]` — Builds `llama`, `llama-cli`, `llama-server`, and `llama-bench` inside `Containerfile` and exports them to `dist/<profile>-<backend>/`. (ROCm defaults to `GPU_TARGETS=gfx1100`; override via `--build-arg` or `Containerfile`.)
- `./llama_ce.py status` — Shows configured profiles and worktree state.

## Writing Custom Recipes (`recipes.toml`)

```toml
[upstream]
repo = "https://github.com/ggml-org/llama.cpp.git"
base_commit = "origin/master"

[profiles.my-recipe]
description = "Custom stack"
patches = ["my-patch", "some-pr"]
auto_prs = true
author_associations = ["MEMBER", "OWNER", "COLLABORATOR", "CONTRIBUTOR"]
max_prs = 0          # 0 = unlimited
on_conflict = "skip" # "skip" or "stop"

[patches.my-patch]
type = "url"
url = "https://example.com/feature.patch"
# override = "patches/my-patch.patch"

[patches.some-pr]
type = "github_pr"
pr = 32390
# repo = "ggml-org/llama.cpp"

[patches.local-only]
type = "local"
path = "patches/local-only.patch"
```

## Conflict Resolution Workflow

When a static patch in `patches` fails to apply against a newer `origin/master`:
1. `./llama_ce.py apply` stops in `work/<profile>` with conflict markers.
2. Resolve conflicts in `work/<profile>`, stage with `git add -A`, and export:
   `./llama_ce.py save-patch llama-ce <patch-name>`
3. Set `override = "patches/<patch-name>.patch"` under `[patches.<patch-name>]` in `recipes.toml`.

## Runtime Notes

- **ROCm / CUDA MoE + MTP (`Qwen3.8-Flash-Next` / `qwen4exp`):** When running `llama-server` with `-md` (MTP draft) and `--moe-cache-mib auto`, pass `LLAMA_GDN_REPLAY=0` and `-fit off` (`-ngl 99 -cmoe --moe-cache-mib auto`) to avoid the `LLAMA_GDN_REPLAY` reserve-graph assert.
- **Host ROCm:** If the target host already has native ROCm libraries installed, remove or exclude the bundled `libamd*`/`libhip*`/`libhsa*`/`libroc*`/`rocblas/` files from `dist/llama-ce-rocm/` so `llama-server` links against the host kernel-matched ROCm stack.
