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

## Primary metrics

### Top-K acceptance

For each stream track:
- Accept@3
- Accept@5
- (Accept + Maybe)@5

The v0.1 practical target is that Top-5 regularly contains at least two usable candidates before editing automation is built.

### Known-clip recall

For streams with existing human-made clips, map known clip windows back to the source transcript and test whether the detector surfaces an overlapping candidate.

This is useful but not sufficient: the system should also discover worthwhile ideas that were never clipped.

### Candidate burden

Track how many candidates a human must inspect per accepted item. The final product goal is near-zero operator effort.

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
