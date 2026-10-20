"""Regression for #115: reviewer-agent skills must not tell the model to ``git clone`` for PR review.

The codex, opencode, grok and blackbox skills used to carry a ``mktemp -d && git clone <url> &&
gh pr checkout`` recipe for isolated review. Run against a large repository, every review
materialised a full new clone. The replacement is a bounded recipe against an *existing* local
object store: fetch the PR head/base into unique temporary refs, pin both OIDs, review from a
unique detached linked worktree, and remove exactly that worktree and those refs afterwards.

Static checks pin the guidance (no clone in any code block, no shared conventional refs, stop and
ask an operator when there is no local object store). The execution checks run the documented
script, with a stub reviewer on ``PATH``, against throwaway local repositories and assert the
worktree, the pinned OIDs, the cleanup and that pre-existing refs/worktrees survive. No network and
no clone is involved.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# skill file -> (reviewer executable the recipe invokes)
REVIEW_SKILLS = {
    "skills/autonomous-ai-agents/codex/SKILL.md": "codex",
    "skills/autonomous-ai-agents/opencode/SKILL.md": "opencode",
    "optional-skills/autonomous-ai-agents/grok/SKILL.md": "grok",
    "optional-skills/autonomous-ai-agents/blackbox/SKILL.md": "blackbox",
}

_FENCE_RE = re.compile(r"^```(\w*)[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_OPERATOR_STOP = "provision a bounded review source"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _code_blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _FENCE_RE.finditer(text)]


def _review_script(rel: str) -> str:
    scripts = [body for lang, body in _code_blocks(_read(rel)) if lang == "bash" and "worktree add" in body]
    assert len(scripts) == 1, f"{rel}: expected exactly one bash review recipe, found {len(scripts)}"
    return scripts[0]


def _all_agent_skills() -> list[str]:
    roots = ("skills/autonomous-ai-agents", "optional-skills/autonomous-ai-agents")
    return sorted(
        p.relative_to(REPO_ROOT).as_posix() for root in roots for p in (REPO_ROOT / root).rglob("*.md")
    )


@pytest.mark.parametrize("rel", _all_agent_skills())
def test_agent_skills_have_no_clone_in_code_blocks(rel):
    """No coding-agent skill hands the model a ``git clone`` command to run."""
    offenders = [
        line.strip()
        for lang, body in _code_blocks(_read(rel))
        for line in body.splitlines()
        if re.search(r"\bgit\s+clone\b|\bgh\s+repo\s+clone\b", line)
    ]
    assert not offenders, f"{rel} has clone commands in code blocks: {offenders}"


@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_former_clone_review_example_is_gone(rel):
    text = _read(rel)
    assert not re.search(r"mktemp -d\)?\s*&&\s*git clone", text)
    assert "gh pr checkout" not in text
    assert "origin/main" not in " ".join(
        line for lang, body in _code_blocks(text) if "worktree add" in body for line in body.splitlines()
    ), "review recipe must diff against the pinned base OID, not a moving origin/main"


@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_prose_forbids_clone_and_stops_for_an_operator(rel):
    prose = " ".join(_FENCE_RE.sub(" ", _read(rel)).split())
    assert re.search(r"[Dd]o not use a full clone|never fall back to a full clone", prose)
    assert _OPERATOR_STOP in prose
    assert "unique" in prose and "detached" in prose and "OID" in prose


@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_recipe_uses_unique_refs_pinned_oids_and_explicit_cleanup(rel):
    script = _review_script(rel)
    assert _OPERATOR_STOP in script, "recipe must stop and ask when there is no local object store"
    assert not re.search(r"\bgit\s+clone\b|\bgh\s+pr\s+checkout\b", script)

    # Unique temporary refs in a private namespace; nothing fetched into conventional refs.
    assert 'WT=$(mktemp -d "${TMPDIR:?' in script
    assert "HEAD_REF=refs/hermes-review/$ID/head" in script
    assert "BASE_REF=refs/hermes-review/$ID/base" in script
    fetch = script[script.index("fetch "):script.index("# Record each fetched ref")]
    assert '"refs/pull/$PR/head:$HEAD_REF"' in fetch
    assert '"refs/heads/$BASE_BRANCH:$BASE_REF"' in fetch
    assert "--atomic" in fetch, "fetch must not leave partial ref updates on failure"
    assert "--no-tags" in fetch and "--no-write-fetch-head" in fetch
    assert "+refs" not in fetch and '+"' not in fetch, "no forced refspecs"
    # Explain the forbidden namespaces in prose if useful, but never fetch into them.
    assert not re.search(r'(?<![\w/])refs/remotes/|refs/heads/pr', fetch)

    # OIDs are pinned, the worktree is detached at the pinned head, review is against the base OID.
    # The raw ref OIDs are recorded *before* commit peeling so cleanup can always
    # compare-and-delete the refs this run created, even when validation fails.
    head_assign = 'HEAD_REF_OID=$(git -C "$REPO" rev-parse --verify "$HEAD_REF")'
    base_assign = 'BASE_REF_OID=$(git -C "$REPO" rev-parse --verify "$BASE_REF")'
    head_check = 'HEAD_OID=$(git -C "$REPO" rev-parse --verify --quiet "$HEAD_REF_OID^{commit}")'
    base_check = 'BASE_OID=$(git -C "$REPO" rev-parse --verify --quiet "$BASE_REF_OID^{commit}")'
    for needle in (head_assign, base_assign, head_check, base_check):
        assert needle in script, needle
    assert script.index(head_assign) < script.index(head_check)
    assert script.index(base_assign) < script.index(base_check)
    assert script.index(base_check) < script.index("worktree add")
    assert '"$HEAD_REF^{commit}"' not in script and '"$BASE_REF^{commit}"' not in script
    assert 'git -C "$REPO" worktree add --detach "$WT" "$HEAD_OID"' in script
    assert "$BASE_OID" in script.split('worktree add --detach "$WT" "$HEAD_OID"', 1)[1]

    # Cleanup is trap-driven and scoped to this run's own worktree and refs.
    assert "trap cleanup EXIT" in script
    assert 'git -C "$REPO" worktree remove "$WT"' in script
    assert "worktree remove --force" not in script, "dirty worktrees must be preserved, never force-removed"
    assert "preserved" in script
    assert "show-ref --verify --quiet \"$HEAD_REF\"" in script
    # Cleanup compare-and-deletes the *raw* ref OIDs recorded right after fetch (not the peeled
    # commit OIDs), so it removes exactly the refs this run created even if commit peeling failed.
    assert 'update-ref -d "$HEAD_REF" "$HEAD_REF_OID"' in script
    assert 'update-ref -d "$BASE_REF" "$BASE_REF_OID"' in script
    assert 'update-ref -d "$HEAD_REF" "$HEAD_OID"' not in script
    assert 'update-ref -d "$BASE_REF" "$BASE_OID"' not in script
    for destructive in ("rm -rf", "git clean", "git reset", "git gc", "prune", "branch -D", "--force-with"):
        assert destructive not in script, destructive


# --- execution ---------------------------------------------------------------------------------

_posix = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="recipe is a bash script that needs git",
)


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_env(tmp_path):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "TMPDIR": str(tmp_path / "scratch"),
    }
    (tmp_path / "scratch").mkdir()
    version = subprocess.run(["git", "--version"], capture_output=True, text=True, env=env, check=False).stdout
    major_minor = tuple(int(p) for p in re.search(r"(\d+)\.(\d+)", version).groups())
    if major_minor < (2, 29):
        pytest.skip("--no-write-fetch-head needs git >= 2.29")
    return env


@pytest.fixture
def scenario(tmp_path, git_env):
    """An 'origin' with main (base) and refs/pull/42/head, plus a separate local checkout."""
    work, origin, local = tmp_path / "work", tmp_path / "origin.git", tmp_path / "local"
    for d in (work, local):
        d.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin), env=git_env)
    _git(work, "init", "-b", "main", env=git_env)
    (work / "a.txt").write_text("base\n")
    (work / "file with spaces.txt").write_text("base spaced\n")
    _git(work, "add", "a.txt", "file with spaces.txt", env=git_env)
    _git(work, "commit", "-m", "base", env=git_env)
    base_oid = _git(work, "rev-parse", "HEAD", env=git_env)
    (work / "a.txt").write_text("base\nchange\n")
    (work / "file with spaces.txt").write_text("base spaced\nchange\n")
    _git(work, "commit", "-am", "pr change", env=git_env)
    head_oid = _git(work, "rev-parse", "HEAD", env=git_env)
    _git(work, "push", str(origin), f"{base_oid}:refs/heads/main", f"{head_oid}:refs/pull/42/head", env=git_env)

    # The local checkout is not a clone: init + remote, with unrelated pre-existing state.
    _git(local, "init", "-b", "main", env=git_env)
    _git(local, "remote", "add", "origin", str(origin), env=git_env)
    _git(local, "fetch", "--no-tags", "origin", "main", env=git_env)
    _git(local, "branch", "keep-me", "FETCH_HEAD", env=git_env)
    _git(local, "worktree", "add", "--detach", str(tmp_path / "other-wt"), "FETCH_HEAD", env=git_env)
    return {"local": local, "base": base_oid, "head": head_oid, "env": git_env, "root": tmp_path}


def _stub_reviewers(root: Path, env: dict) -> Path:
    bin_dir, log = root / "bin", root / "reviewer.log"
    bin_dir.mkdir()
    for name in set(REVIEW_SKILLS.values()):
        stub = bin_dir / name
        stub.write_text(
            "#!/bin/sh\n"
            f'{{ echo "tool={name}"; echo "cwd=$(pwd -P)"; echo "head=$(git rev-parse HEAD)";\n'
            '  git symbolic-ref -q HEAD >/dev/null && echo attached || echo detached;\n'
            '  echo "args=$*"; } > "$STUB_LOG"\n'
            'for arg do printf "arg=<%s>\\n" "$arg" >> "$STUB_LOG"; done\n'
            '[ -z "${STUB_DIRTY:-}" ] || echo scratch > reviewer-output.txt\n'
            'exit "${STUB_EXIT:-0}"\n'
        )
        stub.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(log)
    return log


def _run_recipe(
    rel: str, scenario: dict, repo: Path | None = None, extra_env: dict | None = None,
    pr: str = "42", fixed_temp_name: str | None = None
):
    root, env = scenario["root"], dict(scenario["env"])
    log = _stub_reviewers(root, env)
    if fixed_temp_name:
        mktemp = Path(env["PATH"].split(os.pathsep)[0]) / "mktemp"
        mktemp.write_text(
            "#!/bin/sh\n"
            f'path="$TMPDIR/{fixed_temp_name}"\n'
            'mkdir "$path" || exit 1\n'
            'printf "%s\\n" "$path"\n'
        )
        mktemp.chmod(0o755)
    env.update(extra_env or {})
    script = _review_script(rel)
    assert 'REPO="$HOME/project"' in script and "PR=42\n" in script
    script = script.replace('REPO="$HOME/project"', f'REPO="{repo or scenario["local"]}"')
    script = script.replace("PR=42\n", f"PR={pr}\n")
    path = root / "recipe.sh"
    path.write_text(script)
    proc = subprocess.run(["bash", str(path)], cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False)
    return proc, log


def _state(local: Path, env: dict) -> tuple[str, str]:
    refs = _git(local, "for-each-ref", "--format=%(refname) %(objectname)", env=env)
    wts = "\n".join(
        line for line in _git(local, "worktree", "list", "--porcelain", env=env).splitlines()
        if line.startswith("worktree ")
    )
    return refs, wts


@_posix
@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_recipe_reviews_pinned_head_in_detached_worktree_and_cleans_up(rel, scenario):
    local, env = scenario["local"], scenario["env"]
    refs_before, wts_before = _state(local, env)

    proc, log = _run_recipe(rel, scenario)

    assert proc.returncode == 0, proc.stderr
    seen = dict(line.split("=", 1) for line in log.read_text().splitlines() if "=" in line)
    scratch = (scenario["root"] / "scratch").resolve()
    assert seen["tool"] == REVIEW_SKILLS[rel]
    assert Path(seen["cwd"]).parent == scratch and Path(seen["cwd"]).name.startswith("pr-review.")
    assert seen["head"] == scenario["head"]
    assert "detached" in log.read_text()
    assert scenario["base"] in seen["args"], "reviewer must be pointed at the pinned base OID"
    if REVIEW_SKILLS[rel] == "opencode":
        assert "arg=<-f>\narg=<file with spaces.txt>" in log.read_text()

    # Exactly this run's worktree and refs are gone; everything that pre-existed is untouched.
    assert not Path(seen["cwd"]).exists() and not list(scratch.iterdir())
    assert _git(local, "for-each-ref", "refs/hermes-review/", env=env) == ""
    assert _state(local, env) == (refs_before, wts_before)
    assert (scenario["root"] / "other-wt").is_dir()


@_posix
@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_recipe_preserves_a_dirty_worktree_but_still_removes_its_refs(rel, scenario):
    local, env = scenario["local"], scenario["env"]
    refs_before, _ = _state(local, env)

    proc, log = _run_recipe(rel, scenario, extra_env={"STUB_DIRTY": "1"})

    assert proc.returncode == 0, proc.stderr
    kept = Path(dict(l.split("=", 1) for l in log.read_text().splitlines() if "=" in l)["cwd"])
    assert (kept / "reviewer-output.txt").read_text() == "scratch\n", "dirty worktree must survive"
    assert str(kept) in proc.stderr and "preserved" in proc.stderr
    assert _state(local, env)[0] == refs_before
    assert _git(kept, "rev-parse", "HEAD", env=env) == scenario["head"]


@_posix
@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_recipe_cleans_up_when_the_reviewer_fails(rel, scenario):
    local, env = scenario["local"], scenario["env"]
    refs_before, wts_before = _state(local, env)

    proc, _ = _run_recipe(rel, scenario, extra_env={"STUB_EXIT": "3"})

    assert proc.returncode == 3
    assert _state(local, env) == (refs_before, wts_before)
    assert not list((scenario["root"] / "scratch").iterdir())


@_posix
def test_recipe_stops_without_cloning_when_no_local_object_store(scenario):
    proc, log = _run_recipe(
        "skills/autonomous-ai-agents/codex/SKILL.md", scenario, repo=scenario["root"] / "does-not-exist"
    )

    assert proc.returncode == 2
    assert _OPERATOR_STOP in proc.stderr
    assert not log.exists(), "reviewer must not run"
    assert not list((scenario["root"] / "scratch").iterdir()), "no scratch dir or clone created"
    assert not (scenario["root"] / "does-not-exist").exists()


@_posix
def test_recipe_leaves_preexisting_state_when_fetch_fails(scenario):
    local, env = scenario["local"], scenario["env"]
    refs_before, wts_before = _state(local, env)

    proc, log = _run_recipe("skills/autonomous-ai-agents/codex/SKILL.md", scenario, pr="999")

    assert proc.returncode != 0 and not log.exists()
    assert _state(local, env) == (refs_before, wts_before)
    assert not list((scenario["root"] / "scratch").iterdir())


@_posix
@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_recipe_preserves_colliding_preexisting_ref_when_fetch_fails(rel, scenario):
    local, env = scenario["local"], scenario["env"]
    head_ref = "refs/hermes-review/pr-review.collision/head"
    _git(local, "update-ref", head_ref, scenario["base"], env=env)
    refs_before, wts_before = _state(local, env)

    proc, log = _run_recipe(rel, scenario, pr="999", fixed_temp_name="pr-review.collision")

    assert proc.returncode == 2
    assert "temporary review ref already exists" in proc.stderr
    assert not log.exists(), "reviewer must not run after a ref collision"
    assert _state(local, env) == (refs_before, wts_before)
    assert not list((scenario["root"] / "scratch").iterdir())


def _push_pr_ref(scenario: dict, number: int, files: int) -> None:
    """Add refs/pull/<number>/head to the fake origin: base + one commit touching ``files`` files."""
    root, env, base = scenario["root"], scenario["env"], scenario["base"]
    work = root / f"work-{number}"
    work.mkdir()
    _git(work, "init", "-b", "main", env=env)
    _git(work, "fetch", str(root / "origin.git"), base, env=env)
    _git(work, "checkout", "-q", "--detach", "FETCH_HEAD", env=env)
    for i in range(files):
        (work / f"f{i:02d}.txt").write_text(f"{i}\n")
    _git(work, "add", ".", env=env)
    _git(work, "commit", "-m", f"touch {files}", env=env)
    _git(work, "push", str(root / "origin.git"), f"HEAD:refs/pull/{number}/head", env=env)


@_posix
@pytest.mark.parametrize("rel", sorted(REVIEW_SKILLS))
def test_recipe_cleans_refs_when_commit_validation_fails(rel, scenario):
    """A PR ref that resolves to a non-commit must fail loudly and still delete the fetched refs."""
    local, env, root = scenario["local"], scenario["env"], scenario["root"]
    origin = root / "origin.git"
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=origin, env=env, input="not a commit\n",
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(origin, "update-ref", "refs/pull/77/head", blob, env=env)
    refs_before, wts_before = _state(local, env)

    proc, log = _run_recipe(rel, scenario, pr="77")

    assert proc.returncode == 2, proc.stderr
    assert "is not a commit" in proc.stderr and "nothing was reviewed" in proc.stderr
    assert not log.exists(), "reviewer must not run"
    assert _git(local, "for-each-ref", "refs/hermes-review/", env=env) == "", "fetched refs must not leak"
    assert _state(local, env) == (refs_before, wts_before)
    assert not list((root / "scratch").iterdir())


@_posix
def test_opencode_fails_explicitly_instead_of_silently_truncating_large_diffs(scenario):
    rel = "skills/autonomous-ai-agents/opencode/SKILL.md"
    local, env, root = scenario["local"], scenario["env"], scenario["root"]
    _push_pr_ref(scenario, 88, files=21)
    refs_before, wts_before = _state(local, env)

    proc, log = _run_recipe(rel, scenario, pr="88")

    assert proc.returncode == 2, proc.stderr
    assert "more than 20 files" in proc.stderr and "refusing a partial review" in proc.stderr
    assert not log.exists(), "a partial review must never be started"
    assert _state(local, env) == (refs_before, wts_before)
    assert not list((root / "scratch").iterdir())


@_posix
def test_opencode_reviews_every_file_at_the_bound(scenario):
    rel = "skills/autonomous-ai-agents/opencode/SKILL.md"
    _push_pr_ref(scenario, 89, files=20)
    proc, log = _run_recipe(rel, scenario, pr="89")
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().count("arg=<-f>") == 20


@_posix
def test_opencode_max_files_override_raises_the_bound(scenario):
    rel = "skills/autonomous-ai-agents/opencode/SKILL.md"
    _push_pr_ref(scenario, 90, files=21)
    proc, log = _run_recipe(rel, scenario, pr="90", extra_env={"MAX_FILES": "25"})
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().count("arg=<-f>") == 21
