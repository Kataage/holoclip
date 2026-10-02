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

Each saved candidate gets a stable candidate ID. The easiest review flow is:

    holoclip review data/evaluation/runs/stream-001.json

It shows one candidate at a time. Press a/m/r for accept/maybe/reject, s to skip, or q to stop. Already-labeled candidates are skipped by default.

Direct labeling is also available:

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


## Beyond Top-K: editorial-system evaluation

Top-K acceptance only measures local moment ranking. The long-term system also needs to be
evaluated as an idea generator and editor.

### Temporal accuracy

For source-aligned examples track:

- source boundary IoU
- start/end absolute error
- multi-cut segment precision/recall
- edit-map ordering accuracy
- short-moment localization error

### Idea quality

For each EditorialIdea track:

- human accept / maybe / reject
- evidence correctness
- context sufficiency
- novelty versus existing clips
- narrative completeness
- whether the proposed archetype fits the material
- whether optional historical/cross-stream context improves the idea

### Discovery versus imitation

Maintain separate metrics for:

- **retrieval-supported ideas** — strongly supported by human examples
- **exploratory ideas** — accepted ideas not substantially matching known human clips

The system is not successful if it only reproduces known clipping patterns.

### Diversity

Evaluate idea portfolios across:

- archetype
- duration
- member mix
- topic
- emotional tone
- recent versus archival material
- single-source versus multi-source

A high acceptance rate with near-identical output is a failure mode.

### Multimodal ablations

When enough data exists, compare:

- transcript only
- transcript + chat
- transcript + audio
- transcript + chat + audio
- full multimodal

This is necessary because different stream genres expose different signals.

### Product metrics

Track:

- preview -> publish rate
- revise rate
- reject rate
- human review minutes per published clip
- repeated work after retries
- render work discarded before approval

The product target is not simply model accuracy; it is high-quality published output with
minimal operator effort.
