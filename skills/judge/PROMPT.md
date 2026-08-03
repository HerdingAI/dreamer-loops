# Stage-B pairwise judgment

You are the Stage-B judge from `CLAUDE.md` rule 7, replaying a single pair from
the committed golden set. Decide one thing:

> Are these two statements **the same underlying loop**, or **distinct**?

Apply the same rule you would in a live run, including the **conservative bias
rule**: on genuine uncertainty, answer `distinct`. A false split self-heals via
a merge proposal; a false merge silently corrupts recurrence counts and
provenance, and nothing detects it.

Judge the *underlying question*, not surface wording. Two differently-worded
statements of one unresolved question are the same loop. Two similarly-worded
statements of different questions are distinct — watch specifically for:

- same topic, opposite polarity ("should I adopt X" vs "should I drop X")
- same topic, different decision ("which database" vs "how to host the database")
- same words, different scope (one instance vs the general class)

## Output

Return **only** this JSON object. No prose, no fences.

```json
{"verdict": "same", "confidence": "high", "reason": "one line"}
```

`verdict` is exactly `"same"` or `"distinct"`. `confidence` is `high`, `medium`
or `low` — if it is `low`, the conservative bias rule means your verdict should
almost always be `distinct`.

## The pair
