import os
import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
import kornia
import math
import gc

from PIL import Image
from madgrad import MADGRAD

from typing import Optional, Union, Callable, List

script_dir = os.path.dirname(os.path.abspath(__file__))
evals_dir = os.path.dirname(script_dir)
project_dir = os.path.dirname(evals_dir)
sys.path.append(project_dir)

torch.manual_seed(42)

from dyna import DynamicConv2D, WeightsLib2D, siglog, siglog_parametric


class DecoderOnlyModel(nn.Module):
    def __init__(
        self,
        data_cache_ctx_len: int = None,
        data_cache_latents_len: int = None,
        data_cache_latents_shape: list[int] = None,
        dropout_rate: float = 0.01,
        data_cache_ctx_bound: float = 0.01,
        data_cache_latents_bound: float = 0.01,
        dtype_weights: torch.dtype = torch.float32,
    ):
        super().__init__()

        self.data_cache_ctx_len = data_cache_ctx_len
        self.data_cache_latents_len = data_cache_latents_len
        self.data_cache_latents_shape = data_cache_latents_shape
        self.dropout_rate = dropout_rate
        self.use_bias = False
        self.bias_static = 0.0
        self.context_length = 64
        self.mod_rank = 16
        self.transformations_rank = 32

        self.data_cache_ctx_bound = data_cache_ctx_bound
        self.data_cache_latents_bound = data_cache_latents_bound

        self.kernel_size_t = [3, 3]
        self.kernel_size_c_sml = [3, 3]
        self.kernel_size_c_med = [5, 5]
        self.kernel_size_c_lrg = [7, 7]

        self.eps = 1.0e-3
        self.q_levels = 16
        self.q_scale = math.e

        self.channels_io = 3
        self.channels_dynamic_levels = [8, 8, 8, 8, 8]

        self.dtype_weights = dtype_weights

        self.dropout = nn.Dropout(p=self.dropout_rate)
        self.upsample_nearest = nn.Upsample(scale_factor=2, mode="nearest")
        self.upsample_bilinear = nn.Upsample(scale_factor=2, mode="bilinear")
        # ====> Block 04
        self.conv_04_upsampler = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[4],
            out_channels=self.channels_dynamic_levels[4],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_t,
            stride=[2, 2],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=True,
            output_padding=[1, 1],
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_04_c_sml = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[4],
            out_channels=self.channels_dynamic_levels[3],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_sml,
            stride=[1, 1],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_04_c_med = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[4],
            out_channels=self.channels_dynamic_levels[3],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_med,
            stride=[1, 1],
            padding=[2, 2],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_04_c_lrg = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[4],
            out_channels=self.channels_dynamic_levels[3],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_lrg,
            stride=[1, 1],
            padding=[3, 3],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_04_reducer_delta = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[4]
            + self.channels_dynamic_levels[3] * 3,
            out_channels=self.channels_dynamic_levels[3],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=[1, 1],
            stride=[1, 1],
            padding=[0, 0],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        # ====> Block 03
        self.conv_03_upsampler = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[3],
            out_channels=self.channels_dynamic_levels[3],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_t,
            stride=[2, 2],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=True,
            output_padding=[1, 1],
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_03_c_sml = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[3],
            out_channels=self.channels_dynamic_levels[2],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_sml,
            stride=[1, 1],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_03_c_med = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[3],
            out_channels=self.channels_dynamic_levels[2],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_med,
            stride=[1, 1],
            padding=[2, 2],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_03_c_lrg = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[3],
            out_channels=self.channels_dynamic_levels[2],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_lrg,
            stride=[1, 1],
            padding=[3, 3],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_03_reducer_delta = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[3]
            + self.channels_dynamic_levels[2] * 3,
            out_channels=self.channels_dynamic_levels[2],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=[1, 1],
            stride=[1, 1],
            padding=[0, 0],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        # ====> Block 02
        self.conv_02_upsampler = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[2],
            out_channels=self.channels_dynamic_levels[2],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_t,
            stride=[2, 2],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=True,
            output_padding=[1, 1],
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_02_c_sml = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[2],
            out_channels=self.channels_dynamic_levels[1],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_sml,
            stride=[1, 1],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_02_c_med = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[2],
            out_channels=self.channels_dynamic_levels[1],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_med,
            stride=[1, 1],
            padding=[2, 2],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_02_c_lrg = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[2],
            out_channels=self.channels_dynamic_levels[1],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_lrg,
            stride=[1, 1],
            padding=[3, 3],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_02_reducer_delta = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[2]
            + self.channels_dynamic_levels[1] * 3,
            out_channels=self.channels_dynamic_levels[1],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=[1, 1],
            stride=[1, 1],
            padding=[0, 0],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        # ====> Block 01
        self.conv_01_upsampler = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[1],
            out_channels=self.channels_dynamic_levels[1],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_t,
            stride=[2, 2],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=True,
            output_padding=[1, 1],
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_01_c_sml = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[1],
            out_channels=self.channels_dynamic_levels[0],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_sml,
            stride=[1, 1],
            padding=[1, 1],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_01_c_med = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[1],
            out_channels=self.channels_dynamic_levels[0],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_med,
            stride=[1, 1],
            padding=[2, 2],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_up_01_c_lrg = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[1],
            out_channels=self.channels_dynamic_levels[0],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=self.kernel_size_c_lrg,
            stride=[1, 1],
            padding=[3, 3],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        self.conv_01_reducer_delta = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[1]
            + self.channels_dynamic_levels[0] * 3,
            out_channels=self.channels_dynamic_levels[0],
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=[1, 1],
            stride=[1, 1],
            padding=[0, 0],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )
        # ====> Block out
        self.conv_out = DynamicConv2D(
            in_channels=self.channels_dynamic_levels[0],
            out_channels=self.channels_io,
            context_length=self.context_length,
            mod_rank=self.mod_rank,
            transformations_rank=self.transformations_rank,
            kernel_size=[1, 1],
            stride=[1, 1],
            padding=[0, 0],
            dilation=[1, 1],
            bias_dynamic=self.use_bias,
            bias_static=self.bias_static,
            transpose=False,
            output_padding=None,
            asymmetry=1.0e-3,
            dtype_weights=self.dtype_weights,
        )

        self.data_cache_ctx = nn.Parameter(
            data=torch.nn.init.uniform_(
                tensor=torch.empty(
                    [self.data_cache_ctx_len, self.context_length],
                    dtype=self.dtype_weights,
                ),
                a=-self.data_cache_ctx_bound,
                b=+self.data_cache_ctx_bound,
            ),
        )
        self.data_cache_latents = nn.Parameter(
            data=torch.nn.init.uniform_(
                tensor=torch.empty(
                    [self.data_cache_latents_len, *self.data_cache_latents_shape],
                    dtype=self.dtype_weights,
                ),
                a=-self.data_cache_latents_bound,
                b=+self.data_cache_latents_bound,
            ),
        )

        self.wl_noise_04 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.data_cache_latents_shape[1] * 2,
                self.data_cache_latents_shape[2] * 2,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_noise_03 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.data_cache_latents_shape[1] * 4,
                self.data_cache_latents_shape[2] * 4,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_noise_02 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.data_cache_latents_shape[1] * 8,
                self.data_cache_latents_shape[2] * 8,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_noise_01 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.data_cache_latents_shape[1] * 16,
                self.data_cache_latents_shape[2] * 16,
            ],
            dtype_weights=self.dtype_weights,
        )

        self.wl_ctx_04 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.context_length,
                self.context_length,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_ctx_03 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.context_length,
                self.context_length,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_ctx_02 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.context_length,
                self.context_length,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_ctx_01 = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.context_length,
                self.context_length,
            ],
            dtype_weights=self.dtype_weights,
        )
        self.wl_ctx_out = WeightsLib2D(
            components_count=self.context_length,
            mod_rank=self.mod_rank,
            output_shape=[
                self.context_length,
                self.context_length,
            ],
            dtype_weights=self.dtype_weights,
        )

        pass

    def quantizer(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:
        with torch.no_grad():
            l = self.q_levels
            s = self.q_scale
            shift = 1.0 / (l * 2)
            x_q = x / s
            x_q = (x_q * l).clamp(-l, +l)
            x_q = (x_q // 1.0).to(dtype=x.dtype, device=x.device)
            x_q = (x_q / l) + shift
            x_q = x_q * s
        x = x + (x_q - x).detach()
        return x

    def doubleLogNormAbs(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:
        x = x.abs()
        x = x.sub(x.min(-1)[0].min(-1)[0].unsqueeze(-1).unsqueeze(-1))
        x = x.add(torch.e).log().add(self.eps).log()
        return x

    def doubleLogNorm(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:
        x_mul = torch.where(x > 0.0, +1.0, -1.0).to(dtype=x.dtype, device=x.device)
        x = x.abs()
        x = x.sub(x.min(-1)[0].min(-1)[0].unsqueeze(-1).unsqueeze(-1))
        x = x.add(torch.e).log().add(self.eps).log()
        x = x.mul(x_mul)
        return x

    def rescale(
        self,
        x: torch.Tensor,
        min: float = -1.0,
        max: float = +1.0,
    ) -> torch.Tensor:
        assert min < max, f"min: {min}, max: {max}"

        scaling_range = max - min

        x_min = (
            x.flatten(1)
            .min(1)[0]
            .reshape([x.shape[0], *[1 for _ in range(len(x.shape) - 1)]])
        )
        x_max = (
            x.flatten(1)
            .max(1)[0]
            .reshape([x.shape[0], *[1 for _ in range(len(x.shape) - 1)]])
        )

        x = (x - x_min + self.eps) / (x_max - x_min + (self.eps * 2))
        x = x * scaling_range + min

        return x

    def forward(
        self,
        ids: torch.Tensor,
    ) -> torch.Tensor:
        ids = ids.to(device=self.data_cache_latents.device, dtype=torch.int32)
        context = self.data_cache_ctx[ids]
        x = self.data_cache_latents[ids]

        activation_fn = lambda x: siglog_parametric(x, alpha=1.0 / torch.e)
        ctx = context

        # Pseudo-input normalization
        x = F.layer_norm(x, x.shape[1:])
        ctx = F.layer_norm(ctx, ctx.shape[1:])

        # Block 04
        x = self.dropout(x)
        ctx = (ctx.unsqueeze(1) @ self.wl_ctx_04(ctx)).squeeze(1)
        ctx = activation_fn(ctx)
        ctx = F.layer_norm(ctx, ctx.shape[1:])
        ctx = self.dropout(ctx)
        x_upsampled = self.conv_04_upsampler(x, ctx)
        x_upsampled = activation_fn(x_upsampled)
        x_upsampled = F.layer_norm(x_upsampled, x_upsampled.shape[1:])
        noise = self.wl_noise_04(ctx)
        noise = activation_fn(noise).unsqueeze(1)
        noise = F.layer_norm(noise, noise.shape[1:])
        x_noised = x_upsampled + noise
        x_c_sml = self.conv_up_04_c_sml(x_noised, ctx)
        x_c_sml = activation_fn(x_c_sml)
        x_c_sml = F.layer_norm(x_c_sml, x_c_sml.shape[1:])
        x_c_med = self.conv_up_04_c_med(x_noised, ctx)
        x_c_med = activation_fn(x_c_med)
        x_c_med = F.layer_norm(x_c_med, x_c_med.shape[1:])
        x_c_lrg = self.conv_up_04_c_lrg(x_noised, ctx)
        x_c_lrg = activation_fn(x_c_lrg)
        x_c_lrg = F.layer_norm(x_c_lrg, x_c_lrg.shape[1:])
        x_denoised = torch.cat([x_upsampled, x_c_sml, x_c_med, x_c_lrg], dim=1)
        x_denoised = self.conv_04_reducer_delta(x_denoised, ctx)
        x_denoised = activation_fn(x_denoised)
        x_denoised = F.layer_norm(x_denoised, x_denoised.shape[1:])
        x = x_denoised

        # Block 03
        x = self.dropout(x)
        ctx = (ctx.unsqueeze(1) @ self.wl_ctx_03(ctx)).squeeze(1)
        ctx = activation_fn(ctx)
        ctx = F.layer_norm(ctx, ctx.shape[1:])
        ctx = self.dropout(ctx)
        x_upsampled = self.conv_03_upsampler(x, ctx)
        x_upsampled = activation_fn(x_upsampled)
        x_upsampled = F.layer_norm(x_upsampled, x_upsampled.shape[1:])
        noise = self.wl_noise_03(ctx)
        noise = activation_fn(noise).unsqueeze(1)
        noise = F.layer_norm(noise, noise.shape[1:])
        x_noised = x_upsampled + noise
        x_c_sml = self.conv_up_03_c_sml(x_noised, ctx)
        x_c_sml = activation_fn(x_c_sml)
        x_c_sml = F.layer_norm(x_c_sml, x_c_sml.shape[1:])
        x_c_med = self.conv_up_03_c_med(x_noised, ctx)
        x_c_med = activation_fn(x_c_med)
        x_c_med = F.layer_norm(x_c_med, x_c_med.shape[1:])
        x_c_lrg = self.conv_up_03_c_lrg(x_noised, ctx)
        x_c_lrg = activation_fn(x_c_lrg)
        x_c_lrg = F.layer_norm(x_c_lrg, x_c_lrg.shape[1:])
        x_denoised = torch.cat([x_upsampled, x_c_sml, x_c_med, x_c_lrg], dim=1)
        x_denoised = self.conv_03_reducer_delta(x_denoised, ctx)
        x_denoised = activation_fn(x_denoised)
        x_denoised = F.layer_norm(x_denoised, x_denoised.shape[1:])
        x = x_denoised

        # Block 02
        x = self.dropout(x)
        ctx = (ctx.unsqueeze(1) @ self.wl_ctx_02(ctx)).squeeze(1)
        ctx = activation_fn(ctx)
        ctx = F.layer_norm(ctx, ctx.shape[1:])
        ctx = self.dropout(ctx)
        x_upsampled = self.conv_02_upsampler(x, ctx)
        x_upsampled = activation_fn(x_upsampled)
        x_upsampled = F.layer_norm(x_upsampled, x_upsampled.shape[1:])
        noise = self.wl_noise_02(ctx)
        noise = activation_fn(noise).unsqueeze(1)
        noise = F.layer_norm(noise, noise.shape[1:])
        x_noised = x_upsampled + noise
        x_c_sml = self.conv_up_02_c_sml(x_noised, ctx)
        x_c_sml = activation_fn(x_c_sml)
        x_c_sml = F.layer_norm(x_c_sml, x_c_sml.shape[1:])
        x_c_med = self.conv_up_02_c_med(x_noised, ctx)
        x_c_med = activation_fn(x_c_med)
        x_c_med = F.layer_norm(x_c_med, x_c_med.shape[1:])
        x_c_lrg = self.conv_up_02_c_lrg(x_noised, ctx)
        x_c_lrg = activation_fn(x_c_lrg)
        x_c_lrg = F.layer_norm(x_c_lrg, x_c_lrg.shape[1:])
        x_denoised = torch.cat([x_upsampled, x_c_sml, x_c_med, x_c_lrg], dim=1)
        x_denoised = self.conv_02_reducer_delta(x_denoised, ctx)
        x_denoised = activation_fn(x_denoised)
        x_denoised = F.layer_norm(x_denoised, x_denoised.shape[1:])
        x = x_denoised

        # Block 01
        x = self.dropout(x)
        ctx = (ctx.unsqueeze(1) @ self.wl_ctx_01(ctx)).squeeze(1)
        ctx = activation_fn(ctx)
        ctx = F.layer_norm(ctx, ctx.shape[1:])
        ctx = self.dropout(ctx)
        x_upsampled = self.conv_01_upsampler(x, ctx)
        x_upsampled = activation_fn(x_upsampled)
        x_upsampled = F.layer_norm(x_upsampled, x_upsampled.shape[1:])
        noise = self.wl_noise_01(ctx)
        noise = activation_fn(noise).unsqueeze(1)
        noise = F.layer_norm(noise, noise.shape[1:])
        x_noised = x_upsampled + noise
        x_c_sml = self.conv_up_01_c_sml(x_noised, ctx)
        x_c_sml = activation_fn(x_c_sml)
        x_c_sml = F.layer_norm(x_c_sml, x_c_sml.shape[1:])
        x_c_med = self.conv_up_01_c_med(x_noised, ctx)
        x_c_med = activation_fn(x_c_med)
        x_c_med = F.layer_norm(x_c_med, x_c_med.shape[1:])
        x_c_lrg = self.conv_up_01_c_lrg(x_noised, ctx)
        x_c_lrg = activation_fn(x_c_lrg)
        x_c_lrg = F.layer_norm(x_c_lrg, x_c_lrg.shape[1:])
        x_denoised = torch.cat([x_upsampled, x_c_sml, x_c_med, x_c_lrg], dim=1)
        x_denoised = self.conv_01_reducer_delta(x_denoised, ctx)
        x_denoised = activation_fn(x_denoised)
        x_denoised = F.layer_norm(x_denoised, x_denoised.shape[1:])
        x = x_denoised

        # Block out
        ctx_morph = self.wl_ctx_out(ctx)
        ctx = (ctx.unsqueeze(1) @ ctx_morph).squeeze(1)
        ctx = activation_fn(ctx)
        ctx = F.layer_norm(ctx, ctx.shape[1:])
        x = self.conv_out(x, ctx)
        x = F.sigmoid(x)

        return x


def generate_data_from_images(
    shape: list[int],
    images_path_src: str,
    images_sample_count: int,
    starting_from: int = 0,
) -> torch.Tensor:
    data = torch.empty([images_sample_count, 3, *shape], dtype=torch.uint8)
    dir_contents = os.listdir(images_path_src)

    assert len(dir_contents) >= images_sample_count + starting_from, " ".join(
        [
            f"Not enough images in '{images_path_src}'.",
            f"Got: {len(dir_contents)}.",
            f"Need: {images_sample_count + starting_from}.",
        ]
    )

    for i in range(images_sample_count):
        image_name = dir_contents[starting_from + i]
        image_path = os.path.join(images_path_src, image_name)

        try:
            image = Image.open(image_path)
        except ValueError:
            print(f"Failed to load image '{image_path}'.")
            exit()

        image = image.resize([shape[1], shape[0]], Image.LANCZOS)
        image = transforms.ToTensor()(image)
        image = image.unsqueeze(0) * 255.0
        image = image.to(dtype=torch.uint8)
        data[i] = image

    return data


def generate_images_from_data(
    data: torch.Tensor,
    images_path_dst: str,
    prefix: str,
) -> None:
    data = data.to(dtype=torch.float16)
    for i in range(data.shape[0]):
        image = data[i].squeeze(0)
        # image = image.clamp(0.0, 1.0)
        image = (image - image.min()) / (image.max() - image.min())
        image = transforms.ToPILImage()(image)
        image_name = f"{prefix}_mat_{i}.png"
        image_path = os.path.join(images_path_dst, image_name)
        image.save(image_path)
    pass


def data_rgb_to_lab(
    data: torch.Tensor,
) -> torch.Tensor:
    with torch.no_grad():
        lab = kornia.color.rgb_to_lab(data)
        lab[:, 0, ...].div_(100.0)
        lab[:, 1:, ...].add_(128.0).div_(255.0)
    lab.to(data.device)
    return lab


def data_lab_to_rgb(
    data: torch.Tensor,
) -> torch.Tensor:
    rgb = data
    with torch.no_grad():
        rgb[:, 0, ...].mul_(100.0)
        rgb[:, 1:, ...].mul_(255.0).sub_(128.0)
        rgb = kornia.color.lab_to_rgb(rgb)
    rgb.to(data.device)
    return rgb


def train(
    data: torch.Tensor,
    total_steps: int,
    batch_size: int,
    grad_accumulation_steps: int,
    sliding_batch: bool,
    loss_channels_weights: torch.Tensor,
    use_regularization: bool,
    regularization_alpha_model: float,
    regularization_alpha_ctx: float,
    regularization_alpha_latents: float,
    regularization_low_weights_model_bound: Union[float, List[float]],
    regularization_low_weights_model_alpha: Union[float, List[float]],
    regularization_low_weights_fn: Union[Callable, List[Callable]],
    weights_hysteresis_loop: bool,
    weights_hysteresis_loop_zero_bound: float,
    weights_hysteresis_loop_zero_jump: float,
    loss_weights_main_reg: List[float],
    grad_min_clip_value: float,
    grad_max_clip_value: float,
    clip_grad_by: Optional[str],
    freeze_ctx_nth_epoch: Optional[int],
    freeze_ctx_epochs: Optional[int],
    freeze_latents_nth_epoch: Optional[int],
    freeze_latents_epochs: Optional[int],
    freeze_model_nth_epoch: Optional[int],
    freeze_model_epochs: Optional[int],
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    log_nth_iteration: int,
    images_path_dst: str = None,
    save_nth_step: int = 100,
    savers: list = [],
    to_save: list[bool] = [],
) -> None:
    var_logger = model.conv_out.weights_lib._log_var
    params_model = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print("\n# --------------------------------------------------- #\n")
    print(f"Model type: {type(model)}")
    print(f"Model parameters: {params_model}")
    print("\n# --------------------------------------------------- #\n")

    with torch.no_grad():
        data_lab = data.cpu().to(dtype=model.dtype_weights)
        data_lab = data_lab / 255.0
        data_lab = data_rgb_to_lab(data_lab)
        data_lab = data_lab.to(dtype=torch.float16, device=data.device)
        data = data.cpu().detach()
        gc.collect()

    loss_channels_weights = loss_channels_weights.to(data_lab.device)
    accumulation_step = 0
    loss_accumulator = 0
    epoch_ids = torch.randperm(data_lab.shape[0])
    epoch_idx = 0
    last_grad_ctx = 0.0
    last_grad_latents = 0.0
    loss_logging_accumulator = []

    frozen_ctx_last_epoch_id = 0
    epoch_frozen_ctx = None
    frozen_latents_last_epoch_id = 0
    epoch_frozen_latents = None
    frozen_model_last_epoch_id = 0
    epoch_frozen_model = None

    kldiv_loss_fn = nn.KLDivLoss(reduction="none", log_target=True)

    generate_images_from_data(
        data=data_lab_to_rgb(model(torch.arange(0, min(4, data_lab.shape[0]), 1))),
        images_path_dst=images_path_dst,
        prefix="initial_state",
    )
    print(f"Starting training. Epoch #{epoch_idx}")
    for step_idx in range(total_steps):
        if len(epoch_ids) - 1 < batch_size:
            epoch_idx = epoch_idx + 1
            print(f"\n# ==============> New epoch: #{epoch_idx}")
            epoch_ids = torch.randperm(data_lab.shape[0])

        if freeze_ctx_nth_epoch is not None and freeze_ctx_nth_epoch > 0:
            if epoch_frozen_ctx is None:
                if epoch_idx % freeze_ctx_nth_epoch == 0:
                    epoch_frozen_ctx = 1
                    frozen_ctx_last_epoch_id = epoch_idx
                    freeze_ctx(model)
            else:
                if frozen_ctx_last_epoch_id != epoch_idx:
                    frozen_ctx_last_epoch_id = epoch_idx
                    if epoch_frozen_ctx >= freeze_ctx_epochs:
                        unfreeze_ctx(model)
                        epoch_frozen_ctx = None
                    else:
                        epoch_frozen_ctx = epoch_frozen_ctx + 1

        if freeze_latents_nth_epoch is not None and freeze_latents_nth_epoch > 0:
            if epoch_frozen_latents is None:
                if epoch_idx % freeze_latents_nth_epoch == 0:
                    epoch_frozen_latents = 1
                    frozen_latents_last_epoch_id = epoch_idx
                    freeze_latents(model)
            else:
                if frozen_latents_last_epoch_id != epoch_idx:
                    frozen_latents_last_epoch_id = epoch_idx
                    if epoch_frozen_latents >= freeze_latents_epochs:
                        unfreeze_latents(model)
                        epoch_frozen_latents = None
                    else:
                        epoch_frozen_latents = epoch_frozen_latents + 1

        if freeze_model_nth_epoch is not None and freeze_model_nth_epoch > 0:
            if epoch_frozen_model is None:
                if epoch_idx % freeze_model_nth_epoch == 0:
                    epoch_frozen_model = 1
                    frozen_model_last_epoch_id = epoch_idx
                    freeze_model(model)
            else:
                if frozen_model_last_epoch_id != epoch_idx:
                    frozen_model_last_epoch_id = epoch_idx
                    if epoch_frozen_model >= freeze_model_epochs:
                        unfreeze_model(model)
                        epoch_frozen_model = None
                    else:
                        epoch_frozen_model = epoch_frozen_model + 1

        batch_ids = epoch_ids[0:batch_size]
        epoch_ids = epoch_ids[(1 if sliding_batch else batch_size) :]

        sample = data_lab[batch_ids]
        sample = sample.to(dtype=model.dtype_weights)

        accumulation_step = accumulation_step + 1
        decoded = model(batch_ids)

        loss_channels_weights = loss_channels_weights.reshape([1, 3, 1, 1])
        loss_base_decoded = decoded * loss_channels_weights
        loss_base_targets = sample * loss_channels_weights

        # Loss A
        # loss_main = (loss_base_targets - loss_base_decoded).std().sqrt()

        # Loss B
        # loss_main = F.l1_loss(loss_base_decoded, loss_base_targets)

        # Loss C
        # loss_main = F.mse_loss(loss_base_decoded, loss_base_targets)

        # Loss D
        loss_base_targets = (loss_base_targets + 0.01) / (1.02)
        loss_base_decoded = F.log_softmax(loss_base_decoded.flatten(2), dim=-1)
        loss_base_targets = F.log_softmax(loss_base_targets.flatten(2), dim=-1)
        loss_main = kldiv_loss_fn(loss_base_decoded, loss_base_targets)
        loss_main = loss_main.sum().div(math.prod(loss_base_targets.shape[0:2]))

        # Regularizations
        null_placeholder = torch.tensor([0.0]).to(
            dtype=loss_main.dtype,
            device=loss_main.device,
        )
        reg_term_model = (
            get_regularization_term_model(
                model=model,
                alpha=regularization_alpha_model,
            )
            if use_regularization
            else null_placeholder.clone()
        )
        reg_term_ctx = (
            get_regularization_term_ctx(
                model=model,
                alpha=regularization_alpha_ctx,
            )
            if use_regularization
            else null_placeholder.clone()
        )
        reg_term_latents = (
            get_regularization_term_latents(
                model=model,
                alpha=regularization_alpha_latents,
            )
            if use_regularization
            else null_placeholder.clone()
        )

        if regularization_low_weights_fn is not None:
            if type(regularization_low_weights_fn) == list:
                reg_sum = 0.0
                for fn, bound, alpha in zip(
                    regularization_low_weights_fn,
                    regularization_low_weights_model_bound,
                    regularization_low_weights_model_alpha,
                ):
                    reg_sum = reg_sum + fn(model, bound, alpha)
                reg_term_low_weights_model = reg_sum
            else:
                reg_term_low_weights_model = regularization_low_weights_fn(
                    model=model,
                    bound=regularization_low_weights_model_bound,
                    alpha=regularization_low_weights_model_alpha,
                )
        else:
            reg_term_low_weights_model = null_placeholder.clone()

        loss_reg = (
            reg_term_model
            + reg_term_ctx
            + reg_term_latents
            + reg_term_low_weights_model
        )

        loss = (
            loss_main * loss_weights_main_reg[0] + loss_reg * loss_weights_main_reg[1]
        )
        loss_logging_accumulator.append(loss.item())

        if accumulation_step == grad_accumulation_steps:
            accumulation_step = 0

            loss_accumulator = loss_accumulator + loss
            loss_accumulator = loss_accumulator / grad_accumulation_steps

            optimizer.zero_grad()
            loss_accumulator.backward()

            last_grad_ctx = (
                model.data_cache_ctx.grad.abs().mean().item()
                if model.data_cache_ctx.requires_grad
                and model.data_cache_ctx.requires_grad is not None
                else -1.0
            )
            last_grad_latents = (
                model.data_cache_latents.grad.abs().mean().item()
                if model.data_cache_latents.requires_grad
                and model.data_cache_latents.grad is not None
                else -1.0
            )

            grad_clip_val = -1.0
            if clip_grad_by is not None:
                if "ctx" in clip_grad_by:
                    grad_clip_val = model.data_cache_ctx.grad.abs().mean().item()
                    torch.nn.utils.clip_grad_value_(model.parameters(), grad_clip_val)
                if "latents" in clip_grad_by:
                    grad_clip_val = model.data_cache_latents.grad.abs().mean().item()
                    torch.nn.utils.clip_grad_value_(model.parameters(), grad_clip_val)
                if "mean" in clip_grad_by:
                    mean_grads = []
                    for name, param in model.named_parameters():
                        if param.requires_grad and param.grad is not None:
                            mean_grads.append(param.grad.abs().mean().item())
                    grad_clip_val = sum(mean_grads) / len(mean_grads)
                    torch.nn.utils.clip_grad_value_(model.parameters(), grad_clip_val)
            else:
                if grad_min_clip_value is not None and grad_max_clip_value is not None:
                    if grad_min_clip_value == grad_max_clip_value:
                        grad_clip_val = grad_min_clip_value
                    else:
                        mean_grad = [
                            p.grad.abs().mean().item() for p in model.parameters()
                        ]
                        mean_grad = sum(mean_grad) / len(mean_grad)
                        grad_clip_val = max(grad_min_clip_value, mean_grad)
                        grad_clip_val = min(grad_clip_val, grad_max_clip_value)
                    torch.nn.utils.clip_grad_value_(model.parameters(), grad_clip_val)

            optimizer.step()

            if weights_hysteresis_loop:
                weights_hysteresis_loop_zero(
                    model=model,
                    bound=weights_hysteresis_loop_zero_bound,
                    jump=weights_hysteresis_loop_zero_jump,
                )

            loss_accumulator = 0
        elif loss_accumulator == 0:
            loss_accumulator = loss
        else:
            loss_accumulator = loss_accumulator + loss

        # print(f"step loss: {loss.item()}")

        if (step_idx + 1) % log_nth_iteration == 0:
            print(
                "\n# ==============> "
                + "\n".join(
                    [
                        f"Iteration #{step_idx+1}:",
                        f"Loss main: {loss_main.item()}",
                        f"Loss reg.: {loss_reg.item()}; model={reg_term_model.item():.5f}, ctx={reg_term_ctx.item():.5f}, latents={reg_term_latents.item():.5f}",
                        f"Loss m/r scaled: {loss_main.item()/loss_weights_main_reg[0]:.5f}/{loss_reg.item()/loss_weights_main_reg[1]:.5f}",
                        f"Loss total: {loss.item()}",
                        f"Mean grad ctx/latents: {last_grad_ctx:.10f}/{last_grad_latents:.10f}",
                        f"Grad clipping level: {grad_clip_val:.10f}",
                        f"StdR: {(sample - decoded).std()}",
                        f"Mean loss: {(sum(loss_logging_accumulator)/len(loss_logging_accumulator)):.5f}",
                    ]
                )
                + "\n# <=============="
            )
            loss_logging_accumulator = []
            # var_logger(model.data_cache_ctx, "model.data_cache_ctx", False)
            # var_logger(model.data_cache_latents, "model.data_cache_latents", False)
            generate_images_from_data(
                data=data_lab_to_rgb(decoded[-1].unsqueeze(0)),
                images_path_dst=images_path_dst,
                prefix=f"output_e{epoch_idx:0>4d}_i{(step_idx+1):0>7d}",
            )
        if (step_idx + 1) % save_nth_step == 0:
            for j, saver in enumerate(savers):
                if to_save[j]:
                    saver(step_idx + 1)
            pass

    print("\n# --------------------------------------------------- #\n")

    generate_images_from_data(
        data=data_lab_to_rgb(decoded[-1].unsqueeze(0)),
        images_path_dst=images_path_dst,
        prefix=f"output_final_i{step_idx+1}",
    )

    pass


def get_regularization_term_model(
    model: nn.Module,
    alpha: float,
) -> torch.Tensor:
    sum = 0.0
    for name, param in model.named_parameters():
        if "data_cache" in name:
            continue
        else:
            sum = sum + (param**2).sum()
    return alpha * sum


def get_regularization_term_ctx(
    model: nn.Module,
    alpha: float,
) -> torch.Tensor:
    sum = 0.0
    for name, param in model.named_parameters():
        if "data_cache_ctx" in name:
            sum = sum + (param**2).sum()
    return alpha * (sum / model.data_cache_ctx.shape[0])


def get_regularization_term_latents(
    model: nn.Module,
    alpha: float,
) -> torch.Tensor:
    sum = 0.0
    for name, param in model.named_parameters():
        if "data_cache_latents" in name:
            sum = sum + (param**2).sum()
    return alpha * (sum / model.data_cache_latents.shape[0])


def get_regularization_term_low_weights_model_alpha(
    model: nn.Module,
    bound: float,
    alpha: float,
) -> torch.Tensor:
    sum = 0.0
    for name, param in model.named_parameters():
        if any(
            [
                "data_cache" in name,
                "weights_lib.mod_i" in name,
                "weights_lib.mod_j" in name,
            ]
        ):
            continue
        vars = param.abs().clamp(0.0, bound).sub(bound).abs()
        sum = sum + vars.sum()
    return alpha * sum


def get_regularization_term_low_weights_model_beta(
    model: nn.Module,
    bound: float,
    alpha: float,
) -> torch.Tensor:
    sum = 0.0
    for name, param in model.named_parameters():
        if any(
            [
                "data_cache" in name,
                "weights_lib.mod_i" in name,
                "weights_lib.mod_j" in name,
            ]
        ):
            continue
        vars = param.abs().clamp(0.0, bound).sub(bound).abs()
        varsum = vars.mul(1.0 / bound).sqrt()
        varsum = varsum / ((varsum[varsum > 0.0]).numel() + 1.0)
        varsum = varsum.sum()
        sum = sum + varsum
    return alpha * sum


def weights_hysteresis_loop_zero(
    model: nn.Module,
    bound: float,
    jump: float,
) -> None:
    with torch.no_grad():
        for name, param in model.named_parameters():
            if "data_cache" in name:
                continue
            cond_to_neg = (param > -0.0) & (param < +bound)
            cond_to_pos = (param < +0.0) & (param > -bound)
            if cond_to_neg.any():
                param[cond_to_neg] = param[cond_to_neg] - jump
            if cond_to_pos.any():
                param[cond_to_pos] = param[cond_to_pos] + jump


def count_vals(
    model: nn.Module,
    target: float,
    excl: str = None,
    incl: str = None,
    abs: bool = True,
    mode: str = "gt",
) -> int:
    assert mode in ["gt", "lt", "eq"], f"mode must be 'gt' or 'lt', got {mode}"
    with torch.no_grad():
        cond_fn = lambda x: torch.where(
            ((x.abs() if abs else x) < target)
            if mode == "lt"
            else (
                ((x.abs() if abs else x) > target)
                if mode == "gt"
                else ((x.abs() if abs else x) == target)
            )
        )
        cnt_fn = lambda x, acc, cond: acc + x[cond].numel()
        sum_fn = lambda x, acc, cond: acc + (
            x[cond].abs().sum() if abs else x[cond].sum()
        )
        cnt = 0
        sum = 0
        cond = None
        for name, param in model.named_parameters():
            if excl is not None and excl in name:
                continue
            else:
                cond = cond_fn(param)
            if incl is not None:
                cnt = cnt_fn(param, cnt, cond) if incl in name else cnt
                sum = sum_fn(param, sum, cond) if incl in name else sum
            else:
                cnt = cnt_fn(param, cnt, cond)
                sum = sum_fn(param, sum, cond)
    return dict(cnt=cnt, sum=sum)


def count_params(
    model: nn.Module,
) -> int:
    sum = 0
    with torch.no_grad():
        for name, param in model.named_parameters():
            if "data_cache" in name:
                continue
            sum = sum + param.numel()
    return sum


def model_info(
    model: nn.Module,
) -> None:
    info = [
        f"{n: <64s} -> "
        + ", ".join(
            [
                f"{p.abs().min().item()=:.6f}",
                f"{p.abs().max().item()=:.6f}",
                f"{p.abs().mean().item()=:.6f}",
                f"{p.min().item()=:.6f}",
                f"{p.max().item()=:.6f}",
                f"{p.mean().item()=:.6f}",
                f"{p.std().item()=:.6f}",
            ]
        )
        for n, p in model.named_parameters()
    ]
    print("\n".join(info))
    pass


def freeze_model(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache" in name:
            continue
        param.requires_grad = False
        print(f"freeze_model: {name}")
    pass


def unfreeze_model(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache" in name:
            continue
        param.requires_grad = True
        print(f"unfreeze_model: {name}")
    pass


def freeze_latents(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache_latents" in name:
            param.requires_grad = False
            print(f"freeze_latents: {name}")
    pass


def unfreeze_latents(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache_latents" in name:
            param.requires_grad = True
            print(f"unfreeze_latents: {name}")
    pass


def freeze_ctx(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache_ctx" in name:
            param.requires_grad = False
            print(f"freeze_ctx: {name}")
    pass


def unfreeze_ctx(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache_ctx" in name:
            param.requires_grad = True
            print(f"unfreeze_ctx: {name}")
    pass


def modify_cache_latents(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache_latents" in name:
            base = torch.ones_like(param.data)
            deviation = torch.nn.init.normal_(
                torch.empty_like(param.data),
                mean=0.0,
                std=1.0e-2,
            )
            param.data = base + deviation
            print(f"modify_cache_latents: {name}")
    pass


def perturb_weights(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    for name, param in model.named_parameters():
        if "data_cache" in name:
            continue
        param.data = param.data + (torch.randn_like(param.data) * 1.0e-2)
        print(f"perturb_weights: {name}")
    pass


def data_cache_double(
    model: DecoderOnlyModel,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> None:
    model.data_cache_ctx = nn.Parameter(
        torch.cat(
            [
                model.data_cache_ctx,
                torch.zeros_like(model.data_cache_ctx),
            ],
            dim=0,
        )
    )
    model.data_cache_latents = nn.Parameter(
        torch.cat(
            [
                model.data_cache_latents,
                torch.zeros_like(model.data_cache_latents),
            ],
            dim=0,
        )
    )
    print(f"change_data_cache: {model.data_cache_ctx.shape}")
    print(f"change_data_cache: {model.data_cache_latents.shape}")
    pass


def quantize_weights(
    model: DecoderOnlyModel,
    device: torch.device,
    dtype: torch.dtype,
) -> None:
    levels = 2**8
    offset = 1.0 / (levels * 2)
    scale = 1.5

    with torch.no_grad():
        for name, param in model.named_parameters():
            if "data_cache" in name:
                continue
            sign = torch.where(param.data > 0.0, +1.0, -1.0)
            sign = sign.to(dtype=dtype, device=device)
            d_max = param.data.abs().max()
            d_s = ((param.data / d_max) * (levels - 1)) // 1.0
            d_s = ((d_s / levels) + offset).mul(sign).mul(scale)
            d_s = d_s.to(dtype=dtype, device=device)
            param.data = d_s
            print(
                "; ".join(
                    [
                        f"quantize_weights: {name}",
                        f"levels={levels}",
                        f"offset={offset}",
                        f"scale={scale}",
                    ]
                )
            )
    pass


def eval_infer(
    model: DecoderOnlyModel,
) -> None:
    model_output = model(torch.arange(0, 16, 1))
    data_rgb = data_lab_to_rgb(model_output)
    generate_images_from_data(data_rgb, images_path_dst, "infer")
    pass


def eval_ctx_rand(
    model: DecoderOnlyModel,
) -> None:
    ctx = model.data_cache_ctx.data.clone()
    model.data_cache_ctx.data = torch.randn_like(ctx)
    model_output = model(torch.arange(0, 16, 1))
    model.data_cache_ctx.data = ctx
    data_rgb = data_lab_to_rgb(model_output)
    generate_images_from_data(data_rgb, images_path_dst, "ctx_rand")
    pass


def eval_latents_rand(
    model: DecoderOnlyModel,
) -> None:
    latents = model.data_cache_latents.data.clone()
    model.data_cache_latents.data = torch.randn_like(latents)
    model_output = model(torch.arange(0, 16, 1))
    model.data_cache_latents.data = latents
    data_rgb = data_lab_to_rgb(model_output)
    generate_images_from_data(data_rgb, images_path_dst, "latents_rand")
    pass


def eval_latents_ones(
    model: DecoderOnlyModel,
) -> None:
    latents = model.data_cache_latents.data.clone()
    model.data_cache_latents.data = torch.ones_like(latents)
    model_output = model(torch.arange(0, 16, 1))
    model.data_cache_latents.data = latents
    data_rgb = data_lab_to_rgb(model_output)
    generate_images_from_data(data_rgb, images_path_dst, "latents_ones")
    pass


def downscale_weights(
    model: DecoderOnlyModel,
    lim: float = 1.0,
) -> None:
    for name, param in model.named_parameters():
        pmax = param.data.abs().max()
        if pmax > lim:
            param.data = param.data / pmax
            print(f"weights_clamp: {name}")
    pass


if __name__ == "__main__":
    train_mode = True

    load_model = False
    load_optim = False
    drop_ctx_cache = False
    drop_latents_cache = False
    # onload_fn = perturb_weights
    # onload_fn = freeze_model
    # onload_fn = data_cache_double
    # onload_fn = [
    #     quantize_weights,
    #     freeze_model,
    # ]
    # onload_fn = quantize_weights
    # onload_fn = [
    #     freeze_latents,
    #     freeze_ctx,
    # ]
    # onload_fn = [
    #     data_cache_double,
    #     freeze_model,
    # ]
    # onload_fn = freeze_model
    # onload_fn = freeze_latents
    # onload_fn = [
    #     data_cache_double,
    #     data_cache_double,
    # ]
    onload_fn = None

    path_prefix_load = "/mnt/f/git_AIResearch/dyna/data/models"
    path_prefix_save = "/mnt/f/git_AIResearch/dyna/data/models"
    load_path_model = f"{path_prefix_load}/decoder_model_G1.60000.pth"
    load_path_optim = f"{path_prefix_load}/"
    save_path_model = f"{path_prefix_save}/decoder_model_G1"
    save_path_optim = f"{path_prefix_save}/decoder_optim_G1"
    save_model = True
    save_optim = False
    save_nth_step = 10_000
    log_nth_iteration = 10

    learning_rate = 1.0e-5
    momentum = 0.9
    weight_decay = 0.0
    eps = 1.0e-5
    data_cache_ctx_bound = 1.0e-4
    data_cache_latents_bound = 1.0e-4
    use_regularization = False
    regularization_alpha_model = 1.0e-9
    regularization_alpha_ctx = 1.0e-2
    regularization_alpha_latents = 1.0e-4
    regularization_low_weights_model_bound = [
        1.0e-2,
        1.0e-4,
    ]
    regularization_low_weights_model_alpha = [
        2.5e-6,
        1.0e-3,
    ]
    # regularization_low_weights_fn = [
    #     get_regularization_term_low_weights_model_alpha,
    #     get_regularization_term_low_weights_model_beta,
    # ]
    regularization_low_weights_fn = None
    weights_hysteresis_loop = False
    weights_hysteresis_loop_zero_bound = 1.0e-3
    weights_hysteresis_loop_zero_jump = 2.0e-3
    loss_channels_weights = [1.0, 1.0, 1.0]
    loss_weights_main_reg = [1.0, 1.0]
    grad_min_clip_value = None
    grad_max_clip_value = None
    # clip_grad_by = "mean"  # ctx/latents/mean/None
    # clip_grad_by = "latents"
    # clip_grad_by = "ctx"
    clip_grad_by = None

    freeze_ctx_nth_epoch = 0
    freeze_ctx_epochs = 0
    freeze_latents_nth_epoch = 0
    freeze_latents_epochs = 0
    freeze_model_nth_epoch = 0
    freeze_model_epochs = 0

    data_cache_ctx_len = 2048
    data_cache_latents_len = 2048
    data_cache_latents_shape = [8, 32, 32]
    dropout_rate = 0.05

    total_steps = 100_000
    batch_size = 32
    sliding_batch = False
    grad_accumulation_steps = 1

    images_sample_count = 2048
    starting_from = 1024 * 8
    images_path_src = "/mnt/f/Datasets/Images_512x512/dataset_01"
    images_path_dst = "/mnt/f/git_AIResearch/dyna/data/img_dst"
    output_shape = [512, 512]
    dtype_weights = torch.float32
    device = torch.device("cuda")

    model = DecoderOnlyModel(
        data_cache_ctx_len=data_cache_ctx_len,
        data_cache_latents_len=data_cache_latents_len,
        data_cache_latents_shape=data_cache_latents_shape,
        dropout_rate=dropout_rate,
        data_cache_ctx_bound=data_cache_ctx_bound,
        data_cache_latents_bound=data_cache_latents_bound,
        dtype_weights=dtype_weights,
    ).to(device=device, dtype=dtype_weights)

    if load_model:
        inconsistent_keys = model.load_state_dict(
            torch.load(load_path_model),
            strict=False,
        )
        print(f"Model loaded from {load_path_model}")

    if onload_fn is not None:
        if callable(onload_fn):
            onload_fn(model, device, dtype_weights)
        if type(onload_fn) is list:
            for fn in onload_fn:
                fn(model, device, dtype_weights)

    if drop_ctx_cache:
        model.data_cache_ctx = nn.Parameter(
            torch.nn.init.uniform_(
                model.data_cache_ctx,
                a=-data_cache_ctx_bound,
                b=+data_cache_ctx_bound,
            ).to(
                dtype=dtype_weights,
            )
        )
        print("Dropped: data_cache_ctx")

    if drop_latents_cache:
        model.data_cache_latents = nn.Parameter(
            torch.nn.init.uniform_(
                model.data_cache_latents,
                a=-data_cache_latents_bound,
                b=+data_cache_latents_bound,
            ).to(
                dtype=dtype_weights,
            )
        )
        print("Dropped: data_cache_latents")

    optimizer = MADGRAD(
        model.parameters(),
        lr=learning_rate,
        weight_decay=weight_decay,
        momentum=momentum,
        eps=eps,
    )

    if load_optim:
        optimizer.load_state_dict(torch.load(load_path_optim))
        print(f"Optimizer loaded from {load_path_optim}")

    with torch.no_grad():
        data = generate_data_from_images(
            shape=output_shape,
            images_path_src=images_path_src,
            images_sample_count=images_sample_count,
            starting_from=starting_from,
        ).to(device)

    print("Generated data specs:")
    print(f"{data.min()=}")
    print(f"{data.max()=}")

    savers = [
        lambda p: torch.save(model.state_dict(), f"{save_path_model}.{p}.pth"),
        lambda p: torch.save(optimizer.state_dict(), f"{save_path_optim}.{p}.pth"),
    ]

    start_training = lambda: train(
        data=data,
        total_steps=total_steps,
        batch_size=batch_size,
        grad_accumulation_steps=grad_accumulation_steps,
        sliding_batch=sliding_batch,
        loss_channels_weights=torch.tensor(loss_channels_weights),
        use_regularization=use_regularization,
        regularization_alpha_model=regularization_alpha_model,
        regularization_alpha_ctx=regularization_alpha_ctx,
        regularization_alpha_latents=regularization_alpha_latents,
        regularization_low_weights_model_bound=regularization_low_weights_model_bound,
        regularization_low_weights_model_alpha=regularization_low_weights_model_alpha,
        regularization_low_weights_fn=regularization_low_weights_fn,
        weights_hysteresis_loop=weights_hysteresis_loop,
        weights_hysteresis_loop_zero_bound=weights_hysteresis_loop_zero_bound,
        weights_hysteresis_loop_zero_jump=weights_hysteresis_loop_zero_jump,
        loss_weights_main_reg=loss_weights_main_reg,
        grad_min_clip_value=grad_min_clip_value,
        grad_max_clip_value=grad_max_clip_value,
        clip_grad_by=clip_grad_by,
        freeze_ctx_nth_epoch=freeze_ctx_nth_epoch,
        freeze_ctx_epochs=freeze_ctx_epochs,
        freeze_latents_nth_epoch=freeze_latents_nth_epoch,
        freeze_latents_epochs=freeze_latents_epochs,
        freeze_model_nth_epoch=freeze_model_nth_epoch,
        freeze_model_epochs=freeze_model_epochs,
        model=model,
        optimizer=optimizer,
        log_nth_iteration=log_nth_iteration,
        images_path_dst=images_path_dst,
        save_nth_step=save_nth_step,
        savers=savers,
        to_save=[save_model, save_optim],
    )

    if train_mode:
        start_training()
