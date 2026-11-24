# Install Syncwheel

Use this file when a user gives an AI agent a markdown install prompt and asks it to install or
bootstrap Syncwheel.

Syncwheel is a deterministic Git maintenance tool for PR stacks, integration
branches, pinned deployment-channel branches, and multi-agent worktrees.

## What To Do

1. Inspect the host for Python 3.11+, Git, and `uv`.
2. Install the `syncwheel` CLI if it is missing.
3. Verify the CLI.
4. Install the Syncwheel companion skill through Agentwheel when Agentwheel is available.
5. Run only read-only repo diagnostics unless the user explicitly asks for branch, worktree, push, or
   recovery changes.

## Install The CLI

Check whether Syncwheel is already available:

```bash
syncwheel --version
syncwheel --help
```

Install the production tool with `uv`:

```bash
uv tool install "git+https://github.com/NestDevLab/syncwheel"
```

For Syncwheel development from a local checkout:

```bash
uv tool install --editable .
```

If the repo checkout is available but the CLI is not on `PATH`, use the script directly:

```bash
python3 scripts/syncwheel.py --version
python3 scripts/syncwheel.py --help
```

## Verify A Repository

Run these from the target repository:

```bash
syncwheel repo tracking status
syncwheel reconcile
syncwheel validate
```

`reconcile` is a dry-run diagnostic by default. Do not run `sync`, `publish`,
`reconcile --apply`, branch rebuilds, or push commands unless the user asked for that exact scope.

## Install The Companion Skill

When Agentwheel is available, install the Syncwheel skill into the active runtime. For a project-local
Codex setup:

```bash
agentwheel doctor --adapter codex --local --skill syncwheel --source github:NestDevLab/syncwheel
agentwheel install github:NestDevLab/syncwheel --adapter codex --local --skill syncwheel
```

For Claude user scope:

```bash
agentwheel doctor --adapter claude --user --skill syncwheel --source github:NestDevLab/syncwheel
agentwheel install github:NestDevLab/syncwheel --adapter claude --user --skill syncwheel
```

After installation, run `syncwheel --version` to read the CLI version. In the source
checkout, set `QUALIFIED_REV` to the qualified Git commit and `INSTALLED_SKILL` to the
installed skill directory, then compare the file digests:

```bash
set -o pipefail
syncwheel --version
sha256sum "$INSTALLED_SKILL/SKILL.md"
git show "$QUALIFIED_REV:skills/syncwheel/SKILL.md" | sha256sum
```

Matching digests prove that this skill file matches the file at that revision; they do
not establish package provenance or verify additional installed files. Inspect those
separately through the supported Agentwheel status interface. If source, revision or
installed contents cannot be established, report the installation as unverified. Do not
edit generated runtime copies by hand.

## Success Criteria

- `syncwheel --version` works, or the checkout script fallback works.
- `syncwheel repo tracking status` runs in the target repo.
- The companion skill is installed if the user requested it and Agentwheel is available.
- When the skill is installed, verify its source and revision against the qualified
  source. If either cannot be established, report the installation as unverified;
  this verification criterion remains unmet.
- Any branch, worktree, integration, or push mutation was explicitly requested.
- Every channel create/edit/refresh/promotion/resolution/apply/publish/close was
  explicitly requested and used its preview's exact `planDigest` with
  `--apply`; uncertain outcomes were observed with `channel operation
  reconcile`, never retried.
- Channel publication was not reported as an environment deployment.
