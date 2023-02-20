from pathlib import Path

import torch
from torch import nn
import numpy as np
from einops import rearrange
from beartype.typing import Optional

from torchaudio.functional import resample
from .utils import exists, curtail_to_multiple
from transformers import HubertModel
from sklearn.cluster import MiniBatchKMeans

import joblib
import logging
logging.root.setLevel(logging.ERROR)


class HfHubertWithKmeans(nn.Module):
    """
    Hugging Face HubertModel + a k-means layer on top. Pretrained checkpoint for music: https://huggingface.co/m-a-p/MERT-v0
    Note: Hubert outputs features at 50Hz while Wav2Vec-BERT (used in the paper) outputs at 25 Hz. Although Hubert embeddings should be better, it will have longer sequence lengths.
    """

    def __init__(
        self,
        *,
        hubert: HubertModel,
        kmeans: Optional[MiniBatchKMeans] = None,
        embed_layer: int=7,
        target_sample_hz=16000,
        seq_len_multiple_of=int(16000 / 50)
    ):
        super().__init__()
        self.target_sample_hz = target_sample_hz
        self.seq_len_multiple_of = seq_len_multiple_of

        self.embed_layer = embed_layer

        self.hubert = hubert
        self.kmeans = kmeans

    @torch.no_grad()
    def forward(
        self,
        wav_input: torch.Tensor,
        flatten=True,
        return_embed=False,
        input_sample_hz=None
    ):
        assert return_embed or exists(self.kmeans), "kmeans model must be provided if return_embed==False"

        device = wav_input.device

        if exists(input_sample_hz):
            wav_input = resample(wav_input, input_sample_hz, self.target_sample_hz)

        if exists(self.seq_len_multiple_of):
            wav_input = curtail_to_multiple(wav_input, self.seq_len_multiple_of)

        # normalize wav input
        mean, std = torch.mean(wav_input, dim=-1, keepdim=True), torch.std(wav_input, dim=-1, keepdim=True)
        wav_input = wav_input - mean
        non_zero_std = std.squeeze(1) > 0.
        wav_input[non_zero_std] = wav_input[non_zero_std] / std[non_zero_std]

        hubert_args = {
            'input_values': wav_input,
            'attention_mask': torch.ones_like(wav_input, device=device), # TODO: handle padding
        }

        outputs = self.hubert(**hubert_args, output_hidden_states = True)
        embed = outputs.hidden_states[self.embed_layer]

        if return_embed:
            return embed

        codebook_indices = self.kmeans.predict(embed.detach().cpu().numpy())

        codebook_indices = torch.from_numpy(codebook_indices).to(device).long()

        if flatten:
            return codebook_indices

        return rearrange(codebook_indices, 'b t -> b t 1')


def get_kmeans_model(
    n_clusters,
    init,
    max_iter,
    batch_size,
    tol,
    max_no_improvement,
    n_init,
    reassignment_ratio,
):
    return MiniBatchKMeans(
        n_clusters=n_clusters,
        init=init,
        max_iter=max_iter,
        batch_size=batch_size,
        verbose=1,
        compute_labels=False,
        tol=tol,
        max_no_improvement=max_no_improvement,
        init_size=None,
        n_init=n_init,
        reassignment_ratio=reassignment_ratio,
    )


def learn_kmeans(
    feat,
    seed,
    km_path='./results/kmeans.joblib',
    n_clusters=1024,
    init="k-means++",
    max_iter=100,
    batch_size=10000,
    tol=0.0,
    n_init=20,
    reassignment_ratio=0.0,
    max_no_improvement=100,
):
    np.random.seed(seed)
    km_model = get_kmeans_model(
        n_clusters,
        init,
        max_iter,
        batch_size,
        tol,
        max_no_improvement,
        n_init,
        reassignment_ratio,
    )
    km_model.fit(feat)
    joblib.dump(km_model, km_path)

    inertia = -km_model.score(feat) / len(feat)
    print("total intertia: %.5f", inertia)
    print("finished successfully")


def get_hubert_kmeans(model_name: str="m-a-p/MERT-v0", kmeans_path: Optional[str]='./checkpoints/kmeans.joblib'):
    wav2vec = HubertModel.from_pretrained(model_name)
    kmeans = joblib.load(kmeans_path) if exists(kmeans_path) else None

    return HfHubertWithKmeans(hubert=wav2vec, kmeans=kmeans)