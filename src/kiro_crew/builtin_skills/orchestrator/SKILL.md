---
name: orchestrator
description: Opt-in orchestrator mode. Load this only when the user asks for orchestrator mode or for one task split into plan, implement and review on different models. You plan on the session's model, then hand implementation and review to sub-agents tagged with those roles, and report each phase's model.
---

# Orchestrator

You split ONE task into three phases and run each on the model the owner chose
for it:

| Phase | Where it runs | Model |
|---|---|---|
| Plan | this session | the session's model |
| Implement | sub-agents, `spawn_run(role: "implement")` | `agent.role_models.implement` |
| Review | a sub-agent, `spawn_run(role: "review")` | `agent.role_models.review` |

The owner maps roles to models in Settings → Chat → Per-role models (the
`agent.role_models` config). An unset role falls back to the sub-agent model
(`agent.role_models.subagent`) and then to the session default, so with nothing
set every phase runs on the model it would run on anyway. The `plan` role is
for an orchestrator that hands planning to a sub-agent with
`spawn_run(role: "plan")`; this procedure plans in its own session.

You declare each phase through the spawn's `role` tag. Nothing infers a phase
for you, and the tag only picks the owner's saved model: never pass a `model`
of your own to change a phase's model, and never name a model the user did not
choose. A `model` you pass wins over the role, which is why you leave it out.

## When to use it

Use it when the user asks for orchestrator mode, or asks for one piece of work
to be planned, built and reviewed on different models. Do not switch into it on
your own for an ordinary request: a phased run costs a planning turn plus one
sub-agent per implementation slice plus a review, and it is the user's choice to
pay that.

## Procedure

1. **Plan, here.** Read what the task needs and write the plan in this session:
   the goal, the slices of implementation work, which files each slice owns, and
   how each slice is checked. Slices that touch the same file are one slice.
   Show the plan to the user before you implement anything that is hard to undo.
2. **Implement.** One `spawn_run` call for the wave, every entry tagged:

   ```
   spawn_run(tasks: ["<slice 1 brief>", "<slice 2 brief>"], role: "implement")
   ```

   Each brief is self-contained: the goal, the slice's files, the check that
   proves it done, and what to report back. Read the receipt's
   `ℹ role 'implement' ...` line and note the model it names.
3. **Wait** for every `[Subagent completion event]`. Do not do a slice's work
   yourself while it runs. A failed slice is retried once with what it reported;
   a second failure goes in the summary as unfinished.
4. **Review.** One sub-agent reviews the combined result against the plan:

   ```
   spawn_run(task: "<review brief: the plan, what changed, how to check it>", role: "review")
   ```

   The review brief asks for findings with file and line, not a rewrite. Note
   the model its `ℹ role 'review' ...` receipt line names.
5. **Act on the review.** Fix a real finding with another `role: "implement"`
   spawn (or yourself when it is one line), then review again only when the fix
   was more than mechanical.
6. **Summarise** for the user, ending with the phase-model table below.

## Phase models

End the summary with one row per phase, copied from what the runtime told you,
never from what you expected:

```
| Phase | Model | From |
|---|---|---|
| Plan | <the session's model> | this session |
| Implement | <model from the ℹ role 'implement' line> | <the source it names> |
| Review | <model from the ℹ role 'review' line> | <the source it names> |
```

A receipt line that says "the session default model" means the role was not
set; say so in the row rather than naming a model. When a completion event
names the model that served the run (`· model <id>` in a wave digest) and it
differs from the receipt, report the served model and note the difference.
