# Evaluation

The central problem is ranking human-interest moments, not generating fluent explanations.

## Labels

For each surfaced candidate:

- accept - would seriously consider publishing
- maybe - interesting but needs different context, boundaries or angle
- reject - not worth showing again

Optional rejection reasons:
- not actually interesting
- context missing
- payoff too weak
- too similar to an existing idea
- only works for deep fans
- title/angle is better than the actual clip
- misinterprets joke or tone
- boundaries are wrong

Feedback is append-only JSONL. If the same stream/candidate is labeled more than once, the latest label wins in metric calculation. This makes corrections cheap and auditable.

## Primary metrics

### Top-K acceptance

For each stream track:
- Accept@3
- Accept@5
- (Accept + Maybe)@5

The v0.1 practical target is that Top-5 regularly contains at least two usable candidates before editing automation is built.

### Known-clip recall

For streams with existing human-made clips, map known clip windows back to the source transcript and test whether the detector surfaces a candidate covering at least half of the known clip window.

This is useful but not sufficient: the system should also discover worthwhile ideas that were never clipped.

### Candidate burden

Track how many labeled candidates a human must inspect per accepted item. The final product goal is near-zero operator effort.

## Running an evaluation

Save one analyzed stream as an evaluation run:

    holoclip analyze transcript.json --stream-id stream-001 --save-run data/evaluation/runs/stream-001.json

Each saved candidate gets a stable candidate ID. Record feedback:

    holoclip label stream-001 CANDIDATE_ID accept
    holoclip label stream-001 OTHER_ID maybe --reason "boundaries need work"
    holoclip label stream-001 THIRD_ID reject --reason "weak payoff"

Calculate metrics:

    holoclip metrics --runs-dir data/evaluation/runs

If known human-made source windows are available:

    holoclip metrics --runs-dir data/evaluation/runs --known-clips data/evaluation/known_clips.json

The example file at data/evaluation/known_clips.example.json documents that format.

## Avoid misleading evaluation

Raw views are not a funny score. Public performance mixes channel size, talent popularity, posting speed, title/thumbnail quality, topic timing and format.

When public metrics are used later, normalize within comparable cohorts.

## Initial validation set

Start small:
- talk-heavy streams
- game streams
- streams with known successful clips
- quiet or low-event streams

Scale example ingestion only after the ranking loop shows useful signal.
