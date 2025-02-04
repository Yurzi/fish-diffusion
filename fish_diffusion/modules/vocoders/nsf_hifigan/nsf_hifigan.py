import json
from pathlib import Path
from typing import Optional

import librosa
import pytorch_lightning as pl
import torch
from loguru import logger

from fish_diffusion.utils.audio import dynamic_range_compression
from fish_diffusion.utils.pitch_adjustable_mel import PitchAdjustableMelSpectrogram

# from ..builder import VOCODERS
from .models import AttrDict, Generator


class NsfHifiGAN(pl.LightningModule):
    checkpoint_path: str
    config_file: Optional[str]
    use_natural_log: bool

    def __init__(
        self,
        checkpoint_path: str = "checkpoints/nsf_hifigan/model",
        config_file: Optional[str] = None,
        use_natural_log: bool = True,
        project_root: str = ".",
        **kwargs,
    ):
        super().__init__()

        checkpoint_path = Path(project_root) / checkpoint_path
        logger.info(f"Loading NSF-HiFi-GAN from {checkpoint_path}")

        if config_file is None:
            config_file = Path(checkpoint_path).parent / "config.json"
        logger.info(f"Loading config from {config_file}")

        with open(config_file) as f:
            data = f.read()

        json_config = json.loads(data)
        self.h = AttrDict(json_config)
        self.model = Generator(self.h)
        self.use_natural_log = use_natural_log

        cp_dict = torch.load(checkpoint_path, map_location="cpu")

        if "state_dict" not in cp_dict:
            self.model.load_state_dict(cp_dict["generator"])
        else:
            self.model.load_state_dict(
                {
                    k.replace("generator.", ""): v
                    for k, v in cp_dict["state_dict"].items()
                    if k.startswith("generator.")
                }
            )

        self.model.eval()
        self.model.remove_weight_norm()

        self.mel_transform = PitchAdjustableMelSpectrogram(
            sample_rate=self.h.sampling_rate,
            n_fft=self.h.n_fft,
            win_length=self.h.win_size,
            hop_length=self.h.hop_size,
            f_min=self.h.fmin,
            f_max=self.h.fmax,
            n_mels=self.h.num_mels,
        )

        # Validate kwargs if any
        if "mel_channels" in kwargs:
            kwargs["num_mels"] = kwargs.pop("mel_channels")

        for k, v in kwargs.items():
            if getattr(self.h, k, None) != v:
                raise ValueError(f"Incorrect value for {k}: {v}")

    @torch.no_grad()
    def spec2wav(self, mel, f0, key_shift=0):
        c = mel[None]
        f0 *= 2 ** (key_shift / 12)

        if self.use_natural_log is False:
            c = 2.30259 * c

        f0 = f0[None].to(c.dtype)
        y = self.model(c, f0).view(-1)

        return y

    @property
    def device(self):
        return next(self.model.parameters()).device

    def wav2spec(self, wav_torch, sr=None, key_shift=0, speed=1.0):
        if sr is None:
            sr = self.h.sampling_rate

        if sr != self.h.sampling_rate:
            _wav_torch = librosa.resample(
                wav_torch.cpu().numpy(), orig_sr=sr, target_sr=self.h.sampling_rate
            )
            wav_torch = torch.from_numpy(_wav_torch).to(wav_torch.device)

        mel_torch = self.mel_transform(wav_torch, key_shift=key_shift, speed=speed)[0]
        mel_torch = dynamic_range_compression(mel_torch)

        if self.use_natural_log is False:
            mel_torch = 0.434294 * mel_torch

        return mel_torch
