# Grounded semantic-video foundation

Install the isolated deterministic media extras with `uv sync --extra dev --extra media`.
FFmpeg/ffprobe remain external dependencies. PySceneDetect, OpenCV and Pillow perform
non-AI media analysis; there is no local model/checkpoint inference.

`measure_window` extracts at most 120 seconds, records the input/media SHA-256,
FFmpeg version, ffprobe PTS and PySceneDetect shot frame indexes. Representative
frames cover every detected shot; measured payoff/reaction hints add dense frames.
No random truncation silently drops a shot. Frames retain source resolution.
Contact-sheet labels sit above each image tile, with a machine-readable tile/frame/
shot/PTS mapping. Successful measurements are checksum-validated on reuse.

`VideoUnderstandingProvider` offers native video, shot-aware multi-image, contact
sheet and individual-image evidence fusion. Image-limited providers process bounded
groups, and successful HTTP units have their own cache. Fusion aggregates only
grounded individual observations and supplied transcript/audio/OCR/geometry/scene
evidence; it does not claim unseen motion or visual facts from text alone.

Strict structured output permits event/subject/reaction/game UI/object/source-switch
descriptions, semantic focus candidates, Japanese read text, confidence and unknowns.
It rejects unknown window/frame/shot IDs, unsupported visual claims, and extra fields
such as timestamps, crop coordinates, bounding boxes or paths. Batch validation
permits only the actual supplied image/tile IDs. `VisualTargetProvider` separately
owns localization/tracking. `semantic_evidence` uses the measured window interval
and marks pixel targets NOT_MEASURED, rather than inventing an exact event time.

Training source/finished pairs and selected ClipTimeline windows share this evidence
bridge. They remain separate observations: this does not induce an Editing Profile
or pretend that an Editorial Profile is an Editing Profile. Diagnostic benchmark
results cannot become production editing evidence.

HTTP implementations cover Gemini standard generateContent inline bounded video/
images, NVIDIA documented OpenAI-compatible image/Omni video, Groq Qwen vision/text,
Mistral vision/text, and account-scoped Cloudflare Free REST vision/text. Model-list
probes are read-only, redirect-disabled and do not establish free account status.
The adapter consumes router decisions; it contains no vendor fallback chain.

Current constraints are explicit:

- Cosmos's free native-video candidate is documented, but its current hosted request
  schema could not be verified from the official API reference during this audit.
  The diagnostic records a policy block instead of guessing a request or using a
  paid/partner route. Resolve that schema before its qualification.
- Llama 90B remains unavailable pending fresh endpoint evidence.
- Cerebras is diagnostic/unavailable until recurring no-payment entitlement is
  verified. OpenRouter requires an exact currently verified `:free` entry, zero-price
  limits and disabled provider fallback; no volatile random free alias is promoted.
- Active Speaker Detection's official endpoint uses gRPC/NVCF and real-person
  tracking. It is not an OpenAI chat model. Record a specialized rejection until its
  no-cost endpoint and Live2D applicability have a separate validated transport.
- Inline bounded uploads avoid temporary Files API state. Large Files API and
  direct YouTube URL lifecycle are still pending #92; unsupported units block rather
  than invoke an unverified upload/storage/grounding operation. No interactions
  persistence is enabled; `store=false` is therefore not an applicable REST field.

No live semantic quality is claimed by the mocked adapters or synthetic FFmpeg test.
#99 must qualify each capability and each mode on held-out real Holoclip footage.
