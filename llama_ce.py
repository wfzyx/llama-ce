#!/usr/bin/env python3
"""llama-ce: Reproducible patch-queue & container builder for llama.cpp."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
import urllib.error
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RECIPES_PATH = ROOT / "recipes.toml"
CACHE_DIR = ROOT / ".cache"
UPSTREAM_DIR = CACHE_DIR / "llama.cpp"
PATCH_CACHE_DIR = CACHE_DIR / "patches"
PR_MANIFEST_DIR = CACHE_DIR / "prs"
LOCAL_PATCHES_DIR = ROOT / "patches"
WORK_DIR = ROOT / "work"
DIST_DIR = ROOT / "dist"
PASSING_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd, text=True, check=check)


def load_recipes() -> dict[str, Any]:
    with RECIPES_PATH.open("rb") as f:
        return tomllib.load(f)


def resolve_patch_url(name: str, spec: dict[str, Any]) -> str | None:
    ptype = spec.get("type")
    if ptype == "url":
        return str(spec["url"])
    if ptype == "github_pr":
        pr = spec["pr"]
        repo = spec.get("repo", "ggml-org/llama.cpp")
        return f"https://github.com/{repo}/pull/{pr}.patch"
    if ptype == "local":
        return None
    raise SystemExit(f"Unknown patch type for '{name}': {ptype}")


def get_patch_path(name: str, spec: dict[str, Any]) -> Path:
    if "override" in spec:
        return ROOT / str(spec["override"])
    if spec.get("type") == "local":
        return ROOT / str(spec["path"])
    return PATCH_CACHE_DIR / f"{name}.patch"


def github_repo_slug(repo_url: str) -> str:
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    if not m:
        raise SystemExit(f"Cannot parse GitHub repo slug from: {repo_url}")
    return m.group(1)


def download_patch(url: str, dest: Path, extra_headers: dict[str, str] | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] {dest.stem} <- {url}", flush=True)
    headers = {"User-Agent": "llama-ce/0.1"}
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(4):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
                shutil.copyfileobj(resp, out)
            return
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise


def download_pr_patch(repo_slug: str, pr_num: int, dest: Path, token: str | None = None) -> None:
    tok = token if token is not None else _github_token()
    if tok:
        download_patch(
            f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}",
            dest,
            extra_headers={"Accept": "application/vnd.github.v3.patch", "Authorization": f"Bearer {tok}"},
        )
    else:
        download_patch(f"https://github.com/{repo_slug}/pull/{pr_num}.patch", dest)


def checks_passed(rollup: list[dict[str, Any]] | None) -> bool:
    if not rollup:
        return False
    for item in rollup:
        # CheckRun uses status/conclusion; StatusContext uses state
        if "state" in item and "status" not in item:
            if item.get("state") != "SUCCESS":
                return False
            continue
        status = item.get("status")
        conclusion = item.get("conclusion")
        if status and status != "COMPLETED":
            return False
        if conclusion not in PASSING_CONCLUSIONS:
            return False
    return True


def _github_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token and shutil.which("gh"):
        res = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            token = res.stdout.strip()
    return token


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": "llama-ce/0.1", "Accept": "application/vnd.github+json"}
    tok = token if token is not None else _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def fetch_pr_candidates_graphql(repo_slug: str, token: str, limit: int = 0) -> list[dict[str, Any]]:
    owner, name = repo_slug.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $first: Int!, $after: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(states: OPEN, first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number
            title
            isDraft
            authorAssociation
            author { login }
            commits(last: 1) {
              nodes {
                commit {
                  statusCheckRollup {
                    contexts(first: 100) {
                      nodes {
                        __typename
                        ... on CheckRun { status conclusion }
                        ... on StatusContext { state }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    headers = {
        "User-Agent": "llama-ce/0.1",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    candidates: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        batch = 50 if limit <= 0 else min(50, limit - len(candidates))
        if batch <= 0:
            break
        payload = json.dumps(
            {"query": query, "variables": {"owner": owner, "name": name, "first": batch, "after": after}}
        ).encode("utf-8")
        for attempt in range(4):
            req = urllib.request.Request("https://api.github.com/graphql", data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
        if "errors" in data:
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        pr_conn = data["data"]["repository"]["pullRequests"]
        for node in pr_conn["nodes"]:
            commit_nodes = (node.get("commits") or {}).get("nodes") or []
            rollup = None
            if commit_nodes:
                scr = (commit_nodes[0].get("commit") or {}).get("statusCheckRollup")
                if scr:
                    rollup = (scr.get("contexts") or {}).get("nodes") or []
            candidates.append(
                {
                    "number": node["number"],
                    "title": node["title"],
                    "isDraft": bool(node.get("isDraft")),
                    "authorAssociation": node.get("authorAssociation", "NONE"),
                    "mergeable": "UNKNOWN",
                    "author": node.get("author") or {"login": "ghost"},
                    "statusCheckRollup": rollup,
                }
            )
        page_info = pr_conn["pageInfo"]
        if not page_info["hasNextPage"] or (limit > 0 and len(candidates) >= limit):
            break
        after = page_info["endCursor"]
    return candidates


def fetch_pr_candidates_rest(repo_slug: str, associations: set[str] | None, limit: int = 0, include_drafts: bool = False) -> list[dict[str, Any]]:
    headers = _github_headers()
    candidates: list[dict[str, Any]] = []
    page = 1
    seen = 0
    while True:
        per_page = 100 if limit <= 0 else min(100, limit - seen)
        if per_page <= 0:
            break
        url = f"https://api.github.com/repos/{repo_slug}/pulls?state=open&per_page={per_page}&page={page}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            pulls = json.loads(resp.read().decode("utf-8"))
        if not pulls:
            break
        for pr in pulls:
            seen += 1
            is_draft = bool(pr.get("draft"))
            assoc = pr.get("author_association", "NONE")
            if is_draft and not include_drafts:
                continue
            if associations is not None and assoc not in associations:
                continue
            if is_draft:
                runs: list[dict[str, Any]] = []
            else:
                sha = pr["head"]["sha"]
                chk_url = f"https://api.github.com/repos/{repo_slug}/commits/{sha}/check-runs"
                chk_req = urllib.request.Request(chk_url, headers=headers)
                with urllib.request.urlopen(chk_req, timeout=30) as chk_resp:
                    runs = json.loads(chk_resp.read().decode("utf-8")).get("check_runs", [])
            candidates.append(
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "isDraft": is_draft,
                    "authorAssociation": assoc,
                    "mergeable": "UNKNOWN",
                    "author": {"login": pr["user"]["login"]},
                    "statusCheckRollup": [{"status": r["status"].upper(), "conclusion": (r.get("conclusion") or "").upper()} for r in runs],
                }
            )
            if limit > 0 and seen >= limit:
                break
        if len(pulls) < per_page or (limit > 0 and seen >= limit):
            break
        page += 1
    return candidates


def fetch_pr_candidates(repo_slug: str, associations: set[str] | None = None, limit: int = 0, include_drafts: bool = False) -> list[dict[str, Any]]:
    token = _github_token()
    if token:
        return fetch_pr_candidates_graphql(repo_slug, token, limit=limit)
    return fetch_pr_candidates_rest(repo_slug, associations, limit=limit, include_drafts=include_drafts)


def fetch_trusted_prs(repo_slug: str, profile_name: str, associations: set[str], max_prs: int, workers: int = 4) -> None:
    raw = fetch_pr_candidates(repo_slug, associations=associations, limit=0 if max_prs <= 0 else 200)

    selected: list[dict[str, Any]] = []
    for pr in raw:
        if pr.get("isDraft"):
            continue
        if pr.get("authorAssociation") not in associations:
            continue
        if pr.get("mergeable") == "CONFLICTING":
            continue
        if not checks_passed(pr.get("statusCheckRollup")):
            continue
        selected.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "author": (pr.get("author") or {}).get("login", "unknown"),
                "authorAssociation": pr["authorAssociation"],
            }
        )
        if max_prs > 0 and len(selected) >= max_prs:
            break

    PR_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = PR_MANIFEST_DIR / f"{profile_name}.json"
    manifest_path.write_text(json.dumps(selected, indent=2), encoding="utf-8")
    print(f"[prs] {profile_name}: selected {len(selected)} READY + CI-green PR(s)")

    tok = _github_token()
    failed: set[int] = set()

    def _fetch_one(pr: dict[str, Any]) -> None:
        num = int(pr["number"])
        try:
            download_pr_patch(repo_slug, num, PATCH_CACHE_DIR / f"pr-{num}.patch", token=tok)
        except Exception as exc:
            print(f"[warn] pr-{num}: failed to download patch ({exc}), skipping", file=sys.stderr, flush=True)
            failed.add(num)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(_fetch_one, selected))

    if failed:
        selected = [pr for pr in selected if int(pr["number"]) not in failed]
        manifest_path.write_text(json.dumps(selected, indent=2), encoding="utf-8")


def cmd_fetch(args: argparse.Namespace) -> None:
    recipes = load_recipes()
    upstream = recipes["upstream"]
    repo_url = upstream["repo"]
    repo_slug = github_repo_slug(repo_url)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PATCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not (UPSTREAM_DIR / ".git").exists():
        run(["git", "clone", "--filter=blob:none", repo_url, str(UPSTREAM_DIR)])
    else:
        run(["git", "fetch", "--all", "--prune"], cwd=UPSTREAM_DIR)

    patches: dict[str, Any] = recipes.get("patches", {})
    for name, spec in patches.items():
        if "override" in spec and not args.force:
            print(f"[skip] {name}: using local override {spec['override']}")
            continue
        url = resolve_patch_url(name, spec)
        if url is None:
            continue
        download_patch(url, PATCH_CACHE_DIR / f"{name}.patch")

    profiles: dict[str, Any] = recipes.get("profiles", {})
    target_profiles = [args.profile] if args.profile else list(profiles.keys())
    for pname in target_profiles:
        prof = profiles.get(pname, {})
        if prof.get("auto_prs"):
            assocs = set(prof.get("author_associations", ["MEMBER", "OWNER", "COLLABORATOR", "CONTRIBUTOR"]))
            max_prs = int(prof.get("max_prs", 20))
            fetch_trusted_prs(repo_slug, pname, assocs, max_prs, workers=args.workers)


def ensure_worktree(profile: str, base_commit: str) -> Path:
    if not (UPSTREAM_DIR / ".git").exists():
        raise SystemExit("Upstream cache missing. Run `./llama_ce.py fetch` first.")
    wt = WORK_DIR / profile
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=UPSTREAM_DIR, check=False)
        if wt.exists():
            shutil.rmtree(wt)
    run(["git", "worktree", "add", "--detach", str(wt), base_commit], cwd=UPSTREAM_DIR)
    if (wt / "tests").exists():
        run(["git", "rm", "-rf", "--ignore-unmatch", "-q", "tests"], cwd=wt)
        run(
            ["git", "-c", "user.name=llama-ce", "-c", "user.email=llama-ce@local", "commit", "-q", "-m", "llama-ce: prune tests"],
            cwd=wt,
        )
    return wt


def apply_patch_file(wt: Path, label: str, patch_file: Path, profile_name: str, on_conflict: str) -> bool:
    if not patch_file.exists():
        raise SystemExit(f"Patch file missing: {patch_file} (run `fetch` first)")

    print(f"\n[apply] {label} ({patch_file})")
    res = run(["git", "apply", "--3way", "--exclude=tests/*", str(patch_file)], cwd=wt, check=False)
    if res.returncode != 0:
        if on_conflict == "skip":
            print(f"[skip-conflict] {label}: 3-way rebase conflicted; resetting worktree cleanly.")
            run(["git", "reset", "--hard", "HEAD"], cwd=wt)
            run(["git", "clean", "-fd"], cwd=wt)
            return False
        print(
            f"\n[CONFLICT] '{label}' failed to apply cleanly in {wt}.\n"
            f"Resolve conflicts in {wt}, stage with `git add -A`, then export with:\n"
            f"  ./llama_ce.py save-patch {profile_name} {label}",
            file=sys.stderr,
        )
        sys.exit(res.returncode)

    run(["git", "add", "-A"], cwd=wt)
    diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt, check=False)
    if diff_check.returncode == 0:
        print(f"[skip-empty] {label}: already upstream or no changes.")
        return True
    run(
        ["git", "-c", "user.name=llama-ce", "-c", "user.email=llama-ce@local", "commit", "-m", f"llama-ce: apply {label}"],
        cwd=wt,
    )
    return True


def cmd_apply(args: argparse.Namespace) -> None:
    recipes = load_recipes()
    profile_name: str = args.profile
    profiles: dict[str, Any] = recipes.get("profiles", {})
    if profile_name not in profiles:
        raise SystemExit(f"Unknown profile '{profile_name}'. Available: {', '.join(profiles)}")

    prof = profiles[profile_name]
    repo_slug = github_repo_slug(recipes["upstream"]["repo"])
    base_commit = args.base_commit or recipes["upstream"]["base_commit"]
    wt = ensure_worktree(profile_name, base_commit)

    patch_specs: dict[str, Any] = recipes.get("patches", {})
    patch_names: list[str] = prof.get("patches", [])

    for name in patch_names:
        if name not in patch_specs:
            raise SystemExit(f"Profile '{profile_name}' references undefined patch '{name}'")
        patch_file = get_patch_path(name, patch_specs[name])
        apply_patch_file(wt, name, patch_file, profile_name, on_conflict="stop")

    if prof.get("auto_prs"):
        manifest_path = PR_MANIFEST_DIR / f"{profile_name}.json"
        if not manifest_path.exists():
            raise SystemExit(f"PR manifest missing: {manifest_path} (run `./llama_ce.py fetch --profile {profile_name}` first)")
        prs = json.loads(manifest_path.read_text(encoding="utf-8"))
        on_conflict = prof.get("on_conflict", "skip")
        applied_prs: list[int] = []
        skipped_prs: list[int] = []
        for pr in prs:
            num = pr["number"]
            label = f"pr-{num}"
            ok = apply_patch_file(wt, f"PR #{num} ({pr['authorAssociation']}, {pr['author']})", PATCH_CACHE_DIR / f"{label}.patch", profile_name, on_conflict)
            (applied_prs if ok else skipped_prs).append(num)
        print(f"\n[prs-summary] applied={applied_prs} skipped={skipped_prs}")

    for pr_num in args.prs:
        dest = PATCH_CACHE_DIR / f"pr-{pr_num}.patch"
        if not dest.exists():
            download_patch(f"https://github.com/{repo_slug}/pull/{pr_num}.patch", dest)
        apply_patch_file(wt, f"pr-{pr_num}", dest, profile_name, on_conflict="stop")

    print(f"\n[ok] Profile '{profile_name}' ready at {wt} (base={base_commit})")


def cmd_save_patch(args: argparse.Namespace) -> None:
    wt = WORK_DIR / args.profile
    if not wt.exists():
        raise SystemExit(f"Worktree not found: {wt}")
    LOCAL_PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    out_file = LOCAL_PATCHES_DIR / f"{args.name}.patch"
    res = subprocess.run(["git", "diff", "--cached"], cwd=wt, text=True, capture_output=True, check=True)
    if not res.stdout.strip():
        res = subprocess.run(["git", "diff"], cwd=wt, text=True, capture_output=True, check=True)
    if not res.stdout.strip():
        raise SystemExit("No changes to save in worktree.")
    out_file.write_text(res.stdout, encoding="utf-8")
    print(f"[saved] {out_file} ({len(res.stdout.splitlines())} lines)")


def detect_container_runtime(preferred: str | None) -> str:
    if preferred:
        return preferred
    for tool in ("podman", "docker"):
        if shutil.which(tool):
            return tool
    raise SystemExit("Neither podman nor docker found in PATH.")


def cmd_build(args: argparse.Namespace) -> None:
    wt = WORK_DIR / args.profile
    if not wt.exists():
        raise SystemExit(f"Worktree missing for '{args.profile}'. Run `./llama_ce.py apply --profile {args.profile}` first.")
    runtime = detect_container_runtime(args.runtime)
    out_dir = DIST_DIR / f"{args.profile}-{args.backend}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        runtime,
        "build",
        "-f",
        str(ROOT / "Containerfile"),
        "--target",
        "export",
        "--output",
        f"type=local,dest={out_dir}",
        "--build-arg",
        f"BACKEND={args.backend}",
    ]
    if args.cmake_flags:
        cmd.extend(["--build-arg", f"CMAKE_EXTRA_FLAGS={args.cmake_flags}"])
    cmd.append(str(wt))
    run(cmd)
    print(f"\n[ok] Exported binaries to: {out_dir}")

def cmd_status(_: argparse.Namespace) -> None:
    recipes = load_recipes()
    print(f"Upstream: {recipes['upstream']['repo']} @ {recipes['upstream']['base_commit']}")
    print("Profiles:")
    for name, prof in recipes.get("profiles", {}).items():
        wt_state = "ready" if (WORK_DIR / name).exists() else "not applied"
        print(f"  - {name} [{wt_state}]: patches={prof.get('patches', [])}")
def cmd_prs(args: argparse.Namespace) -> None:
    recipes = load_recipes()
    repo_slug = github_repo_slug(recipes["upstream"]["repo"])
    trusted_assocs = {"MEMBER", "OWNER", "COLLABORATOR", "CONTRIBUTOR"}
    raw = fetch_pr_candidates(repo_slug, associations=None, limit=args.limit, include_drafts=True)

    trusted: list[dict[str, Any]] = []
    non_trusted: list[dict[str, Any]] = []
    drafts = 0
    ci_not_green = 0

    for pr in raw:
        if pr.get("isDraft"):
            drafts += 1
            continue
        if not checks_passed(pr.get("statusCheckRollup")):
            ci_not_green += 1
            continue
        if pr.get("authorAssociation") in trusted_assocs:
            trusted.append(pr)
        else:
            non_trusted.append(pr)

    print(
        f"Scanned {len(raw)} open PR(s) in {repo_slug}: "
        f"trusted={len(trusted)}, non_trusted_ready_green={len(non_trusted)}, "
        f"draft={drafts}, ci_not_green={ci_not_green}"
    )
    if non_trusted:
        print("\nNon-trusted READY + CI-green PRs (apply manually with `./llama_ce.py apply <num>`):")
        for pr in non_trusted:
            print(f"  #{pr['number']} [{pr['authorAssociation']}] @{pr['author']['login']}: {pr['title']}")
    if args.all and trusted:
        print("\nTrusted READY + CI-green PRs:")
        for pr in trusted:
            print(f"  #{pr['number']} [{pr['authorAssociation']}] @{pr['author']['login']}: {pr['title']}")
def main() -> None:
    parser = argparse.ArgumentParser(prog="llama_ce.py", description="llama.cpp community patch-queue & container builder")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Clone/update upstream, patches, and trusted PRs")
    p_fetch.add_argument("--force", action="store_true", help="Re-download even if local override exists")
    p_fetch.add_argument("--profile", default=None, help="Only fetch auto-PRs for this profile")
    p_fetch.add_argument("--workers", type=int, default=4, help="Concurrent patch download workers (default: 4)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_apply = sub.add_parser("apply", help="Create clean worktree, apply profile patches, and optional explicit PR numbers")
    p_apply.add_argument("prs", nargs="*", type=int, help="Explicit PR numbers to apply on top (e.g. 32390)")
    p_apply.add_argument("--profile", default="llama-ce", help="Profile name from recipes.toml (default: llama-ce)")
    p_apply.add_argument("--base-commit", default=None, help="Override upstream base commit")
    p_apply.set_defaults(func=cmd_apply)

    p_save = sub.add_parser("save-patch", help="Save resolved worktree diff to patches/<name>.patch")
    p_save.add_argument("profile", help="Profile worktree name")
    p_save.add_argument("name", help="Patch name")
    p_save.set_defaults(func=cmd_save_patch)

    p_build = sub.add_parser("build", help="Build container image for an applied profile")
    p_build.add_argument("--profile", default="llama-ce", help="Profile name (default: llama-ce)")
    p_build.add_argument("--backend", default="cpu", choices=["cpu", "cuda", "vulkan", "rocm"], help="Compute backend")
    p_build.add_argument("--runtime", default=None, choices=["podman", "docker"], help="Container runtime")
    p_build.add_argument("--cmake-flags", default="", help="Extra CMake flags")
    p_build.set_defaults(func=cmd_build)

    p_status = sub.add_parser("status", help="Show configured profiles and worktree state")
    p_status.set_defaults(func=cmd_status)

    p_prs = sub.add_parser("prs", help="Inspect open PRs and list non-trusted READY + CI-green candidates")
    p_prs.add_argument("--limit", type=int, default=200, help="Max open PRs to scan (0 = all open PRs)")
    p_prs.add_argument("--all", action="store_true", help="Also list trusted PRs")
    p_prs.set_defaults(func=cmd_prs)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
