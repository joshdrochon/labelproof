# Hallway test protocol

Written **before** the test is run, so the success criteria cannot be adjusted to whatever
happens (LP-272, UX-1). Nobody has run this yet; the results section is empty on purpose.

## What is being tested

The brief's benchmark, in Sarah Chen's words: a 73-year-old should be able to use this. It
is not a metaphor — the team is 47 agents, many long-tenured, and the previous vendor's
tool was abandoned partly because it needed training nobody had time for.

So the question is narrow: **can someone who has never seen this reach a verdict without
being told anything?**

## Recruiting

Three people minimum. None of them may have seen the app, the README, or a screenshot.
They do not need to know what TTB is — if the screen requires that, that is a finding.

Do not recruit engineers who have watched this being built. A colleague who has heard
about it for a week is not a cold user.

## Setup

Sit them in front of <https://labelproof.fly.dev> on a normal laptop. Hand them nothing.

Say exactly this and nothing more:

> "This is a tool for checking alcohol label applications. Please try to use it. Think out
> loud as you go. I can't answer questions while you're working, but ask them anyway —
> what you ask is the useful part."

Then be quiet. The urge to help is the thing to resist; every hint you give deletes a
finding.

## Tasks, in order

1. **Check a label.** No further instruction. They may use a sample or upload something —
   both count.
2. **Say what the tool decided, and why.** In their own words, without scrolling back.
3. **Find the row that needs attention** and say what they would do about it.
4. **Do it again for a batch.** Only if tasks 1–3 took under ten minutes.

## Success criteria — fixed now

| | Passes if |
|---|---|
| **Reaches a verdict** | All 3 do, unaided, within 5 minutes of sitting down |
| **Understands the recommendation** | All 3 can state it in their own words and say it is advice rather than a decision |
| **Finds the problem row** | At least 2 of 3 identify the row needing attention without being pointed at it |
| **Knows what to do next** | At least 2 of 3 name a plausible next action |
| **No dead ends** | Nobody gets stuck with no idea what to click, at any point |

**Anything less is a fail, and the fix goes in before submission.** The point of writing
this down first is that "well, two of them nearly got it" is not available afterwards.

## What to record

For each person: what they clicked first, where they hesitated for more than five seconds,
every question they asked, every place they said "I don't know what this means", and the
exact words they used for the recommendation. Verbatim beats summary — their wording is
the copy fix.

## What is already known

One informal data point, from the only person other than the author who has used it: they
uploaded a label and asked **"and then what?"** The form on the right read as a separate
panel rather than as the next step. Both panels are numbered now — Step 1 pictures, Step 2
the application — and whether that fixed it is exactly what task 1 tests.

That was one question from one person and it found a real defect. It is the argument for
running this properly.

## Results

_Not yet run._ Three cold users are required and none have been recruited. This section
stays empty rather than filled with plausible output.
