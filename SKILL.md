---
name: llama-ce
description: Build, customize, and maintain llama.cpp community patch stacks using the llama-ce toolchain (recipes.toml, llama_ce.py, Containerfile). Use when adding downstream forks/PRs, resolving rebase conflicts onto origin/master, or exporting container-built binaries.
---

# llama-ce Toolchain Skill

## Repository Layout
- `recipes.toml` — Upstream repo/ref (`origin/master`), `llama-ce` and `base` profiles, and patch definitions (`url`, `github_pr`, `local`, `override`).
- `llama_ce.py` — Zero-dependency Python 3.11+ CLI (`fetch`, `apply`, `prs`, `save-patch`, `build`, `status`).
- `Containerfile` — Multi-stage builder (`cpu`, `vulkan`, `cuda`, `rocm`) that compiles `llama`, `llama-cli`, `llama-server`, `llama-bench` with `$ORIGIN` RPATH and exports `/build/bin/` via `FROM scratch AS export`.
- `patches/` — Locally rebased `.patch` overrides (e.g., `tandemcpp.patch`, `sparse-decode.patch`).
- `.cache/` / `work/` / `dist/` — Gitignored cache, worktrees (`tests/` pruned), and exported host binaries.

## Standard Workflow
1. **Update & fetch:** `./llama_ce.py fetch --profile llama-ce --workers 4`
2. **Apply stack:** `./llama_ce.py apply` (or `./llama_ce.py apply 32390` to test an untrusted/explicit PR on top)
3. **Configure sanity check (fast, ~1.5s):**
   `cmake -S work/llama-ce -B /tmp/cfg -G Ninja -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF -DGGML_NATIVE=OFF && rm -rf /tmp/cfg`
4. **Container build & binary export:**
   `./llama_ce.py build --backend cuda` (or `rocm` / `vulkan` / `cpu`) -> outputs to `dist/llama-ce-<backend>/`

## Adding or Rebasing a Downstream Fork Patch
When a downstream fork branch (e.g., `owner/llama.cpp@branch`) is behind `origin/master` and `git apply --3way` fails due to missing base blobs:
1. Add the fork as a temporary remote in `.cache/llama.cpp` and fetch its branch.
2. Find the merge-base: `MB=$(git -C .cache/llama.cpp merge-base origin/master <remote>/<branch>)`.
3. Create a temporary worktree on `<remote>/<branch>`, squash to 1 commit (`git reset --soft "$MB" && git commit -m "<name>"`), and `git rebase` onto `origin/master` (or onto `work/llama-ce` after earlier patches).
4. Resolve conflicts, test with `cmake` + `ninja`, and export `git diff <base> -- ':!tests' > patches/<name>.patch`.
5. Remove the temporary worktree and remote; set `override = "patches/<name>.patch"` in `recipes.toml`.

## Rules & Invariants
- Always target `origin/master` (`base_commit = "origin/master"`).
- Always prune `tests/` in worktrees and pass `--exclude=tests/*` to `git apply` so test churn never causes conflicts.
- Auto-PRs (`auto_prs = true`) only include `isDraft == false`, `authorAssociation` in `MEMBER`/`OWNER`/`COLLABORATOR`/`CONTRIBUTOR`, and all CI checks `SUCCESS`/`NEUTRAL`/`SKIPPED`; conflicts on auto-PRs are isolated and skipped (`on_conflict = "skip"`).
- Untrusted PRs (`NONE` / `FIRST_TIME_CONTRIBUTOR`) are never auto-merged; inspect with `./llama_ce.py prs --limit 0` and opt in explicitly via `./llama_ce.py apply <pr>`.
