import json
import os
from pathlib import Path
import subprocess
import sys

from holoclip.execution import make_input_fingerprint


def test_provider_resume_id_is_identical_across_process_hash_seeds():
    root = Path(__file__).resolve().parents[1]
    script = '''
import json
from holoclip.execution import make_input_fingerprint
from holoclip.provider_benchmark import BenchmarkCase, BenchmarkManifest
from holoclip.providers.cost import Capability, free_registry
from holoclip.providers.mesh import StageContract, ModeRequirement, ExecutionMode
case = BenchmarkCase(id="case",clip_id="clip",task="text",source_url="https://example.org",
    teacher_reference="fixture",public_data=True,prompt="fixture Japanese context",
    dimensions={"japanese","avatar","reaction","game_ui","ocr"})
manifest = BenchmarkManifest(cases=(case,),training_clip_ids={"alpha","beta","gamma","delta"})
contract = StageContract(stage="fixture",alternatives=(ModeRequirement(mode=ExecutionMode.TEXT,
    required={Capability.REASONING_STRONG,Capability.REASONING_FAST,Capability.JSON}),),
    optional={Capability.GROUNDING,Capability.JAPANESE_READING},
    dimensions=case.dimensions,quota_estimate={"requests":1})
print(json.dumps({"dataset":make_input_fingerprint(manifest.model_dump(mode="json")),
    "stage":make_input_fingerprint(contract.model_dump(mode="json")),
    "registry":make_input_fingerprint({k:v.model_dump(mode="json") for k,v in free_registry().items()})},sort_keys=True))
'''
    observations = []
    for seed in ("1", "2", "7"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(root / "src"), PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                                capture_output=True, encoding="utf-8", timeout=30, check=True)
        observations.append(json.loads(result.stdout))
    assert observations[0] == observations[1] == observations[2]


def test_unordered_sets_are_canonical_but_frame_sequence_is_not_reordered():
    assert make_input_fingerprint({"ids": frozenset({"f2", "f1"})}) == make_input_fingerprint({"ids": ["f1", "f2"]})
    assert make_input_fingerprint({"frames": ["f1", "f2"]}) != make_input_fingerprint({"frames": ["f2", "f1"]})
