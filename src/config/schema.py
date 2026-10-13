"""The single source of truth for every tunable in the project.

One YAML file per stage profile, validated into these models. Nothing in the
codebase reads an environment variable or hard-codes a hyperparameter; if a
number affects a result, it is a field here, and it is recorded in the
provenance of every checkpoint that number produced.

Three parts of this schema exist because of decisions taken at Stage 0 and are
not incidental:

  * `BackendConfig` is a discriminated union, so the model is a swappable
    backend from the first line of code rather than a retrofit. `scratch` is
    the transformer trained here; `gguf` is a quantized open-weights base
    served through llama.cpp. Everything downstream -- data, retrieval, tools,
    serving, evals -- is written against whichever one the config names.
  * `modality_mode` is present from day one with `text` as the default,
    because at this scale learned projectors are expected to lose to a
    transcript-plus-caption baseline, and the fallback must not be a rewrite.
  * `DeviceProfile` is a list, not a single target. Each declared device gets
    its own sizes and budgets, and the capability tier fails the build when a
    profile misses its own numbers rather than a shared lowest denominator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)


class Strict(BaseModel):
    """Reject unknown keys everywhere.

    A silently ignored typo in a config file is a hyperparameter that did not
    take effect, found days later in a result that cannot be explained.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


class ModelConfig(Strict):
    """Shape of the from-scratch decoder. Pre-norm, RMSNorm, RoPE, SwiGLU, GQA."""

    n_layers: PositiveInt
    d_model: PositiveInt
    n_heads: PositiveInt
    # Grouped-query attention: n_kv_heads < n_heads shrinks the KV cache,
    # which is what the laptop and the board are short of.
    n_kv_heads: PositiveInt
    d_ff: PositiveInt
    vocab_size: PositiveInt
    max_seq_len: PositiveInt
    rope_theta: PositiveFloat = 10_000.0
    rms_norm_eps: PositiveFloat = 1e-5
    tie_embeddings: bool = True
    # No biases anywhere; kept as a field only so the ablation is expressible.
    bias: bool = False
    dropout: NonNegativeFloat = 0.0

    @model_validator(mode="after")
    def _divisible(self) -> ModelConfig:
        if self.d_model % self.n_heads:
            raise ValueError(
                f"d_model ({self.d_model}) must divide by n_heads ({self.n_heads})"
            )
        if self.n_heads % self.n_kv_heads:
            raise ValueError(
                f"n_heads ({self.n_heads}) must divide by "
                f"n_kv_heads ({self.n_kv_heads}) for grouped-query attention"
            )
        return self

    @property
    def head_dim(self) -> int:
        return self.d_model // self.n_heads

    def parameter_count(self) -> int:
        """Exact count, to check against what the built model reports.

        A mismatch between this and the instantiated module is a bug in one of
        them, and the T0 tier asserts they agree.
        """
        head_dim = self.head_dim
        embed = self.vocab_size * self.d_model
        attn_per_layer = (
            self.d_model * self.d_model  # q
            + 2 * self.d_model * (self.n_kv_heads * head_dim)  # k, v
            + self.d_model * self.d_model  # o
        )
        # SwiGLU: gate and up projections in, down projection out.
        ffn_per_layer = 3 * self.d_model * self.d_ff
        norms_per_layer = 2 * self.d_model
        blocks = self.n_layers * (attn_per_layer + ffn_per_layer + norms_per_layer)
        final_norm = self.d_model
        head = 0 if self.tie_embeddings else self.vocab_size * self.d_model
        return embed + blocks + final_norm + head


# --------------------------------------------------------------------------
# backend -- the architectural commitment from Stage 0
# --------------------------------------------------------------------------


class ScratchBackend(Strict):
    """The transformer trained in this repository. The control, not the ship."""

    kind: Literal["scratch"] = "scratch"
    checkpoint: Path | None = None
    model: ModelConfig
    precision: Literal["fp32", "fp16", "bf16"] = "fp16"
    # torch.compile on ROCm with gfx1031 is unreliable; off until the numerics
    # are checked against eager and it is shown to help.
    compile: bool = False


class GgufBackend(Strict):
    """A quantized open-weights base, served through llama.cpp. The primary."""

    kind: Literal["gguf"] = "gguf"
    model_path: Path
    quantization: Literal["Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0", "F16"] = "Q4_K_M"
    n_ctx: PositiveInt = 4096
    n_gpu_layers: int = -1  # -1 offloads everything the device can hold
    # LoRA adapter from the Saule fine-tune, applied at load.
    lora_path: Path | None = None


BackendConfig = Annotated[
    ScratchBackend | GgufBackend, Field(discriminator="kind")
]


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


class SauleToolchain(Strict):
    """How the harness reaches the compiler.

    `binary` is pinned to a path and `git_sha` to a commit because Stage 0
    measured the installed `saule` on PATH disagreeing with the repository
    HEAD: the same project type-checked clean at HEAD and failed with the
    older binary. A sample's accept/reject verdict is only meaningful next to
    the exact compiler that issued it, so both are recorded in every manifest.
    """

    repo: Path
    binary: Path
    git_sha: str = Field(min_length=7, max_length=40)
    # `saule check` measured at ~4 ms/file with 8 parallel jobs on an M1.
    check_timeout_s: PositiveFloat = 10.0
    run_timeout_s: PositiveFloat = 10.0
    max_output_bytes: PositiveInt = 65_536
    parallel_jobs: PositiveInt = 8

    @field_validator("git_sha")
    @classmethod
    def _hex(cls, value: str) -> str:
        if any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise ValueError(f"git_sha must be hex, got {value!r}")
        return value.lower()


class GeneratorConfig(Strict):
    """One synthetic-data generator, and the yield it is held to.

    Every generator reports how many candidates it produced and how many
    survived `saule fmt` then `saule check`. A generator whose accept rate
    falls below `min_accept_rate` is a bug or a bad prompt, and the pipeline
    says so rather than quietly contributing noise.
    """

    name: str
    enabled: bool = True
    target_samples: PositiveInt
    min_accept_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.1
    # Seeds come only from the training split; the eval fixtures are excluded
    # from every generator's seed pool by construction, not by filtering later.
    seed_groups: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class DataConfig(Strict):
    root: Path
    saule: SauleToolchain
    generators: list[GeneratorConfig] = Field(default_factory=list)

    # Dedup runs on bytecode where the compiler can produce it, and on
    # formatted text where it cannot. Stage 0 measured `saule disasm` as
    # single-file only: 113/113 single-file programs disassemble, but only
    # 14/68 multi-file example files and 2/12 library files do, because a file
    # compiled in isolation cannot resolve its imports. So the key is not
    # uniform across the corpus and the fallback is not optional.
    dedup_on: Literal["bytecode", "formatted_text", "bytecode_then_text"] = (
        "bytecode_then_text"
    )
    minhash_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = 0.8
    decontamination_ngram: PositiveInt = 13

    # Fractions of the *natural* corpus. The eval fixtures are held out whole,
    # so these apply to everything else.
    val_fraction: Annotated[float, Field(ge=0.0, lt=1.0)] = 0.05
    seed: int = 1337

    @model_validator(mode="after")
    def _unique_generator_names(self) -> DataConfig:
        names = [g.name for g in self.generators]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate generator names: {sorted(duplicates)}")
        return self


# --------------------------------------------------------------------------
# training
# --------------------------------------------------------------------------


class OptimizerConfig(Strict):
    lr: PositiveFloat
    betas: tuple[float, float] = (0.9, 0.95)
    weight_decay: NonNegativeFloat = 0.1
    eps: PositiveFloat = 1e-8
    grad_clip: PositiveFloat = 1.0


class ScheduleConfig(Strict):
    kind: Literal["cosine", "linear", "constant"] = "cosine"
    warmup_steps: NonNegativeInt = 100
    min_lr_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = 0.1


class TrainConfig(Strict):
    micro_batch_size: PositiveInt
    grad_accum_steps: PositiveInt = 1
    max_steps: PositiveInt
    optimizer: OptimizerConfig
    schedule: ScheduleConfig = ScheduleConfig()

    # Checkpointing carries optimizer, scheduler, RNG and dataloader position,
    # so a killed run resumes without loss. A checkpoint missing any of those
    # is a bug the T1 tier tests for.
    checkpoint_every_steps: PositiveInt = 500
    checkpoint_dir: Path = Path("checkpoints")
    keep_last_n_checkpoints: PositiveInt = 3

    eval_every_steps: PositiveInt = 500
    log_every_steps: PositiveInt = 10
    # Vocab-sized logits are the usual OOM; the loss is computed in chunks.
    ce_chunk_size: PositiveInt = 1024
    seed: int = 1337

    @property
    def tokens_per_step(self) -> int:
        return self.micro_batch_size * self.grad_accum_steps


class LoraConfig(Strict):
    """The primary track: LoRA on a 1.5-3B base, fp16, inside 12 GB."""

    r: PositiveInt = 16
    alpha: PositiveFloat = 32.0
    dropout: NonNegativeFloat = 0.05
    target_modules: list[str] = Field(
        default_factory=lambda: [
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ]
    )
    # bitsandbytes 4-bit training on ROCm is unverified; Stage 0 plans without
    # it and the probe reports whether it is usable.
    load_in_4bit: bool = False


# --------------------------------------------------------------------------
# modality, retrieval, serving
# --------------------------------------------------------------------------


class ModalityConfig(Strict):
    """`text` is the default because it is the baseline the rest must beat."""

    mode: Literal["embed", "text", "hybrid"] = "text"
    audio_encoder: str | None = "whisper-tiny"
    vision_encoder: str | None = "clip-vit-base-patch32"
    n_audio_tokens: PositiveInt = 32
    n_image_tokens: PositiveInt = 32
    # Encoders load on first use and are released after, so audio and vision
    # weights never sit resident in a 16 GB machine that is also serving.
    lazy_load: bool = True


class RetrievalConfig(Strict):
    enabled: bool = True
    embedding_model: str = "bge-small-en-v1.5"
    index_path: Path = Path("data/processed/index")
    # The whole doc set is ~50k words, so it is indexed entire.
    chunk_tokens: PositiveInt = 512
    chunk_overlap_tokens: NonNegativeInt = 64
    top_k: PositiveInt = 8
    rerank_top_k: PositiveInt = 3
    # mmap rather than load: the index must not cost resident memory.
    mmap: bool = True


class ToolConfig(Strict):
    """Sandboxing for the tools the model may call."""

    enabled: bool = True
    timeout_s: PositiveFloat = 10.0
    max_output_bytes: PositiveInt = 65_536
    domain_allowlist: list[str] = Field(default_factory=list)
    # Retrieved text is fenced and labelled untrusted in the prompt the model
    # sees. Never an instruction, always data.
    fence_untrusted: bool = True


class SamplingConfig(Strict):
    temperature: NonNegativeFloat = 0.7
    top_k: NonNegativeInt = 0
    top_p: Annotated[float, Field(gt=0.0, le=1.0)] = 0.95
    min_p: Annotated[float, Field(ge=0.0, le=1.0)] = 0.05
    repetition_penalty: PositiveFloat = 1.1
    max_new_tokens: PositiveInt = 512
    stop: list[str] = Field(default_factory=list)
    seed: int | None = None


class ServeConfig(Strict):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    sampling: SamplingConfig = SamplingConfig()
    stream: bool = True
    max_request_bytes: PositiveInt = 1_048_576
    rate_limit_per_minute: PositiveInt = 60
    # Constrained decoding ships only once the grammar's disagreement with the
    # real parser is measured and small, and always with this fallback.
    grammar_path: Path | None = None
    grammar_fallback_unconstrained: bool = True


# --------------------------------------------------------------------------
# devices -- a profile per declared target, not one denominator
# --------------------------------------------------------------------------


class LatencyBudget(Strict):
    """Measured end to end, microphone to action. The build fails on a miss."""

    speech_to_response_s: PositiveFloat
    sustained_tokens_per_s: PositiveFloat
    visual_question_to_answer_s: PositiveFloat
    capture_hz: PositiveFloat
    detector_hz: PositiveFloat
    cold_start_s: PositiveFloat
    crash_recovery_s: PositiveFloat
    max_resident_memory_gb: PositiveFloat


class DeviceProfile(Strict):
    name: str
    tier: Literal["tier0_build", "tier1_laptop", "tier2_board"]
    # Train only where it is allowed. The GTX laptop and the board never train.
    trainable: bool = False
    backend: BackendConfig
    modality: ModalityConfig = ModalityConfig()
    budget: LatencyBudget
    notes: str = ""


# --------------------------------------------------------------------------
# root
# --------------------------------------------------------------------------


class ProvenanceConfig(Strict):
    """What every checkpoint must be able to name about its own origin.

    A checkpoint whose provenance cannot be reconstructed is a bug, so these
    are recorded at save time and asserted on load.
    """

    git_sha: str | None = None
    config_hash: str | None = None
    data_manifest_hash: str | None = None
    tokenizer_version: str | None = None


class Config(Strict):
    """The root document. One YAML file per stage profile."""

    profile_name: str
    stage: NonNegativeInt

    data: DataConfig
    # Which declared device this run targets; must name one of `devices`.
    active_device: str
    devices: list[DeviceProfile] = Field(min_length=1)

    train: TrainConfig | None = None
    lora: LoraConfig | None = None
    retrieval: RetrievalConfig = RetrievalConfig()
    tools: ToolConfig = ToolConfig()
    serve: ServeConfig = ServeConfig()
    provenance: ProvenanceConfig = ProvenanceConfig()

    @model_validator(mode="after")
    def _coherent(self) -> Config:
        names = [d.name for d in self.devices]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate device names: {sorted(duplicates)}")
        if self.active_device not in names:
            raise ValueError(
                f"active_device {self.active_device!r} is not among the declared "
                f"devices {names}"
            )
        device = self.device
        if self.train is not None and not device.trainable:
            raise ValueError(
                f"device {device.name!r} is declared trainable=false but this "
                "config carries a `train` section"
            )
        if self.lora is not None and device.backend.kind != "gguf":
            # A LoRA section belongs to the transfer track, whose serving
            # backend is the quantized base.
            raise ValueError(
                "a `lora` section applies to the transfer track, whose backend "
                f"is `gguf`; active device {device.name!r} uses "
                f"{device.backend.kind!r}"
            )
        return self

    @property
    def device(self) -> DeviceProfile:
        return next(d for d in self.devices if d.name == self.active_device)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        """Parse and validate a YAML profile. Raises on anything unexpected."""
        text = Path(path).read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must contain a YAML mapping at the top level")
        return cls.model_validate(raw)
