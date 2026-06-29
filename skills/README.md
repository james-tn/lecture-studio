# Skill packs (Agent Skills standard)

Each subdomain skill is a **folder containing a `SKILL.md`**, following the open
**Agent Skills** standard ([agentskills.io](https://agentskills.io)). The layout is:

```
skills/
  _base.md                         # always-loaded base vocabulary + timing rules (not a skill)
  <subject>/                       # our grouping (math, biology, ...) — the router scopes to it
    <skill-name>/
      SKILL.md                     # YAML frontmatter + Markdown body
```

A `SKILL.md` documents the idiomatic objects, actions, layout patterns, and worked
examples for one area. Because each skill is a self-contained standard folder, it is
**portable**: drop `cell-biology/` into `~/.claude/skills/`, an OpenAI Codex skills dir,
etc., and it works unchanged.

## SKILL.md format

```markdown
---
name: cell-biology                 # required — unique, kebab-case
description: <what it does>. Use when <triggers>.   # required — drives router selection
license: Apache-2.0                # optional
metadata:                          # optional (free-form)
  subject: biology
  version: "1.0"
---

# Human Title

## Idiomatic objects & actions ...
## Layout pattern ...
## Worked example (Lecture Score JSON) ...
## Pitfalls ...
```

## Progressive disclosure (how the router uses this)

`src/author.py` mirrors the standard's **progressive disclosure** so we never load every
skill at once:

1. **Level 1 — index.** `list_packs(subject)` reads only the `description` from each
   `SKILL.md` frontmatter and shows that menu to a fast router call (`select_skills`).
2. **Level 2 — body.** Only the chosen packs' Markdown bodies are loaded into the gpt-5.4
   authoring prompt (`_system_prompt`); the frontmatter is stripped.

(Anthropic's runtime adds a model-driven Level 3 — linked files/scripts the agent reads or
executes on demand — which requires an agentic filesystem loop we don't use for the
single-shot authoring call.)

## Adding a skill

1. `mkdir skills/<subject>/<skill-name>/`
2. Create `SKILL.md` with the frontmatter above (a clear `description` with "Use when …").
3. Fill the body: idiomatic objects/actions -> layout pattern -> a worked Lecture Score
   example -> pitfalls.

A new `<subject>/` directory automatically appears as a subject in the UI once it contains
at least one `*/SKILL.md`. The base vocabulary and timing rules live in `_base.md` and
always load.
