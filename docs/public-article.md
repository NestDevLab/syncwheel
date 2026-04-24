# Syncwheel

## A simple idea for a messy reality

Modern software work often looks clean from the outside: a repository, a `main` branch, a few pull requests, and a team moving forward together.

In practice, many projects are more layered than that, even when they are healthy and well run.

You may be working from a fork. Several pull requests may be open at the same time. Some changes may depend on other changes that are not merged yet. A team may also keep an internal integration branch just to make the whole system runnable while waiting for upstream reviews.

At that point, the normal mental model of “just branch from `main` and open a PR” starts to break down.

That is the context behind **Syncwheel**.

Syncwheel is not a Git feature and not a new branching model. It is a name for a recurring maintenance workflow: the work needed to bring a repository back into a state that is understandable, reviewable, and usable.

## What problem does it solve?

Syncwheel exists for projects where development does not happen in a perfectly linear way, even when that is completely normal and expected.

Typical situations include:
- multiple open PRs that are all still in flight
- a fork that moves faster than the upstream repository
- local or team-level integration branches used to combine unmerged work
- fixes that were made quickly for operational reasons and must later be placed into the right branch

In these situations, the problem is not only technical. It is also cognitive.

People can lose track of what belongs where.
A pull request may still be mergeable, but no longer clean.
An integration branch may still run, but hide coupling between features.
A developer may think they are “up to date” while still missing important commits from other active workstreams.

Syncwheel is the discipline of stopping, recovering the true state of the repository, and putting things back into a clearer shape.

## Where it is especially useful

Syncwheel is especially useful in situations like these:

- **open-source contribution with long review latency**  
  You want to keep opening and refining PRs even though upstream may merge them weeks later, and you still want to run all of them together in your own working fork.

- **product or platform teams working in parallel**  
  Several changes are in review at the same time, but development does not stop while waiting.

- **Agile delivery environments**  
  Teams keep moving across multiple workstreams while reviews, approvals, and integration happen asynchronously.

In all of these cases, the issue is not that the repository is broken. The issue is that the workflow is multi-layered, and the true state becomes harder to see.

## Why is this needed?

Because normal tooling often shows only a small slice of the truth.

For example, when you work directly on `main`, it is usually easy to understand whether you are ahead or behind. But once you have multiple long-lived branches, a fork, and parallel pull requests, that visibility drops sharply.

A branch can look healthy while the wider stack is drifting.
A PR can look acceptable while secretly depending on work from another branch.
A repository can appear stable while its real structure has become confusing.

Syncwheel addresses exactly that kind of drift.

## What does Syncwheel actually do?

At a high level, Syncwheel does four things:

1. **Recovers reality**  
   It inspects the repository as it actually exists, not as people assume it exists.

2. **Separates roles clearly**  
   It distinguishes review branches, integration branches, local patches, and upstream history.

3. **Realigns branches**  
   It rebuilds or repairs pull request branches so they are based on the right place and contain the right changes.

4. **Restores confidence**  
   It validates the result and makes the final state understandable again.

In other words, Syncwheel is not just about rebasing. It is about restoring structure.

## How it is implemented

The implementation is conceptually simple, even if the repository is not.

A Syncwheel run usually follows this order:

1. **Map the real repository state**  
   Inspect remotes, active branches, open pull requests, integration branches, local patches, and any other place where work may be hiding.

2. **Identify the branch roles**  
   Decide which branches are meant for review, which branch acts as the combined integration surface, and which changes are still only local or temporary.

3. **Rebuild clean PR branches from the correct base**  
   If a PR branch has become tangled, rebuild it from the canonical base branch and move only the intended commits into it.

4. **Reconstruct the integration branch intentionally**  
   Instead of trusting whatever history happened to accumulate, replay the branch stack in the order that reflects the real desired combined state.

5. **Validate the result**  
   Run the checks that matter for the project: branch divergence, typecheck, tests, mergeability, and manual sanity checks where needed.

6. **Publish a truthful final picture**  
   The output of Syncwheel is not just updated branches. It is also a clear explanation of what changed, what remains coupled, and what is still blocked.

In practice, this means Syncwheel is part inspection workflow, part branch repair workflow, and part reporting workflow.

## A minimal mental model

A useful way to think about it is this:

- **upstream** is the official line of history
- **PR branches** are clean review surfaces
- **integration** is the branch that reflects the combined working reality
- **Syncwheel** is the process that keeps those layers from collapsing into each other

That separation is the core of the implementation.

## How to run it in practice

Below is a minimal Git-oriented implementation flow.

## Why worktrees fit Syncwheel so well

If a repository has multiple active PR branches plus an integration branch, Git worktrees are usually the cleanest operational model.

A very practical setup is:
- the **repo root** stays on `main` as an administrative checkout
- each **PR branch** gets its own worktree
- the **integration branch** gets its own worktree too

That gives you three benefits immediately:
- less branch-hopping confusion
- fewer accidental mixed edits between unrelated streams
- a more truthful mapping between branch role and working directory

In other words, worktrees are not Syncwheel itself, but they are often the best physical layout for running Syncwheel repeatedly without losing clarity.

### A minimal worktree layout

```bash
git fetch --all --prune

git worktree add ../repo-pr-feature-a -b pr/feature-a upstream/main
git worktree add ../repo-pr-feature-b -b pr/feature-b upstream/main
git worktree add ../repo-integration -b integration/my-stack upstream/main
```

With that layout:
- `../repo-pr-feature-a` is only for PR A
- `../repo-pr-feature-b` is only for PR B
- `../repo-integration` is only for the combined runtime branch
- the original repo directory can return to `main`

### 1. Inspect the real state

Start by looking at remotes, branches, upstream tracking, and the recent graph:

```bash
git remote -v
git branch -vv
git stash list
git log --oneline --decorate --graph --all -20
```

If the repository uses both a fork and an upstream, refresh everything first:

```bash
git fetch --all --prune
```

### 2. Identify the canonical base

In many repositories, the real review base is `upstream/main` or `origin/main`.
Do not guess. Inspect the remotes and choose the branch that represents the official history.

### 3. Rebuild a clean PR branch

If a PR branch has become tangled with unrelated work, rebuild it from the canonical base and replay only the intended commits.

If you use worktrees, create the branch and workspace together:

```bash
git fetch --all --prune
git worktree add ../repo-pr-my-clean-branch -b pr/my-clean-branch upstream/main
cd ../repo-pr-my-clean-branch

git cherry-pick <commit-a> <commit-b> <commit-c>
```

Then validate and push:

```bash
npm test        # or project-specific validation
git push -u -f fork pr/my-clean-branch
```

The point is not the branch name. The point is that the PR branch is recreated from the correct base and contains only the intended change set.

### 4. Rebuild the integration branch

If the integration branch is the place where several unmerged streams must coexist, rebuild it intentionally from the same canonical base.

If you use worktrees:

```bash
git fetch --all --prune
git worktree add ../repo-integration -b integration/my-stack upstream/main
cd ../repo-integration

git cherry-pick <pr-a-commit-1> <pr-a-commit-2>
git cherry-pick <pr-b-commit-1>
git cherry-pick <hotfix-commit>
```

If the combined branch needs glue code that should not live inside any review PR, add a dedicated reconciliation commit there:

```bash
# edit files as needed
git add .
git commit -m "integration: reconcile combined branch state"
```

Then validate and publish it:

```bash
npm test        # or project-specific validation
git push -f fork integration/my-stack
```

### 5. Compare branch divergence explicitly

One of the key points of Syncwheel is that you should compare more than the current branch.
For example:

```bash
git rev-list --left-right --count upstream/main...pr/my-clean-branch
git rev-list --left-right --count upstream/main...integration/my-stack
```

This makes it easier to see whether a PR branch is just the intended change set, and how far the integration branch has moved beyond the canonical base.

### 6. Finish with a truthful report

At the end, the output should answer concrete questions such as:
- Which PR branches were rebuilt?
- Which branch is the integration branch now?
- Which commits were intentionally replayed?
- What still depends on something else?
- Which validation steps passed or failed?

## Example: two PRs, one hotfix, one integration branch

Imagine this situation:

- `feature-a` is already in review upstream
- `feature-b` depends on `feature-a`, but you do not want to wait for that merge before continuing
- a small hotfix is needed immediately for local use
- your fork needs all of them working together today

A Syncwheel-style implementation could look like this:

```bash
git fetch --all --prune

# rebuild clean PR branch A
git checkout -B pr/feature-a upstream/main
git cherry-pick <feature-a-commit-1> <feature-a-commit-2>
git push -f fork pr/feature-a

# rebuild clean PR branch B from upstream/main as its own review surface
git checkout -B pr/feature-b upstream/main
git cherry-pick <feature-b-commit-1> <feature-b-commit-2>
git push -f fork pr/feature-b

# rebuild combined integration branch for actual day-to-day use
git checkout -B integration/my-stack upstream/main
git cherry-pick <feature-a-commit-1> <feature-a-commit-2>
git cherry-pick <feature-b-commit-1> <feature-b-commit-2>
git cherry-pick <hotfix-commit>
git push -f fork integration/my-stack
```

That way:
- upstream sees clean PR branches
- your fork keeps a runnable combined branch
- you do not have to stop working just because review is slow
- each active stream can live in its own worktree without polluting the others

This is the core idea behind Syncwheel: separating **review surfaces** from **working reality**.

## What makes it hard?

The hard part is that this workflow is both important and annoying.

Two drawbacks show up again and again.

First, IDEs such as VSCode do not automatically make this broader situation obvious. They often show the status of the current branch, but not the deeper question: **how much work from other developers or other branches still needs to be synchronized into the real stack?**

Second, the workflow itself is cumbersome. It involves discovery, comparison, branch repair, integration repair, and validation. It is easy to do badly, and it is easy to miss hidden coupling between changes.

That is one reason AI is useful here. Not because it removes the complexity, but because it can help make the complexity visible and manageable.

## What Syncwheel is not

It is not a replacement for good branch hygiene.
It is not an excuse to let branches drift forever.
It is not a claim that every repository should work this way.

In a simple project with a short review cycle, you may never need anything like this.

But in open source, in internal platform work, and in teams with multiple parallel reviews, Syncwheel can be a perfectly healthy and intentional workflow.

## Why give it a name?

Because unnamed workflows are hard to repeat.

Teams often perform this kind of cleanup informally, in slightly different ways each time, usually only when something becomes painful enough.

Giving it a name makes it discussable.
It turns a vague maintenance effort into something you can ask for explicitly.
It also makes it easier to document, improve, automate, and teach.

That is the real value of Syncwheel.

It is a way to say:

> “Let’s stop pretending this repository is simpler than it is, recover the actual state, and put it back into order.”

## A practical definition

If you wanted to explain it in one sentence:

**Syncwheel is the workflow for recovering, realigning, and validating a repository when forks, open PRs, and integration branches have made the real state harder to see.**

That is all it is.
And in the right kind of project, that turns out to be extremely useful.
